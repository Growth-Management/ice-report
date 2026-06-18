#!/usr/bin/env python
"""Read-only retention review helper for ICE Report Generator.

This script only lists deletion candidates. It never deletes Firestore records
or GCS objects, and it intentionally omits secret values, tokens, PINs, raw
recipient email addresses, message bodies, and provider payloads from output.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.cloud import firestore


DEFAULT_PROJECT = "ice-sh"
DEFAULT_BUCKET = "ice-report-files"
DEFAULT_PREFIX = "reports/plus/"
DEFAULT_DELIVERIES_COLLECTION = "deliveries"
DEFAULT_DOWNLOAD_LOGS_COLLECTION = "download_logs"
DEFAULT_SECURITY_EVENTS_COLLECTION = "security_events"
DEFAULT_ADMIN_AUDIT_LOGS_COLLECTION = "admin_audit_logs"
DEFAULT_OTP_COLLECTION = "otp_challenges"
DEFAULT_DOWNLOAD_SESSIONS_COLLECTION = "download_sessions"


@dataclass(frozen=True)
class RetentionPolicy:
    firestore_days: int = 400
    gcs_expires_days: int = 180
    gcs_report_month_months: int = 13
    ephemeral_days: int = 30


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month)


def report_month_retention_date(report_month: str | None, months: int) -> datetime | None:
    if not report_month:
        return None
    try:
        base = datetime.strptime(report_month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError:
        return None
    return add_months(base, months)


def parse_gcs_uri(gcs_uri: str | None) -> tuple[str | None, str | None]:
    if not gcs_uri or not gcs_uri.startswith("gs://"):
        return None, None
    rest = gcs_uri[5:]
    bucket, _, object_name = rest.partition("/")
    if not bucket or not object_name:
        return None, None
    return bucket, object_name


def public_delivery_summary(doc_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc_id,
        "active": bool(data.get("active")),
        "report_month": data.get("report_month"),
        "current_version": data.get("current_version"),
        "expires_at": iso(parse_datetime(data.get("expires_at"))),
        "created_at": iso(parse_datetime(data.get("created_at"))),
        "updated_at": iso(parse_datetime(data.get("updated_at"))),
        "version_count": len(data.get("versions") or []),
    }


def version_gcs_uri(version: dict[str, Any]) -> str | None:
    gcs_uri = version.get("gcs_uri")
    if gcs_uri:
        return str(gcs_uri)
    bucket = version.get("bucket")
    object_name = version.get("object_name")
    if bucket and object_name:
        return f"gs://{bucket}/{object_name}"
    return None


def current_version_uri(delivery: dict[str, Any]) -> str | None:
    current_version = delivery.get("current_version")
    for version in delivery.get("versions") or []:
        if version.get("version") == current_version:
            return version_gcs_uri(version)
    return None


def list_documents(
    db: firestore.Client,
    collection_name: str,
    *,
    limit: int,
) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for snap in db.collection(collection_name).limit(limit).stream():
        rows.append((snap.id, snap.to_dict() or {}))
    return rows


def collect_deliveries(
    db: firestore.Client,
    collection_name: str,
    *,
    limit: int,
) -> list[tuple[str, dict[str, Any]]]:
    return list_documents(db, collection_name, limit=limit)


def firestore_record_candidates(
    rows: list[tuple[str, dict[str, Any]]],
    *,
    date_fields: tuple[str, ...],
    cutoff: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for doc_id, data in rows:
        candidate_date = None
        source_field = None
        for field in date_fields:
            candidate_date = parse_datetime(data.get(field))
            if candidate_date:
                source_field = field
                break
        if candidate_date is None or candidate_date > cutoff:
            continue
        candidates.append(
            {
                "id": doc_id,
                "date_field": source_field,
                "date": iso(candidate_date),
                "age_days": (utcnow() - candidate_date).days,
                "delivery_id": data.get("delivery_id") or None,
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def delivery_record_candidates(
    deliveries: list[tuple[str, dict[str, Any]]],
    *,
    cutoff: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for doc_id, data in deliveries:
        if data.get("active") is True:
            continue
        expires_at = parse_datetime(data.get("expires_at"))
        if expires_at is None or expires_at > cutoff:
            continue
        item = public_delivery_summary(doc_id, data)
        item["age_days_after_expires_at"] = (utcnow() - expires_at).days
        candidates.append(item)
        if len(candidates) >= limit:
            break
    return candidates


def gcs_object_candidates(
    deliveries: list[tuple[str, dict[str, Any]]],
    *,
    bucket_name: str,
    prefix: str,
    policy: RetentionPolicy,
    limit: int,
) -> list[dict[str, Any]]:
    now = utcnow()
    active_referenced_uris: set[str] = set()
    for _, delivery in deliveries:
        if delivery.get("active") is True:
            for version in delivery.get("versions") or []:
                uri = version_gcs_uri(version)
                if uri:
                    active_referenced_uris.add(uri)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for doc_id, delivery in deliveries:
        expires_at = parse_datetime(delivery.get("expires_at"))
        report_month_date = report_month_retention_date(
            delivery.get("report_month"),
            policy.gcs_report_month_months,
        )
        expires_retention = (
            expires_at + timedelta(days=policy.gcs_expires_days)
            if expires_at
            else None
        )
        retention_dates = [
            item for item in (expires_retention, report_month_date) if item is not None
        ]
        if not retention_dates:
            continue
        retain_until = max(retention_dates)
        if retain_until > now:
            continue

        for version in delivery.get("versions") or []:
            uri = version_gcs_uri(version)
            if not uri or uri in seen or uri in active_referenced_uris:
                continue
            bucket, object_name = parse_gcs_uri(uri)
            if bucket != bucket_name:
                continue
            if prefix and object_name and not object_name.startswith(prefix):
                continue

            seen.add(uri)
            candidates.append(
                {
                    "gcs_uri": uri,
                    "delivery_id": doc_id,
                    "report_month": delivery.get("report_month"),
                    "delivery_active": bool(delivery.get("active")),
                    "version": version.get("version"),
                    "is_current_version": version.get("version")
                    == delivery.get("current_version"),
                    "expires_at": iso(expires_at),
                    "retain_until": iso(retain_until),
                    "protected_by_active_delivery": False,
                }
            )
            if len(candidates) >= limit:
                return candidates
    return candidates


def annotate_gcs_existence(
    candidates: list[dict[str, Any]],
    *,
    project: str,
) -> None:
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise SystemExit(
            "google-cloud-storage is required. Install project requirements "
            "before running this script."
        ) from exc

    storage_client = storage.Client(project=project)
    for item in candidates:
        bucket_name, object_name = parse_gcs_uri(item.get("gcs_uri"))
        if not bucket_name or not object_name:
            item["exists"] = None
            continue
        blob = storage_client.bucket(bucket_name).blob(object_name)
        item["exists"] = blob.exists()
        if item["exists"]:
            blob.reload()
            item["updated"] = iso(parse_datetime(blob.updated))
            item["size"] = blob.size


def build_result(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from google.cloud import firestore
    except ImportError as exc:
        raise SystemExit(
            "google-cloud-firestore is required. Install project requirements "
            "before running this script."
        ) from exc

    policy = RetentionPolicy(
        firestore_days=args.firestore_days,
        gcs_expires_days=args.gcs_expires_days,
        gcs_report_month_months=args.gcs_report_month_months,
        ephemeral_days=args.ephemeral_days,
    )
    now = utcnow()
    firestore_cutoff = now - timedelta(days=policy.firestore_days)
    ephemeral_cutoff = now - timedelta(days=policy.ephemeral_days)

    db = firestore.Client(project=args.project)
    deliveries = collect_deliveries(
        db,
        args.deliveries_collection,
        limit=args.delivery_scan_limit,
    )

    download_logs = list_documents(
        db,
        args.download_logs_collection,
        limit=args.record_scan_limit,
    )
    security_events = list_documents(
        db,
        args.security_events_collection,
        limit=args.record_scan_limit,
    )
    admin_audit_logs = list_documents(
        db,
        args.admin_audit_logs_collection,
        limit=args.record_scan_limit,
    )
    otp_challenges = list_documents(
        db,
        args.otp_collection,
        limit=args.record_scan_limit,
    )
    download_sessions = list_documents(
        db,
        args.download_sessions_collection,
        limit=args.record_scan_limit,
    )

    gcs_candidates = gcs_object_candidates(
        deliveries,
        bucket_name=args.bucket,
        prefix=args.prefix,
        policy=policy,
        limit=args.candidate_limit,
    )
    if args.check_gcs_exists and gcs_candidates:
        annotate_gcs_existence(gcs_candidates, project=args.project)

    candidates = {
        "deliveries": delivery_record_candidates(
            deliveries,
            cutoff=firestore_cutoff,
            limit=args.candidate_limit,
        ),
        "download_logs": firestore_record_candidates(
            download_logs,
            date_fields=("downloaded_at", "created_at"),
            cutoff=firestore_cutoff,
            limit=args.candidate_limit,
        ),
        "security_events": firestore_record_candidates(
            security_events,
            date_fields=("created_at",),
            cutoff=firestore_cutoff,
            limit=args.candidate_limit,
        ),
        "admin_audit_logs": firestore_record_candidates(
            admin_audit_logs,
            date_fields=("created_at",),
            cutoff=firestore_cutoff,
            limit=args.candidate_limit,
        ),
        "otp_challenges": firestore_record_candidates(
            otp_challenges,
            date_fields=("expires_at", "created_at"),
            cutoff=ephemeral_cutoff,
            limit=args.candidate_limit,
        ),
        "download_sessions": firestore_record_candidates(
            download_sessions,
            date_fields=("expires_at", "created_at"),
            cutoff=ephemeral_cutoff,
            limit=args.candidate_limit,
        ),
        "gcs_report_objects": gcs_candidates,
    }

    counts = {name: len(items) for name, items in candidates.items()}
    scanned = {
        "deliveries": len(deliveries),
        "download_logs": len(download_logs),
        "security_events": len(security_events),
        "admin_audit_logs": len(admin_audit_logs),
        "otp_challenges": len(otp_challenges),
        "download_sessions": len(download_sessions),
    }

    return {
        "generatedAt": iso(now),
        "dryRun": True,
        "project": args.project,
        "bucket": args.bucket,
        "prefix": args.prefix,
        "policy": {
            "firestoreDays": policy.firestore_days,
            "gcsExpiresDays": policy.gcs_expires_days,
            "gcsReportMonthMonths": policy.gcs_report_month_months,
            "ephemeralDays": policy.ephemeral_days,
            "firestoreCutoff": iso(firestore_cutoff),
            "ephemeralCutoff": iso(ephemeral_cutoff),
        },
        "collections": {
            "deliveries": args.deliveries_collection,
            "download_logs": args.download_logs_collection,
            "security_events": args.security_events_collection,
            "admin_audit_logs": args.admin_audit_logs_collection,
            "otp_challenges": args.otp_collection,
            "download_sessions": args.download_sessions_collection,
        },
        "scanned": scanned,
        "candidateCounts": counts,
        "candidates": candidates,
        "note": (
            "Read-only dry-run. No Firestore records or GCS objects were deleted. "
            "Output omits secret values, tokens, PINs, raw recipient email, "
            "message bodies, and provider payloads."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only retention candidate report for ICE Report Generator."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--deliveries-collection", default=DEFAULT_DELIVERIES_COLLECTION)
    parser.add_argument("--download-logs-collection", default=DEFAULT_DOWNLOAD_LOGS_COLLECTION)
    parser.add_argument("--security-events-collection", default=DEFAULT_SECURITY_EVENTS_COLLECTION)
    parser.add_argument("--admin-audit-logs-collection", default=DEFAULT_ADMIN_AUDIT_LOGS_COLLECTION)
    parser.add_argument("--otp-collection", default=DEFAULT_OTP_COLLECTION)
    parser.add_argument("--download-sessions-collection", default=DEFAULT_DOWNLOAD_SESSIONS_COLLECTION)
    parser.add_argument("--firestore-days", type=int, default=400)
    parser.add_argument("--gcs-expires-days", type=int, default=180)
    parser.add_argument("--gcs-report-month-months", type=int, default=13)
    parser.add_argument("--ephemeral-days", type=int, default=30)
    parser.add_argument("--delivery-scan-limit", type=int, default=1000)
    parser.add_argument("--record-scan-limit", type=int, default=1000)
    parser.add_argument("--candidate-limit", type=int, default=100)
    parser.add_argument(
        "--check-gcs-exists",
        action="store_true",
        help="Also check whether candidate GCS objects exist. Still read-only.",
    )
    return parser.parse_args()


def main() -> None:
    result = build_result(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False))


if __name__ == "__main__":
    main()
