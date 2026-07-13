from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import google.auth
import requests
from flask import Request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore, secretmanager, storage

FIRESTORE_COLLECTION_DELIVERIES = os.environ.get("DELIVERIES_COLLECTION", "deliveries")
FIRESTORE_COLLECTION_DOWNLOAD_LOGS = os.environ.get("DOWNLOAD_LOGS_COLLECTION", "download_logs")
FIRESTORE_COLLECTION_REPORT_DEFINITIONS = os.environ.get(
    "REPORT_DEFINITIONS_COLLECTION",
    "report_definitions",
)
FIRESTORE_COLLECTION_SCHEDULED_REPORT_RUNS = os.environ.get(
    "SCHEDULED_REPORT_RUNS_COLLECTION",
    "scheduled_report_runs",
)
REPORT_DEFINITION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
REPORT_DEFINITION_EDITABLE_FIELDS = (
    "name",
    "owner",
    "primary_operator",
    "customer_name",
    "default_report_month",
    "gcs_prefix",
    "drive_folder_name",
)
REPORT_DEFINITION_SCHEDULE_TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
REPORT_DEFINITION_SCHEDULE_RUN_IDEMPOTENCY_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{7,127}$")
REPORT_DEFINITION_DELIVERY_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
REPORT_DEFINITION_SCHEDULE_RUN_CONFIRMATION = "RUN_DUE_REPORTS"
REPORT_DEFINITION_SCHEDULE_GENERATION_CONFIRMATION = "GENERATE_REPORTS"
REPORT_DEFINITION_SCHEDULE_DELIVERY_CONFIRMATION = "CREATE_DELIVERY_RECORDS"
REPORT_DEFINITION_SCHEDULE_TIMEZONE_ALLOWLIST = {"Asia/Tokyo"}
REPORT_DEFINITION_SCHEDULE_TIMEZONES = {
    "Asia/Tokyo": timezone(timedelta(hours=9), "Asia/Tokyo"),
}
DEFAULT_REPORT_ALLOWED_GCS_PREFIXES = (
    "gs://ice-report-files/reports/plus/",
    "reports/plus/",
)
DEFAULT_REPORT_ALLOWED_DRIVE_FOLDERS = (
    "OMFダウンロード数報告",
    "126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt",
)
DEFAULT_REPORT_ALLOWED_QUERY_CONFIG_IDS = ("plus-monthly-default-v1",)
DEFAULT_REPORT_ALLOWED_MAPPING_VERSION_IDS = ("plus-monthly-table-mapping-v1",)
TEMPLATE_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SLACK_WEBHOOK_SECRET = os.environ.get(
    "SLACK_WEBHOOK_SECRET_NAME",
    os.environ.get("SLACK_WEBHOOK_SECRET", "slack-download-webhook-url"),
)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
DEFAULT_EXPIRES_DAYS = int(os.environ.get("DEFAULT_EXPIRES_DAYS", "7"))
DOWNLOAD_SIGNED_URL_SECONDS = int(os.environ.get("DOWNLOAD_SIGNED_URL_SECONDS", "300"))

SIGNED_URL_SERVICE_ACCOUNT = os.environ.get(
    "SIGNED_URL_SERVICE_ACCOUNT",
    "ice-report-runner@ice-sh.iam.gserviceaccount.com",
)


@dataclass
class Delivery:
    delivery_id: str
    token: str
    customer_name: str
    report_month: str
    current_version: int
    active: bool
    expires_at: datetime
    allowed_domains: list[str]
    allowed_emails: list[str]
    versions: list[dict[str, Any]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_normalized_email(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def get_email_domain(email: str) -> str:
    return normalize_email(email).split("@")[-1]


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_firestore_client() -> firestore.Client:
    return firestore.Client()


def get_storage_client() -> storage.Client:
    return storage.Client()


def parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
    if not gcs_uri.startswith("gs://"):
        raise ValueError("gcs_uri must start with gs://")

    bucket_and_object = gcs_uri[5:]
    bucket, _, object_name = bucket_and_object.partition("/")

    if not bucket or not object_name:
        raise ValueError("gcs_uri must be gs://bucket/object")

    return bucket, object_name


def get_secret(secret_id: str) -> str | None:
    project_id = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
        or os.environ.get("SECRET_PROJECT_ID")
        or "ice-sh"
    )

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"

    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8").strip()
    except Exception:
        return None


def get_slack_webhook_url() -> str | None:
    return SLACK_WEBHOOK_URL or get_secret(SLACK_WEBHOOK_SECRET)


def notify_slack(text: str) -> None:
    webhook_url = get_slack_webhook_url()

    if not webhook_url:
        return

    try:
        requests.post(
            webhook_url,
            json={"text": text},
            timeout=5,
        )
    except Exception:
        return


def notify_slack_event(title: str, payload: dict[str, Any]) -> None:
    lines = [
        title,
        f"delivery_id: {payload.get('delivery_id', '-')}",
        f"顧客: {payload.get('customer_name', '-')}",
        f"対象月: {payload.get('report_month', '-')}",
        f"version: v{payload.get('version', payload.get('current_version', '-'))}",
        f"file: {payload.get('file_name', '-')}",
        f"email: {payload.get('email', '-')}",
        f"状態: {payload.get('active', '-')}",
        f"日時: {payload.get('timestamp', utcnow().isoformat())}",
    ]

    if payload.get("download_url"):
        lines.append(f"URL: {payload.get('download_url')}")

    if payload.get("gcs_uri"):
        lines.append(f"GCS: {payload.get('gcs_uri')}")

    notify_slack("\n".join(lines))


def create_delivery_record(
    *,
    customer_name: str,
    report_month: str,
    gcs_uri: str,
    allowed_domains: list[str] | None = None,
    allowed_emails: list[str] | None = None,
    email: str | None = None,
    expires_days: int = DEFAULT_EXPIRES_DAYS,
    version_note: str | None = None,
) -> dict[str, Any]:
    token = generate_token()
    token_hash = hash_token(token)
    now = utcnow()
    expires_at = now + timedelta(days=expires_days)

    bucket, object_name = parse_gcs_uri(gcs_uri)
    file_name = object_name.rsplit("/", 1)[-1]
    url = f"{PUBLIC_BASE_URL.rstrip('/')}/d/{token}" if PUBLIC_BASE_URL else f"/d/{token}"

    normalized_allowed_emails = [
        normalize_email(e)
        for e in (allowed_emails or [])
        if str(e).strip()
    ]

    if email:
        normalized_allowed_emails.append(normalize_email(email))

    normalized_allowed_emails = sorted(set(normalized_allowed_emails))

    doc = {
        "token_hash": token_hash,
        "token": token,
        "public_download_url": url,
        "customer_name": customer_name,
        "report_month": report_month,
        "current_version": 1,
        "active": True,
        "expires_at": expires_at,
        "allowed_domains": [
            d.strip().lower()
            for d in (allowed_domains or [])
            if str(d).strip()
        ],
        "allowed_emails": normalized_allowed_emails,
        "versions": [
            {
                "version": 1,
                "gcs_uri": gcs_uri,
                "bucket": bucket,
                "object_name": object_name,
                "file_name": file_name,
                "created_at": now,
                "note": version_note or "initial",
            }
        ],
        "created_at": now,
        "updated_at": now,
    }

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_DELIVERIES).document()
    ref.set(doc)

    result = {
        "delivery_id": ref.id,
        "download_url": url,
        "public_download_url": url,
        "token": token,
        "expires_at": expires_at.isoformat(),
        "gcs_uri": gcs_uri,
    }

    notify_slack_event(
        "ICEレポート配布URLが作成されました",
        {
            "delivery_id": ref.id,
            "customer_name": customer_name,
            "report_month": report_month,
            "version": 1,
            "file_name": file_name,
            "gcs_uri": gcs_uri,
            "download_url": url,
            "active": True,
            "timestamp": now.isoformat(),
        },
    )

    return result


def create_scheduled_delivery_record(
    *,
    customer_name: str,
    report_month: str,
    gcs_uri: str,
    allowed_domains: list[str] | None = None,
    allowed_emails: list[str] | None = None,
    expires_days: int = DEFAULT_EXPIRES_DAYS,
    version_note: str | None = None,
) -> dict[str, Any]:
    token = generate_token()
    token_hash = hash_token(token)
    now = utcnow()
    expires_at = now + timedelta(days=expires_days)

    bucket, object_name = parse_gcs_uri(gcs_uri)
    file_name = object_name.rsplit("/", 1)[-1]
    url = f"{PUBLIC_BASE_URL.rstrip('/')}/d/{token}" if PUBLIC_BASE_URL else f"/d/{token}"

    normalized_allowed_emails = sorted(
        {
            normalize_email(e)
            for e in (allowed_emails or [])
            if str(e).strip()
        }
    )
    normalized_allowed_domains = sorted(
        {
            d.strip().lower()
            for d in (allowed_domains or [])
            if str(d).strip()
        }
    )

    doc = {
        "token_hash": token_hash,
        "token": token,
        "public_download_url": url,
        "customer_name": customer_name,
        "report_month": report_month,
        "current_version": 1,
        "active": True,
        "expires_at": expires_at,
        "allowed_domains": normalized_allowed_domains,
        "allowed_emails": normalized_allowed_emails,
        "versions": [
            {
                "version": 1,
                "gcs_uri": gcs_uri,
                "bucket": bucket,
                "object_name": object_name,
                "file_name": file_name,
                "created_at": now,
                "note": version_note or "scheduled",
            }
        ],
        "created_at": now,
        "updated_at": now,
        "source": "schedule_run",
    }

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_DELIVERIES).document()
    ref.set(doc)

    return {
        "delivery_id": ref.id,
        "expires_at": expires_at.isoformat(),
        "report_month": report_month,
        "output_file": file_name,
        "allowed_domain_count": len(normalized_allowed_domains),
        "allowed_email_count": len(normalized_allowed_emails),
    }


def _format_dt(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _public_delivery(
    delivery_id: str,
    delivery: dict[str, Any],
    include_versions: bool = False,
) -> dict[str, Any]:
    item = {
        "delivery_id": delivery_id,
        "token": delivery.get("token"),
        "public_download_url": delivery.get("public_download_url"),
        "download_url": delivery.get("public_download_url"),
        "customer_name": delivery.get("customer_name"),
        "report_month": delivery.get("report_month"),
        "current_version": delivery.get("current_version"),
        "active": delivery.get("active"),
        "expires_at": _format_dt(delivery.get("expires_at")),
        "created_at": _format_dt(delivery.get("created_at")),
        "updated_at": _format_dt(delivery.get("updated_at")),
        "allowed_domains": delivery.get("allowed_domains") or [],
        "allowed_emails": delivery.get("allowed_emails") or [],
    }

    if include_versions:
        versions = []

        for version in delivery.get("versions", []):
            version_copy = dict(version)
            version_copy["created_at"] = _format_dt(version_copy.get("created_at"))
            versions.append(version_copy)

        item["versions"] = versions

    return item


def _public_report_definition_version(
    version: dict[str, Any],
    *,
    current_version: int | None = None,
) -> dict[str, Any]:
    version_number = version.get("version")
    return {
        "version": version_number,
        "current": version_number is not None and version_number == current_version,
        "status": version.get("status") or "draft",
        "note": version.get("note") or version.get("change_summary") or "",
        "template_name": version.get("template_name") or version.get("template_file_name") or "",
        "query_config_id": version.get("query_config_id") or "",
        "mapping_version_id": version.get("mapping_version_id") or "",
        "created_at": _format_dt(version.get("created_at")),
        "updated_at": _format_dt(version.get("updated_at")),
    }


def _public_report_definition(
    report_id: str,
    definition: dict[str, Any],
    *,
    include_versions: bool = False,
) -> dict[str, Any]:
    versions = definition.get("versions") or []
    current_version = definition.get("current_version")
    if current_version is None and versions:
        current_version = max((v.get("version", 0) for v in versions), default=None)

    storage = definition.get("storage") or {}
    drive = definition.get("drive") or {}
    gcs = definition.get("gcs") or {}
    schedule = dict(definition.get("schedule") or {})
    if "enabled" not in schedule:
        schedule["enabled"] = definition.get("schedule_enabled", False)
    delivery_allowlist = _public_report_definition_delivery_allowlist(
        definition.get("delivery_allowlist") or {}
    )

    item = {
        "report_id": report_id,
        "name": (
            definition.get("name")
            or definition.get("report_name")
            or definition.get("display_name")
            or report_id
        ),
        "status": definition.get("status") or ("archived" if definition.get("archived") else "active"),
        "current_version": current_version,
        "version_count": len(versions),
        "owner": definition.get("owner") or definition.get("operational_owner") or "",
        "primary_operator": definition.get("primary_operator") or "",
        "customer_name": definition.get("customer_name") or "",
        "default_report_month": definition.get("default_report_month") or "",
        "gcs_prefix": definition.get("gcs_prefix") or gcs.get("prefix") or storage.get("gcs_prefix") or "",
        "drive_folder_name": (
            definition.get("drive_folder_name")
            or drive.get("folder_name")
            or storage.get("drive_folder_name")
            or ""
        ),
        "schedule_enabled": bool(schedule.get("enabled", False)),
        "schedule": _public_report_definition_schedule(schedule),
        "delivery_allowlist": delivery_allowlist,
        "created_at": _format_dt(definition.get("created_at")),
        "updated_at": _format_dt(definition.get("updated_at")),
        "archived_at": _format_dt(definition.get("archived_at")),
    }

    if include_versions:
        public_versions = [
            _public_report_definition_version(version, current_version=current_version)
            for version in versions
        ]
        public_versions.sort(key=lambda item: item.get("version") or 0, reverse=True)
        item["versions"] = public_versions

    return item


def list_report_definitions(*, limit: int = 100) -> list[dict[str, Any]]:
    db = get_firestore_client()
    query = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).limit(limit)

    items = [
        _public_report_definition(doc.id, doc.to_dict() or {})
        for doc in query.stream()
    ]

    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return items


def _validate_report_id(report_id: str) -> str:
    clean_id = (report_id or "").strip()
    if (
        not clean_id
        or "/" in clean_id
        or (clean_id.startswith("__") and clean_id.endswith("__"))
        or not REPORT_DEFINITION_ID_PATTERN.match(clean_id)
    ):
        raise ValueError("report_id not found")

    return clean_id


def _report_definition_payload(payload: dict[str, Any]) -> dict[str, Any]:
    item = {
        key: str(payload.get(key) or "").strip()
        for key in REPORT_DEFINITION_EDITABLE_FIELDS
    }
    _validate_report_definition_storage(item)
    return item


def _normalize_delivery_allowed_domains(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_items = [str(item).strip() for item in value]
    else:
        raise ValueError("allowed_domains must be a list")

    domains = sorted({item.lower() for item in raw_items if item})
    for domain in domains:
        if not REPORT_DEFINITION_DELIVERY_DOMAIN_PATTERN.match(domain):
            raise ValueError("allowed_domains contains invalid domain")
    return domains


def _normalize_delivery_allowed_email_hashes(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_items = [str(item).strip() for item in value]
    else:
        raise ValueError("allowed_emails must be a list")

    email_hashes = []
    for item in raw_items:
        email = normalize_email(item)
        if not email:
            continue
        if "@" not in email or not get_email_domain(email):
            raise ValueError("allowed_emails contains invalid email")
        email_hashes.append(hash_normalized_email(email))
    return sorted(set(email_hashes))


def _report_definition_delivery_allowlist_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_domains = _normalize_delivery_allowed_domains(payload.get("allowed_domains"))
    allowed_email_hashes = _normalize_delivery_allowed_email_hashes(payload.get("allowed_emails"))
    return {
        "allowed_domains": allowed_domains,
        "allowed_email_hashes": allowed_email_hashes,
        "allowed_domain_count": len(allowed_domains),
        "allowed_email_count": len(allowed_email_hashes),
    }


def _public_report_definition_delivery_allowlist(allowlist: dict[str, Any]) -> dict[str, Any]:
    allowed_domains = _normalize_delivery_allowed_domains(allowlist.get("allowed_domains"))
    allowed_email_hashes = [
        str(item)
        for item in (allowlist.get("allowed_email_hashes") or [])
        if str(item).strip()
    ]
    return {
        "allowed_domains": allowed_domains,
        "allowed_domain_count": int(allowlist.get("allowed_domain_count") or len(allowed_domains)),
        "allowed_email_count": int(allowlist.get("allowed_email_count") or len(allowed_email_hashes)),
        "updated_at": _format_dt(allowlist.get("updated_at")),
    }


def _split_allowlist(value: str | None, defaults: tuple[str, ...]) -> list[str]:
    items = [
        item.strip()
        for item in (value or "").split(",")
        if item.strip()
    ]
    return items or list(defaults)


def get_report_definition_storage_allowlist() -> dict[str, list[str]]:
    return {
        "gcs_prefixes": _split_allowlist(
            os.environ.get("REPORT_ALLOWED_GCS_PREFIXES"),
            DEFAULT_REPORT_ALLOWED_GCS_PREFIXES,
        ),
        "drive_folders": _split_allowlist(
            os.environ.get("REPORT_ALLOWED_DRIVE_FOLDERS"),
            DEFAULT_REPORT_ALLOWED_DRIVE_FOLDERS,
        ),
    }


def get_report_definition_query_mapping_allowlist() -> dict[str, list[str]]:
    return {
        "query_config_ids": _split_allowlist(
            os.environ.get("REPORT_ALLOWED_QUERY_CONFIG_IDS"),
            DEFAULT_REPORT_ALLOWED_QUERY_CONFIG_IDS,
        ),
        "mapping_version_ids": _split_allowlist(
            os.environ.get("REPORT_ALLOWED_MAPPING_VERSION_IDS"),
            DEFAULT_REPORT_ALLOWED_MAPPING_VERSION_IDS,
        ),
    }


def _normalize_storage_prefix(value: str) -> str:
    normalized = (value or "").strip().replace("\\", "/")
    while "//" in normalized.replace("gs://", "gs:/"):
        normalized = normalized.replace("//", "/")
        normalized = normalized.replace("gs:/", "gs://")
    return normalized


def _is_allowed_gcs_prefix(gcs_prefix: str, allowed_prefixes: list[str]) -> bool:
    if not gcs_prefix:
        return True
    normalized = _normalize_storage_prefix(gcs_prefix)
    if ".." in normalized.split("/"):
        return False

    for allowed in allowed_prefixes:
        allowed_normalized = _normalize_storage_prefix(allowed)
        if normalized == allowed_normalized or normalized.startswith(allowed_normalized):
            return True
    return False


def _is_allowed_drive_folder(drive_folder_name: str, allowed_folders: list[str]) -> bool:
    if not drive_folder_name:
        return True
    normalized = drive_folder_name.strip()
    return normalized in {item.strip() for item in allowed_folders if item.strip()}


def _validate_report_definition_storage(item: dict[str, Any]) -> None:
    allowlist = get_report_definition_storage_allowlist()

    if not _is_allowed_gcs_prefix(str(item.get("gcs_prefix") or ""), allowlist["gcs_prefixes"]):
        raise ValueError("gcs_prefix is not allowed")

    if not _is_allowed_drive_folder(
        str(item.get("drive_folder_name") or ""),
        allowlist["drive_folders"],
    ):
        raise ValueError("drive_folder_name is not allowed")


def _bool_from_payload(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _public_report_definition_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(schedule.get("enabled", False)),
        "frequency": schedule.get("frequency") or "monthly",
        "day_of_month": int(schedule.get("day_of_month") or 1),
        "time_of_day": schedule.get("time_of_day") or "09:00",
        "timezone": schedule.get("timezone") or "Asia/Tokyo",
    }


def _report_definition_schedule_payload(payload: dict[str, Any]) -> dict[str, Any]:
    frequency = str(payload.get("frequency") or "monthly").strip().lower()
    if frequency != "monthly":
        raise ValueError("schedule frequency must be monthly")

    try:
        day_of_month = int(payload.get("day_of_month") or 1)
    except (TypeError, ValueError):
        raise ValueError("schedule day_of_month must be a number") from None
    if day_of_month < 1 or day_of_month > 28:
        raise ValueError("schedule day_of_month must be between 1 and 28")

    time_of_day = str(payload.get("time_of_day") or "09:00").strip()
    if not REPORT_DEFINITION_SCHEDULE_TIME_PATTERN.match(time_of_day):
        raise ValueError("schedule time_of_day must be HH:MM")

    timezone_name = str(payload.get("timezone") or "Asia/Tokyo").strip()
    if timezone_name not in REPORT_DEFINITION_SCHEDULE_TIMEZONE_ALLOWLIST:
        raise ValueError("schedule timezone is not allowed")

    return {
        "enabled": _bool_from_payload(payload.get("enabled", False)),
        "frequency": frequency,
        "day_of_month": day_of_month,
        "time_of_day": time_of_day,
        "timezone": timezone_name,
    }


def create_report_definition(payload: dict[str, Any]) -> dict[str, Any]:
    report_id = _validate_report_id(payload.get("report_id") or "")
    item = _report_definition_payload(payload)

    if not item["name"]:
        raise ValueError("name is required")

    now = utcnow()
    doc = {
        **item,
        "status": "active",
        "archived": False,
        "current_version": 1,
        "versions": [
            {
                "version": 1,
                "status": "draft",
                "note": str(payload.get("version_note") or "initial definition").strip(),
                "created_at": now,
                "updated_at": now,
            }
        ],
        "created_at": now,
        "updated_at": now,
    }

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if snap.exists:
        raise ValueError("report_id already exists")

    ref.set(doc)
    return _public_report_definition(report_id, doc, include_versions=True)


def update_report_definition(report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    item = _report_definition_payload(payload)

    if not item["name"]:
        raise ValueError("name is required")

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError("report_id not found")

    update_doc = {
        **item,
        "updated_at": utcnow(),
    }
    ref.update(update_doc)

    current = snap.to_dict() or {}
    current.update(update_doc)
    return _public_report_definition(report_id, current, include_versions=True)


def set_report_definition_schedule(report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    schedule = _report_definition_schedule_payload(payload)

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError("report_id not found")

    now = utcnow()
    schedule_doc = {
        **schedule,
        "updated_at": now,
    }
    update_doc = {
        "schedule": schedule_doc,
        "schedule_enabled": schedule["enabled"],
        "updated_at": now,
    }
    ref.update(update_doc)

    current = snap.to_dict() or {}
    current.update(update_doc)
    return _public_report_definition(report_id, current, include_versions=True)


def set_report_definition_delivery_allowlist(report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    delivery_allowlist = _report_definition_delivery_allowlist_payload(payload)

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError("report_id not found")

    now = utcnow()
    update_doc = {
        "delivery_allowlist": {
            **delivery_allowlist,
            "updated_at": now,
        },
        "updated_at": now,
    }
    ref.update(update_doc)

    current = snap.to_dict() or {}
    current.update(update_doc)
    return _public_report_definition(report_id, current, include_versions=True)


def _report_definition_schedule_preview_item(
    report_id: str,
    definition: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    schedule = dict(definition.get("schedule") or {})
    if "enabled" not in schedule:
        schedule["enabled"] = definition.get("schedule_enabled", False)
    public_schedule = _public_report_definition_schedule(schedule)

    local_now = now
    timezone_name = public_schedule["timezone"]
    schedule_timezone = REPORT_DEFINITION_SCHEDULE_TIMEZONES.get(timezone_name)
    if schedule_timezone is None:
        timezone_name = "UTC"
        local_now = now.astimezone(timezone.utc)
    else:
        local_now = now.astimezone(schedule_timezone)

    scheduled_time = public_schedule["time_of_day"]
    local_time = local_now.strftime("%H:%M")
    due = False
    reason = "disabled"

    if public_schedule["enabled"]:
        if public_schedule["frequency"] != "monthly":
            reason = "unsupported_frequency"
        elif public_schedule["day_of_month"] != local_now.day:
            reason = "day_mismatch"
        elif scheduled_time > local_time:
            reason = "before_scheduled_time"
        else:
            due = True
            reason = "due"

    public_definition = _public_report_definition(report_id, definition, include_versions=False)
    return {
        "report_id": report_id,
        "name": public_definition["name"],
        "status": public_definition["status"],
        "current_version": public_definition["current_version"],
        "schedule": public_schedule,
        "due": due,
        "reason": reason,
        "local_date": local_now.strftime("%Y-%m-%d"),
        "local_time": local_time,
        "timezone": timezone_name,
    }


def preview_report_definition_schedule_run(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    evaluation_time = now or utcnow()
    if evaluation_time.tzinfo is None:
        evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)

    db = get_firestore_client()
    query = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).limit(limit)
    items: list[dict[str, Any]] = []

    for doc in query.stream():
        definition = doc.to_dict() or {}
        if definition.get("archived") or definition.get("status") == "archived":
            continue
        items.append(
            _report_definition_schedule_preview_item(
                doc.id,
                definition,
                now=evaluation_time,
            )
        )

    items.sort(key=lambda item: (not item["due"], item["report_id"]))
    due_items = [item for item in items if item["due"]]
    scheduled_items = [item for item in items if item["schedule"]["enabled"]]
    return {
        "generated_at": _format_dt(utcnow()),
        "evaluation_time": _format_dt(evaluation_time),
        "dry_run": True,
        "items": items,
        "due_items": due_items,
        "counts": {
            "checked": len(items),
            "scheduled": len(scheduled_items),
            "due": len(due_items),
            "not_due": len(items) - len(due_items),
        },
    }


def _parse_schedule_run_evaluation_time(value: Any, *, now: datetime | None = None) -> datetime:
    if now is not None:
        evaluation_time = now
    elif value:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            evaluation_time = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("evaluation_time must be ISO-8601") from None
    else:
        evaluation_time = utcnow()

    if evaluation_time.tzinfo is None:
        evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)
    return evaluation_time


def _schedule_run_report_ids(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_items = [str(item).strip() for item in value]
    else:
        raise ValueError("report_ids must be a list")

    report_ids = [_validate_report_id(item) for item in raw_items if item]
    return sorted(set(report_ids))


def _schedule_run_mode(payload: dict[str, Any]) -> str:
    mode = str(payload.get("mode") or "dry_run").strip().lower()
    if mode not in {"dry_run", "execute"}:
        raise ValueError("mode must be dry_run or execute")
    return mode


def _schedule_run_execute_step(payload: dict[str, Any]) -> str:
    step = str(payload.get("execute_step") or "validate").strip().lower()
    if step not in {"validate", "generate", "deliver"}:
        raise ValueError("execute_step must be validate, generate, or deliver")
    return step


def _schedule_run_idempotency_hash(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("idempotency_key is required")
    if not REPORT_DEFINITION_SCHEDULE_RUN_IDEMPOTENCY_PATTERN.match(key):
        raise ValueError("idempotency_key is invalid")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _schedule_run_current_version(definition: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    try:
        return _find_report_definition_current_version(definition), ""
    except (TypeError, ValueError):
        return None, "current_version_not_found"


def _schedule_run_eligibility(
    report_id: str,
    definition: dict[str, Any],
    item: dict[str, Any],
) -> tuple[bool, str]:
    if not item["due"]:
        return False, str(item["reason"] or "not_due")
    if item["status"] != "active":
        return False, "inactive"
    if item["current_version"] is None:
        return False, "current_version_required"

    current_version, error_reason = _schedule_run_current_version(definition)
    if current_version is None:
        return False, error_reason
    if not str(current_version.get("template_gcs_uri") or "").strip():
        return False, "published_template_required"
    if not str(current_version.get("query_config_id") or "").strip():
        return False, "query_config_required"
    if not str(current_version.get("mapping_version_id") or "").strip():
        return False, "mapping_version_required"

    public_definition = _public_report_definition(report_id, definition)
    try:
        _validate_report_definition_storage(public_definition)
    except ValueError:
        return False, "storage_not_allowed"

    return True, "eligible"


def _schedule_run_record_id(item: dict[str, Any], idempotency_key_hash: str) -> str:
    source = "|".join(
        [
            str(item.get("report_id") or ""),
            str(item.get("local_date") or ""),
            str((item.get("schedule") or {}).get("time_of_day") or ""),
            str(item.get("timezone") or ""),
            idempotency_key_hash,
        ]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _safe_schedule_generation_result(result: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {
        "status": str(result.get("status") or "generated"),
        "report_month": str(result.get("report_month") or ""),
        "output_file": str(result.get("output_file") or ""),
        "has_gcs_object": bool(result.get("has_gcs_object", False)),
    }
    for key in ("paid_rows", "free_rows"):
        value = result.get(key)
        if isinstance(value, int):
            safe[key] = value
    return safe


def _schedule_run_allowed_domains(payload: dict[str, Any]) -> list[str]:
    return _normalize_delivery_allowed_domains(payload.get("allowed_domains"))


def _schedule_run_allowed_emails(payload: dict[str, Any]) -> list[str]:
    value = payload.get("allowed_emails")
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_items = [str(item).strip() for item in value]
    else:
        raise ValueError("allowed_emails must be a list")
    return sorted({normalize_email(item) for item in raw_items if item})


def _schedule_run_definition_delivery_allowlist(definition: dict[str, Any]) -> dict[str, list[str]]:
    allowlist = definition.get("delivery_allowlist") or {}
    return {
        "allowed_domains": _normalize_delivery_allowed_domains(allowlist.get("allowed_domains")),
        # Raw emails are intentionally not stored on report definitions. Request-time
        # allowed_emails may still be supplied for one-off manual smoke runs.
        "allowed_emails": [],
    }


def _safe_schedule_delivery_result(result: dict[str, Any]) -> dict[str, Any]:
    delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else result
    safe: dict[str, Any] = {
        "status": str(result.get("status") or delivery.get("status") or "delivery_created"),
        "report_month": str(result.get("report_month") or delivery.get("report_month") or ""),
        "output_file": str(result.get("output_file") or delivery.get("output_file") or ""),
        "has_gcs_object": bool(result.get("has_gcs_object", False)),
        "has_delivery_record": bool(delivery.get("delivery_id")),
        "delivery_id": str(delivery.get("delivery_id") or ""),
        "expires_at": str(delivery.get("expires_at") or ""),
    }
    for key in ("paid_rows", "free_rows", "allowed_domain_count", "allowed_email_count"):
        value = result.get(key)
        if isinstance(value, int):
            safe[key] = value
    return safe


def run_report_definition_schedule_runs(
    payload: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    mode = _schedule_run_mode(payload)
    execute_step = _schedule_run_execute_step(payload)
    dry_run = mode != "execute"
    try:
        limit = int(payload.get("limit") or 100)
    except (TypeError, ValueError):
        raise ValueError("limit must be a number") from None
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")

    report_ids = _schedule_run_report_ids(payload.get("report_ids"))
    report_id_filter = set(report_ids)
    evaluation_time = _parse_schedule_run_evaluation_time(
        payload.get("evaluation_time"),
        now=now,
    )

    idempotency_key_hash = ""
    if not dry_run:
        if str(payload.get("confirm") or "").strip() != REPORT_DEFINITION_SCHEDULE_RUN_CONFIRMATION:
            raise ValueError("execute confirmation is required")
        idempotency_key_hash = _schedule_run_idempotency_hash(payload.get("idempotency_key"))
        if execute_step in {"generate", "deliver"}:
            if str(payload.get("confirm_generation") or "").strip() != REPORT_DEFINITION_SCHEDULE_GENERATION_CONFIRMATION:
                raise ValueError("generation confirmation is required")
            if executor is None:
                raise ValueError("generation executor is required")
        if execute_step == "deliver":
            if str(payload.get("confirm_delivery") or "").strip() != REPORT_DEFINITION_SCHEDULE_DELIVERY_CONFIRMATION:
                raise ValueError("delivery confirmation is required")
            allowed_domains = _schedule_run_allowed_domains(payload)
            allowed_emails = _schedule_run_allowed_emails(payload)
        else:
            allowed_domains = []
            allowed_emails = []

    db = get_firestore_client()
    query = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).limit(limit)
    items: list[dict[str, Any]] = []
    seen_report_ids: set[str] = set()
    run_collection = db.collection(FIRESTORE_COLLECTION_SCHEDULED_REPORT_RUNS) if not dry_run else None
    record_time = utcnow()

    for doc in query.stream():
        if report_id_filter and doc.id not in report_id_filter:
            continue
        seen_report_ids.add(doc.id)
        definition = doc.to_dict() or {}
        if definition.get("archived") or definition.get("status") == "archived":
            continue

        item = _report_definition_schedule_preview_item(
            doc.id,
            definition,
            now=evaluation_time,
        )
        effective_allowed_domains = list(allowed_domains) if not dry_run and execute_step == "deliver" else []
        effective_allowed_emails = list(allowed_emails) if not dry_run and execute_step == "deliver" else []
        if not dry_run and execute_step == "deliver" and not effective_allowed_domains and not effective_allowed_emails:
            definition_allowlist = _schedule_run_definition_delivery_allowlist(definition)
            effective_allowed_domains = definition_allowlist["allowed_domains"]
            effective_allowed_emails = definition_allowlist["allowed_emails"]

        eligible, reason = _schedule_run_eligibility(doc.id, definition, item)
        if (
            not dry_run
            and execute_step == "deliver"
            and eligible
            and not effective_allowed_domains
            and not effective_allowed_emails
        ):
            eligible = False
            reason = "delivery_allowlist_required"
        item["eligible"] = eligible
        item["reason"] = reason
        item["action"] = "would_execute" if dry_run and eligible else "skip"

        if not dry_run and eligible:
            run_record_id = _schedule_run_record_id(item, idempotency_key_hash)
            run_ref = run_collection.document(run_record_id)  # type: ignore[union-attr]
            if run_ref.get().exists:
                item["eligible"] = False
                item["reason"] = "duplicate_run"
                item["action"] = "duplicate"
            else:
                record = {
                    "report_id": item["report_id"],
                    "schedule_local_date": item["local_date"],
                    "schedule_time": item["schedule"]["time_of_day"],
                    "schedule_timezone": item["timezone"],
                    "idempotency_key_hash": idempotency_key_hash,
                    "execution_step": execute_step,
                    "status": "validated",
                    "result_code": "execute_guard_validated",
                    "created_at": record_time,
                    "updated_at": record_time,
                }

                if execute_step == "validate":
                    run_ref.set(record)
                    item["action"] = "execute_guard_validated"
                else:
                    started_code = "generation_started" if execute_step == "generate" else "delivery_started"
                    run_ref.set(
                        {
                            **record,
                            "status": "running",
                            "result_code": started_code,
                        }
                    )
                    try:
                        public_definition = _public_report_definition(doc.id, definition)
                        executor_result = executor(  # type: ignore[misc]
                            {
                                "report_id": item["report_id"],
                                "name": item["name"],
                                "customer_name": public_definition.get("customer_name") or item["name"],
                                "local_date": item["local_date"],
                                "timezone": item["timezone"],
                                "schedule": item["schedule"],
                                "gcs_prefix": public_definition.get("gcs_prefix") or "",
                                "execute_step": execute_step,
                                "allowed_domains": effective_allowed_domains,
                                "allowed_emails": effective_allowed_emails,
                            }
                        )
                        safe_result = (
                            _safe_schedule_delivery_result(executor_result)
                            if execute_step == "deliver"
                            else _safe_schedule_generation_result(executor_result)
                        )
                    except Exception:
                        run_ref.update(
                            {
                                "status": "failed",
                                "result_code": "generation_failed" if execute_step == "generate" else "delivery_failed",
                                "updated_at": utcnow(),
                            }
                        )
                        raise

                    result_field = "generation" if execute_step == "generate" else "delivery"
                    result_code = "generation_succeeded" if execute_step == "generate" else "delivery_succeeded"
                    run_ref.update(
                        {
                            "status": "succeeded",
                            "result_code": result_code,
                            result_field: safe_result,
                            "updated_at": utcnow(),
                        }
                    )
                    item["action"] = "generated" if execute_step == "generate" else "delivery_created"
                    item[result_field] = safe_result

        items.append(item)

    for missing_report_id in sorted(report_id_filter - seen_report_ids):
        items.append(
            {
                "report_id": missing_report_id,
                "name": missing_report_id,
                "status": "missing",
                "current_version": None,
                "schedule": {},
                "due": False,
                "eligible": False,
                "reason": "report_id_not_found",
                "action": "skip",
                "local_date": "",
                "local_time": "",
                "timezone": "",
            }
        )

    items.sort(key=lambda item: (not item["due"], not item["eligible"], item["report_id"]))
    due_items = [item for item in items if item["due"]]
    eligible_items = [item for item in items if item["eligible"]]
    duplicate_items = [item for item in items if item["action"] == "duplicate"]
    validated_items = [item for item in items if item["action"] == "execute_guard_validated"]
    generated_items = [item for item in items if item["action"] == "generated"]
    delivered_items = [item for item in items if item["action"] == "delivery_created"]

    return {
        "generated_at": _format_dt(utcnow()),
        "evaluation_time": _format_dt(evaluation_time),
        "mode": mode,
        "execute_step": execute_step,
        "dry_run": dry_run,
        "items": items,
        "due_items": due_items,
        "eligible_items": eligible_items,
        "counts": {
            "checked": len(items),
            "due": len(due_items),
            "eligible": len(eligible_items),
            "validated": len(validated_items),
            "generated": len(generated_items),
            "delivered": len(delivered_items),
            "duplicates": len(duplicate_items),
            "skipped": len(items) - len(eligible_items) - len(duplicate_items),
        },
    }


def archive_report_definition(report_id: str) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError("report_id not found")

    now = utcnow()
    update_doc = {
        "status": "archived",
        "archived": True,
        "archived_at": now,
        "updated_at": now,
    }
    ref.update(update_doc)

    current = snap.to_dict() or {}
    current.update(update_doc)
    return _public_report_definition(report_id, current, include_versions=True)


def _safe_template_file_name(filename: str) -> str:
    safe_name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not safe_name.lower().endswith(".xlsx"):
        raise ValueError("template file must end with .xlsx")
    return safe_name


def _template_object_name(prefix: str, report_id: str, version: int, filename: str) -> str:
    clean_prefix = (prefix or "report-templates").strip().strip("/")
    if not clean_prefix:
        clean_prefix = "report-templates"
    return f"{clean_prefix}/{report_id}/v{version}/{_safe_template_file_name(filename)}"


def _build_template_version_doc(
    *,
    version: int,
    preview: dict[str, Any],
    gcs_uri: str,
    note: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "version": version,
        "status": "published",
        "note": (note or "template published").strip(),
        "template_name": _safe_template_file_name(str(preview.get("file_name") or "")),
        "template_gcs_uri": gcs_uri,
        "template_size_bytes": int(preview.get("size_bytes") or 0),
        "template_sha256": str(preview.get("sha256") or ""),
        "template_sheet_count": int(preview.get("sheet_count") or 0),
        "template_sheets": list(preview.get("sheets") or []),
        "created_at": now,
        "updated_at": now,
    }


def _public_template_version_result(report_id: str, version_doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report_id,
        "version": version_doc.get("version"),
        "status": version_doc.get("status") or "",
        "template_name": version_doc.get("template_name") or "",
        "template_size_bytes": version_doc.get("template_size_bytes") or 0,
        "template_sha256": version_doc.get("template_sha256") or "",
        "template_sheet_count": version_doc.get("template_sheet_count") or 0,
        "created_at": _format_dt(version_doc.get("created_at")),
        "updated_at": _format_dt(version_doc.get("updated_at")),
    }


def _build_query_mapping_version_doc(
    *,
    version: int,
    current_version: dict[str, Any],
    query_config_id: str,
    mapping_version_id: str,
    note: str,
    now: datetime,
) -> dict[str, Any]:
    copied_keys = (
        "template_name",
        "template_file_name",
        "template_gcs_uri",
        "template_size_bytes",
        "template_sha256",
        "template_sheet_count",
        "template_sheets",
    )
    version_doc = {
        key: current_version[key]
        for key in copied_keys
        if key in current_version and current_version.get(key) not in (None, "")
    }
    version_doc.update(
        {
            "version": version,
            "status": "published",
            "note": (note or "query mapping published").strip(),
            "query_config_id": query_config_id,
            "mapping_version_id": mapping_version_id,
            "created_at": now,
            "updated_at": now,
        }
    )
    return version_doc


def _public_query_mapping_version_result(report_id: str, version_doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report_id,
        "version": version_doc.get("version"),
        "status": version_doc.get("status") or "",
        "query_config_id": version_doc.get("query_config_id") or "",
        "mapping_version_id": version_doc.get("mapping_version_id") or "",
        "created_at": _format_dt(version_doc.get("created_at")),
        "updated_at": _format_dt(version_doc.get("updated_at")),
    }


def _find_report_definition_current_version(definition: dict[str, Any]) -> dict[str, Any]:
    current_version = definition.get("current_version")
    if current_version is None:
        raise ValueError("current_version is required")

    for version in definition.get("versions") or []:
        if int(version.get("version") or 0) == int(current_version):
            return dict(version)

    raise ValueError("current_version not found")


def get_report_definition_runtime_template(report_id: str) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    db = get_firestore_client()
    snap = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id).get()
    if not snap.exists:
        raise ValueError("report_id not found")

    definition = snap.to_dict() or {}
    if definition.get("archived") or definition.get("status") == "archived":
        raise ValueError("report definition is archived")

    version = _find_report_definition_current_version(definition)
    template_gcs_uri = str(version.get("template_gcs_uri") or "").strip()
    if not template_gcs_uri:
        raise ValueError("published template is required")

    bucket, object_name = parse_gcs_uri(template_gcs_uri)
    template_name = _safe_template_file_name(
        str(version.get("template_name") or version.get("template_file_name") or object_name)
    )

    return {
        "report_id": report_id,
        "version": int(version.get("version") or 0),
        "template_name": template_name,
        "template_sha256": str(version.get("template_sha256") or ""),
        "template_size_bytes": int(version.get("template_size_bytes") or 0),
        "bucket": bucket,
        "object_name": object_name,
        "template_gcs_uri": template_gcs_uri,
    }


def _public_runtime_template_result(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": template.get("report_id") or "",
        "report_definition_version": template.get("version"),
        "template_name": template.get("template_name") or "",
        "template_sha256": template.get("template_sha256") or "",
        "template_size_bytes": template.get("template_size_bytes") or 0,
    }


def download_report_definition_template(
    report_id: str,
    *,
    destination_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    template = get_report_definition_runtime_template(report_id)
    destination_root = Path(destination_dir)
    destination_root.mkdir(parents=True, exist_ok=True)

    filename = _safe_template_file_name(str(template.get("template_name") or "template.xlsx"))
    local_name = f"{template['report_id']}-v{template['version']}-{secrets.token_hex(8)}-{filename}"
    local_path = destination_root / local_name

    bucket = get_storage_client().bucket(template["bucket"])
    blob = bucket.blob(template["object_name"])
    blob.download_to_filename(str(local_path))

    return {
        "local_path": str(local_path),
        "template": _public_runtime_template_result(template),
    }


def publish_report_definition_template(
    report_id: str,
    *,
    template_bytes: bytes,
    preview: dict[str, Any],
    bucket_name: str,
    object_prefix: str = "report-templates",
    note: str = "",
) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    if not bucket_name:
        raise ValueError("template bucket is required")
    if not template_bytes:
        raise ValueError("template file is empty")

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError("report_id not found")

    current = snap.to_dict() or {}
    if current.get("archived") or current.get("status") == "archived":
        raise ValueError("report definition is archived")

    versions = list(current.get("versions") or [])
    next_version = max([int(v.get("version") or 0) for v in versions] or [0]) + 1
    file_name = _safe_template_file_name(str(preview.get("file_name") or ""))
    object_name = _template_object_name(object_prefix, report_id, next_version, file_name)
    gcs_uri = f"gs://{bucket_name}/{object_name}"
    now = utcnow()
    version_doc = _build_template_version_doc(
        version=next_version,
        preview=preview,
        gcs_uri=gcs_uri,
        note=note,
        now=now,
    )

    bucket = get_storage_client().bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_string(template_bytes, content_type=TEMPLATE_CONTENT_TYPE)

    versions.append(version_doc)
    update_doc = {
        "versions": versions,
        "current_version": next_version,
        "updated_at": now,
    }
    ref.update(update_doc)

    current.update(update_doc)
    return {
        "item": _public_report_definition(report_id, current, include_versions=True),
        "template": _public_template_version_result(report_id, version_doc),
    }


def publish_report_definition_query_mapping(
    report_id: str,
    *,
    query_config_id: str = "",
    mapping_version_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    allowlist = get_report_definition_query_mapping_allowlist()
    query_config_id = (query_config_id or DEFAULT_REPORT_ALLOWED_QUERY_CONFIG_IDS[0]).strip()
    mapping_version_id = (mapping_version_id or DEFAULT_REPORT_ALLOWED_MAPPING_VERSION_IDS[0]).strip()

    if query_config_id not in allowlist["query_config_ids"]:
        raise ValueError("query_config_id is not allowed")
    if mapping_version_id not in allowlist["mapping_version_ids"]:
        raise ValueError("mapping_version_id is not allowed")

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError("report_id not found")

    current = snap.to_dict() or {}
    if current.get("archived") or current.get("status") == "archived":
        raise ValueError("report definition is archived")

    versions = list(current.get("versions") or [])
    current_version_doc: dict[str, Any] = {}
    if versions and current.get("current_version") is not None:
        current_version_doc = _find_report_definition_current_version(current)

    next_version = max([int(v.get("version") or 0) for v in versions] or [0]) + 1
    now = utcnow()
    version_doc = _build_query_mapping_version_doc(
        version=next_version,
        current_version=current_version_doc,
        query_config_id=query_config_id,
        mapping_version_id=mapping_version_id,
        note=note,
        now=now,
    )

    versions.append(version_doc)
    update_doc = {
        "versions": versions,
        "current_version": next_version,
        "updated_at": now,
    }
    ref.update(update_doc)

    current.update(update_doc)
    return {
        "item": _public_report_definition(report_id, current, include_versions=True),
        "query_mapping": _public_query_mapping_version_result(report_id, version_doc),
    }


def rollback_report_definition_version(report_id: str, version: int) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    if int(version or 0) <= 0:
        raise ValueError("version is required")

    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id)
    snap = ref.get()
    if not snap.exists:
        raise ValueError("report_id not found")

    current = snap.to_dict() or {}
    if current.get("archived") or current.get("status") == "archived":
        raise ValueError("report definition is archived")

    target_version = int(version)
    versions = list(current.get("versions") or [])
    if not any(int(item.get("version") or 0) == target_version for item in versions):
        raise ValueError("version not found")

    update_doc = {
        "current_version": target_version,
        "updated_at": utcnow(),
    }
    ref.update(update_doc)

    current.update(update_doc)
    return _public_report_definition(report_id, current, include_versions=True)


def rollback_report_definition_template(report_id: str, version: int) -> dict[str, Any]:
    return rollback_report_definition_version(report_id, version)


def get_report_definition(report_id: str) -> dict[str, Any]:
    report_id = _validate_report_id(report_id)
    db = get_firestore_client()
    snap = db.collection(FIRESTORE_COLLECTION_REPORT_DEFINITIONS).document(report_id).get()

    if not snap.exists:
        raise ValueError("report_id not found")

    return _public_report_definition(
        snap.id,
        snap.to_dict() or {},
        include_versions=True,
    )


def list_delivery_records(
    *,
    report_month: str | None = None,
    active: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    db = get_firestore_client()
    query = db.collection(FIRESTORE_COLLECTION_DELIVERIES)

    if report_month:
        query = query.where("report_month", "==", report_month)

    if active is not None:
        query = query.where("active", "==", active)

    query = query.order_by(
        "created_at",
        direction=firestore.Query.DESCENDING,
    ).limit(limit)

    return [
        _public_delivery(doc.id, doc.to_dict(), include_versions=True)
        for doc in query.stream()
    ]


def add_delivery_version(
    *,
    delivery_id: str,
    gcs_uri: str,
    note: str | None = None,
    make_current: bool = True,
) -> dict[str, Any]:
    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_DELIVERIES).document(delivery_id)
    snap = ref.get()

    if not snap.exists:
        raise ValueError("delivery_id not found")

    delivery = snap.to_dict()
    versions = list(delivery.get("versions") or [])
    next_version = max([v.get("version", 0) for v in versions] or [0]) + 1

    bucket, object_name = parse_gcs_uri(gcs_uri)
    file_name = object_name.rsplit("/", 1)[-1]
    now = utcnow()

    version_doc = {
        "version": next_version,
        "gcs_uri": gcs_uri,
        "bucket": bucket,
        "object_name": object_name,
        "file_name": file_name,
        "created_at": now,
        "note": note or "updated",
    }

    versions.append(version_doc)

    update_doc = {
        "versions": versions,
        "updated_at": now,
    }

    if make_current:
        update_doc["current_version"] = next_version

    ref.update(update_doc)

    notify_slack_event(
        "ICEレポートのバージョンが追加されました",
        {
            "delivery_id": delivery_id,
            "customer_name": delivery.get("customer_name"),
            "report_month": delivery.get("report_month"),
            "version": next_version,
            "current_version": next_version if make_current else delivery.get("current_version"),
            "file_name": file_name,
            "gcs_uri": gcs_uri,
            "download_url": delivery.get("public_download_url"),
            "active": delivery.get("active"),
            "timestamp": now.isoformat(),
        },
    )

    return {
        "delivery_id": delivery_id,
        "version": next_version,
        "current_version": next_version if make_current else delivery.get("current_version"),
        "gcs_uri": gcs_uri,
    }


def set_delivery_active(delivery_id: str, active: bool) -> dict[str, Any]:
    db = get_firestore_client()
    ref = db.collection(FIRESTORE_COLLECTION_DELIVERIES).document(delivery_id)
    snap = ref.get()

    if not snap.exists:
        raise ValueError("delivery_id not found")

    delivery = snap.to_dict()
    now = utcnow()
    current = get_current_version(delivery)

    ref.update(
        {
            "active": active,
            "updated_at": now,
        }
    )

    notify_slack_event(
        "ICEレポート配布URLの状態が変更されました",
        {
            "delivery_id": delivery_id,
            "customer_name": delivery.get("customer_name"),
            "report_month": delivery.get("report_month"),
            "current_version": delivery.get("current_version"),
            "file_name": current.get("file_name"),
            "gcs_uri": current.get("gcs_uri"),
            "download_url": delivery.get("public_download_url"),
            "active": active,
            "timestamp": now.isoformat(),
        },
    )

    return {
        "delivery_id": delivery_id,
        "active": active,
    }


def list_download_log_records(
    *,
    delivery_id: str | None = None,
    email: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    db = get_firestore_client()
    query = db.collection(FIRESTORE_COLLECTION_DOWNLOAD_LOGS)

    if delivery_id:
        query = query.where("delivery_id", "==", delivery_id)

    if email:
        query = query.where("email", "==", normalize_email(email))

    query = query.order_by(
        "downloaded_at",
        direction=firestore.Query.DESCENDING,
    ).limit(limit)

    items = []

    for doc in query.stream():
        data = doc.to_dict()
        data["log_id"] = doc.id

        for key in ("downloaded_at",):
            data[key] = _format_dt(data.get(key))

        items.append(data)

    return items


def find_delivery_by_token(token: str) -> tuple[str | None, dict[str, Any] | None]:
    db = get_firestore_client()
    token_hash = hash_token(token)

    query = (
        db.collection(FIRESTORE_COLLECTION_DELIVERIES)
        .where("token_hash", "==", token_hash)
        .limit(1)
    )

    results = list(query.stream())

    if not results:
        return None, None

    snap = results[0]

    return snap.id, snap.to_dict()


def validate_delivery_access(delivery: dict[str, Any], email: str) -> tuple[bool, str]:
    if not delivery.get("active", False):
        return False, "この配布URLは無効です。"

    expires_at = delivery.get("expires_at")

    if expires_at and expires_at < utcnow():
        return False, "ダウンロード期限が切れています。"

    email = normalize_email(email)

    if not email or "@" not in email:
        return False, "メールアドレスが不正です。"

    allowed_emails = set(delivery.get("allowed_emails") or [])
    allowed_domains = set(delivery.get("allowed_domains") or [])

    if allowed_emails and email in allowed_emails:
        return True, "ok"

    if allowed_domains and get_email_domain(email) in allowed_domains:
        return True, "ok"

    return False, "許可されていないメールアドレスです。"


def get_current_version(delivery: dict[str, Any]) -> dict[str, Any]:
    current_version = delivery.get("current_version")

    for version in delivery.get("versions", []):
        if version.get("version") == current_version:
            return version

    raise ValueError("current_version が versions に存在しません。")


def make_signed_download_url(version: dict[str, Any]) -> str:
    storage_client = get_storage_client()
    bucket = storage_client.bucket(version["bucket"])
    blob = bucket.blob(version["object_name"])

    credentials, _ = google.auth.default()
    credentials.refresh(GoogleAuthRequest())

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=DOWNLOAD_SIGNED_URL_SECONDS),
        method="GET",
        response_disposition=f"attachment; filename=\"{version['file_name']}\"",
        service_account_email=SIGNED_URL_SERVICE_ACCOUNT,
        access_token=credentials.token,
    )


def log_download(
    *,
    delivery_id: str,
    delivery: dict[str, Any],
    version: dict[str, Any],
    email: str,
    request: Request,
) -> None:
    now = utcnow()

    payload = {
        "delivery_id": delivery_id,
        "customer_name": delivery.get("customer_name"),
        "report_month": delivery.get("report_month"),
        "version": version.get("version"),
        "gcs_uri": version.get("gcs_uri"),
        "file_name": version.get("file_name"),
        "email": normalize_email(email),
        "downloaded_at": now.isoformat(),
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "user_agent": request.headers.get("User-Agent"),
    }

    db = get_firestore_client()
    db.collection(FIRESTORE_COLLECTION_DOWNLOAD_LOGS).add(payload)

    notify_slack_event(
        "ICEレポートがダウンロードされました",
        {
            **payload,
            "download_url": delivery.get("public_download_url"),
            "timestamp": now.isoformat(),
        },
    )


def render_download_form(token: str, error: str | None = None) -> str:
    error_html = f"<p style='color:#b00020'>{error}</p>" if error else ""

    return f"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ICEレポートダウンロード</title>
</head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto; line-height: 1.6;">
  <h1>ICEレポートダウンロード</h1>
  <p>送付先として許可されたメールアドレスを入力してください。</p>
  {error_html}
  <form method="post" action="/d/{token}">
    <label>メールアドレス<br><input name="email" type="email" required style="width:100%;padding:8px;"></label>
    <p><button type="submit" style="padding:8px 16px;">ダウンロード</button></p>
  </form>
</body>
</html>
"""
