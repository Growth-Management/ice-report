import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-retention-candidates.py"
_spec = importlib.util.spec_from_file_location("check_retention_candidates", _SCRIPT)
retention = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = retention
_spec.loader.exec_module(retention)


class VersionGcsUrisTests(unittest.TestCase):
    def test_single_file_version_unchanged(self):
        self.assertEqual(retention.version_gcs_uris({"gcs_uri": "gs://b/reports/plus/a.xlsx"}), ["gs://b/reports/plus/a.xlsx"])

    def test_multi_file_version_lists_every_file(self):
        version = {
            "version": 1,
            "files": [
                {"file_key": "app", "gcs_uri": "gs://b/reports/jumpplus-coin-ledger/2026-08/r/app.xlsx"},
                {"file_key": "web", "bucket": "b", "object_name": "reports/jumpplus-coin-ledger/2026-08/r/web.xlsx"},
            ],
        }
        self.assertEqual(
            retention.version_gcs_uris(version),
            [
                "gs://b/reports/jumpplus-coin-ledger/2026-08/r/app.xlsx",
                "gs://b/reports/jumpplus-coin-ledger/2026-08/r/web.xlsx",
            ],
        )

    def test_active_multi_file_delivery_protects_its_objects(self):
        uri = "gs://b/reports/jumpplus-coin-ledger/2020-01/r/app.xlsx"
        old = datetime.now(timezone.utc) - timedelta(days=3650)
        expired = {"active": False, "report_month": "2020-01", "expires_at": old, "versions": [{"version": 1, "files": [{"gcs_uri": uri}]}]}
        active = {"active": True, "report_month": "2020-01", "expires_at": old, "versions": [{"version": 1, "files": [{"gcs_uri": uri}]}]}
        policy = retention.RetentionPolicy()
        candidates = retention.gcs_object_candidates(
            [("expired", expired)], bucket_name="b", prefix="", policy=policy, limit=10
        )
        self.assertEqual([c["gcs_uri"] for c in candidates], [uri])
        protected = retention.gcs_object_candidates(
            [("expired", expired), ("active", active)], bucket_name="b", prefix="", policy=policy, limit=10
        )
        self.assertEqual(protected, [])


if __name__ == "__main__":
    unittest.main()
