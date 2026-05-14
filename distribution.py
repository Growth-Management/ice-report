from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import google.auth
import requests
from flask import Request
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore, secretmanager, storage

FIRESTORE_COLLECTION_DELIVERIES = os.environ.get("DELIVERIES_COLLECTION", "deliveries")
FIRESTORE_COLLECTION_DOWNLOAD_LOGS = os.environ.get("DOWNLOAD_LOGS_COLLECTION", "download_logs")

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