#!/usr/bin/env python
"""Delete approved Firestore retention records with strict safeguards.

The default mode is dry-run. Execution requires an approval manifest, an
explicit confirmation phrase, and collection allowlist validation. Output never
includes Firestore document contents, secret values, tokens, PINs, raw recipient
emails, message bodies, or provider payloads.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.cloud import firestore


DEFAULT_PROJECT = "ice-sh"
CONFIRMATION_PHRASE = "DELETE_FIRESTORE_RETENTION_RECORDS"
ALLOWED_COLLECTIONS = frozenset(
    {
        "deliveries",
        "download_logs",
        "security_events",
        "admin_audit_logs",
        "otp_challenges",
        "download_sessions",
    }
)
HIGH_RISK_COLLECTIONS = frozenset({"deliveries"})


@dataclass(frozen=True)
class DeletionRecord:
    collection: str
    document_id: str
    reason: str


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise SystemExit(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("Manifest must be a JSON object.")
    return value


def require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"Manifest field '{field_name}' is required.")
    return value.strip()


def validate_path_part(value: str, field_name: str) -> str:
    text = require_text(value, field_name)
    if "/" in text or "\\" in text:
        raise SystemExit(f"Manifest field '{field_name}' must not contain path separators.")
    if text in (".", ".."):
        raise SystemExit(f"Manifest field '{field_name}' is invalid.")
    return text


def parse_records(manifest: dict[str, Any]) -> list[DeletionRecord]:
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise SystemExit("Manifest field 'records' must be a non-empty array.")

    parsed: list[DeletionRecord] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise SystemExit(f"Manifest records[{index}] must be an object.")

        collection = validate_path_part(item.get("collection"), f"records[{index}].collection")
        document_id = validate_path_part(item.get("id"), f"records[{index}].id")
        reason = require_text(item.get("reason"), f"records[{index}].reason")

        if collection not in ALLOWED_COLLECTIONS:
            allowed = ", ".join(sorted(ALLOWED_COLLECTIONS))
            raise SystemExit(
                f"Collection '{collection}' is not allowed. Allowed collections: {allowed}"
            )

        key = (collection, document_id)
        if key in seen:
            raise SystemExit(f"Duplicate deletion target: {collection}/{document_id}")
        seen.add(key)
        parsed.append(DeletionRecord(collection=collection, document_id=document_id, reason=reason))

    return parsed


def validate_manifest(
    manifest: dict[str, Any],
    *,
    allow_deliveries: bool,
    max_records: int,
) -> list[DeletionRecord]:
    for field_name in ("approvalId", "approvedBy", "approvedAt", "reason"):
        require_text(manifest.get(field_name), field_name)

    records = parse_records(manifest)
    if len(records) > max_records:
        raise SystemExit(f"Manifest contains {len(records)} records; max is {max_records}.")

    high_risk = sorted({item.collection for item in records if item.collection in HIGH_RISK_COLLECTIONS})
    if high_risk and not allow_deliveries:
        names = ", ".join(high_risk)
        raise SystemExit(
            f"High-risk collection(s) require --allow-deliveries before deletion: {names}"
        )

    return records


def inspect_targets(
    db: firestore.Client,
    records: list[DeletionRecord],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for record in records:
        ref = db.collection(record.collection).document(record.document_id)
        snapshot = ref.get()
        exists = bool(getattr(snapshot, "exists", False))
        targets.append(
            {
                "collection": record.collection,
                "id": record.document_id,
                "exists": exists,
                "reason": record.reason,
            }
        )
    return targets


def delete_targets(
    db: firestore.Client,
    records: list[DeletionRecord],
    *,
    batch_size: int,
) -> int:
    deleted = 0
    batch = db.batch()
    batch_count = 0

    for record in records:
        ref = db.collection(record.collection).document(record.document_id)
        batch.delete(ref)
        batch_count += 1
        deleted += 1
        if batch_count >= batch_size:
            batch.commit()
            batch = db.batch()
            batch_count = 0

    if batch_count:
        batch.commit()

    return deleted


def build_result(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    records = validate_manifest(
        manifest,
        allow_deliveries=args.allow_deliveries,
        max_records=args.max_records,
    )

    execute = bool(args.execute)
    if execute and args.confirm != CONFIRMATION_PHRASE:
        raise SystemExit(
            f"Execution requires --confirm {CONFIRMATION_PHRASE!r}. "
            "Run without --execute for dry-run."
        )

    try:
        from google.cloud import firestore
    except ImportError as exc:
        raise SystemExit(
            "google-cloud-firestore is required. Install project requirements "
            "before running this script."
        ) from exc

    db = firestore.Client(project=args.project)
    targets = inspect_targets(db, records)
    missing = [item for item in targets if not item["exists"]]
    if execute and missing and not args.allow_missing:
        missing_targets = ", ".join(f"{item['collection']}/{item['id']}" for item in missing)
        raise SystemExit(
            "Execution blocked because manifest contains missing document(s): "
            f"{missing_targets}. Use --allow-missing only after confirming this is expected."
        )

    deleted = 0
    if execute:
        existing_records = [
            item
            for item, target in zip(records, targets, strict=True)
            if target["exists"]
        ]
        deleted = delete_targets(db, existing_records, batch_size=args.batch_size)

    return {
        "generatedAt": utcnow_iso(),
        "project": args.project,
        "dryRun": not execute,
        "executed": execute,
        "manifest": {
            "path": str(manifest_path),
            "approvalId": manifest.get("approvalId"),
            "approvedBy": manifest.get("approvedBy"),
            "approvedAt": manifest.get("approvedAt"),
            "reason": manifest.get("reason"),
        },
        "recordCount": len(records),
        "existingCount": sum(1 for item in targets if item["exists"]),
        "missingCount": len(missing),
        "deletedCount": deleted,
        "targets": targets,
        "note": (
            "Output intentionally omits Firestore document contents, secret values, "
            "tokens, PINs, raw recipient email, message bodies, and provider payloads."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete approved Firestore retention records for ICE Report Generator."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--manifest", required=True, help="Path to approval manifest JSON.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete approved Firestore records. Omit for dry-run.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required with --execute. Must be {CONFIRMATION_PHRASE!r}.",
    )
    parser.add_argument(
        "--allow-deliveries",
        action="store_true",
        help="Allow high-risk deletion from the deliveries collection.",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Allow execute mode to continue when a manifest target is already missing.",
    )
    parser.add_argument("--max-records", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    if args.max_records < 1:
        raise SystemExit("--max-records must be at least 1.")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1.")
    return args


def main() -> None:
    result = build_result(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False))


if __name__ == "__main__":
    main()
