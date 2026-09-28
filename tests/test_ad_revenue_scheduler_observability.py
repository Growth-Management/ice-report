"""Covers the three ICE_REPORT_AD_REVENUE_* log lines that were silently
dropped in Production: the app's root logger has no explicit level
configured, so plain logging.info() calls never reach Cloud Logging (only
WARNING+ does). These specific lines are the minimum Scheduler-operational
signals called for (readiness waiting/ready, scheduled-generation skipped,
sync completed) -- see docs/jumpplus-ad-revenue-report.md. This does not
touch report business logic, BigQuery queries, aggregation, or Drive
upload/transport behavior.
"""

import os
import sys
import types
import unittest
from unittest import mock

_POLLUTABLE_MODULE_PREFIXES = ("flask", "app")
for _name in list(sys.modules):
    if any(_name == prefix or _name.startswith(prefix + ".") for prefix in _POLLUTABLE_MODULE_PREFIXES):
        del sys.modules[_name]

import app as app_module  # noqa: E402
import jumpplus_ad_revenue_report as ad_revenue_module  # noqa: E402


class _FakeDocRef:
    def __init__(self, *, exists=False, data=None):
        self._exists = exists
        self._data = data or {}
        self.set_calls = []
        self.update_calls = []

    def get(self):
        return types.SimpleNamespace(exists=self._exists, to_dict=lambda: dict(self._data))

    def set(self, data):
        self.set_calls.append(data)

    def update(self, data):
        self.update_calls.append(data)


class _FakeCollection:
    def __init__(self, doc_ref):
        self._doc_ref = doc_ref

    def document(self, run_id):
        return self._doc_ref


class _FakeFirestoreClient:
    def __init__(self, doc_ref):
        self._doc_ref = doc_ref

    def collection(self, name):
        return _FakeCollection(self._doc_ref)


class AdRevenueSchedulerObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_readiness_waiting_is_logged_at_warning_level(self):
        doc_ref = _FakeDocRef(exists=False)
        fake_firestore = types.SimpleNamespace(Client=lambda: _FakeFirestoreClient(doc_ref))

        with mock.patch.object(
            app_module, "_check_ad_revenue_scheduler_auth", return_value=(True, "")
        ), mock.patch.object(app_module, "firestore", fake_firestore), mock.patch.object(
            ad_revenue_module,
            "check_readiness",
            return_value=ad_revenue_module.ReadinessResult(status="waiting_detail"),
        ):
            with self.assertLogs(level="WARNING") as logs:
                resp = self.client.post(
                    "/admin/reports/jumpplus-ad-revenue/video-reward/scheduled-generate",
                    json={"target_month": "2026-08-01"},
                )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "waiting")
        joined = "\n".join(logs.output)
        self.assertIn("ICE_REPORT_AD_REVENUE_READINESS", joined)
        self.assertIn("status=waiting_detail", joined)
        self.assertFalse(doc_ref.set_calls, "a readiness-waiting poll must not claim a Firestore run")

    def test_readiness_ready_is_logged_at_warning_level(self):
        doc_ref = _FakeDocRef(exists=False)
        fake_firestore = types.SimpleNamespace(Client=lambda: _FakeFirestoreClient(doc_ref))
        fake_result = {
            "status": "ok",
            "report": "video-reward",
            "target_month": "2026-08-01",
            "generated_date": "2026-09-28",
            "file_id": "f1",
            "file_name": "n.xlsx",
            "webViewLink": "https://drive.google.com/x",
            "detail_row_count": 10,
            "revenue_yen": 100,
        }

        with mock.patch.object(
            app_module, "_check_ad_revenue_scheduler_auth", return_value=(True, "")
        ), mock.patch.object(app_module, "firestore", fake_firestore), mock.patch.object(
            ad_revenue_module,
            "check_readiness",
            return_value=ad_revenue_module.ReadinessResult(status="ready", revenue_yen=100),
        ), mock.patch.object(
            ad_revenue_module, "generate_ad_revenue_report", return_value=fake_result
        ):
            with self.assertLogs(level="WARNING") as logs:
                resp = self.client.post(
                    "/admin/reports/jumpplus-ad-revenue/video-reward/scheduled-generate",
                    json={"target_month": "2026-08-01"},
                )

        self.assertEqual(resp.status_code, 200)
        joined = "\n".join(logs.output)
        self.assertIn("ICE_REPORT_AD_REVENUE_READINESS", joined)
        self.assertIn("status=ready", joined)
        self.assertIn("ICE_REPORT_AD_REVENUE_SCHEDULE_COMPLETED", joined)

    def test_already_generated_skip_is_logged_at_warning_level(self):
        doc_ref = _FakeDocRef(exists=True, data={"status": "succeeded"})
        fake_firestore = types.SimpleNamespace(Client=lambda: _FakeFirestoreClient(doc_ref))

        with mock.patch.object(
            app_module, "_check_ad_revenue_scheduler_auth", return_value=(True, "")
        ), mock.patch.object(app_module, "firestore", fake_firestore):
            with self.assertLogs(level="WARNING") as logs:
                resp = self.client.post(
                    "/admin/reports/jumpplus-ad-revenue/video-reward/scheduled-generate",
                    json={"target_month": "2026-08-01"},
                )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "skipped")
        self.assertEqual(resp.get_json()["reason"], "already_generated")
        joined = "\n".join(logs.output)
        self.assertIn("ICE_REPORT_AD_REVENUE_SCHEDULE_SKIPPED", joined)
        self.assertIn("reason=already_generated", joined)

    def test_sync_completed_is_logged_at_warning_level(self):
        with mock.patch.object(
            app_module, "_check_ad_revenue_sync_scheduler_auth", return_value=(True, "")
        ), mock.patch.object(
            ad_revenue_module, "sync_ad_revenue_confirmed_values", return_value={"row_count": 3}
        ):
            with self.assertLogs(level="WARNING") as logs:
                resp = self.client.post("/admin/ad-revenue/scheduled-sync", json={})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["row_count"], 3)
        joined = "\n".join(logs.output)
        self.assertIn("ICE_REPORT_AD_REVENUE_SYNC_SCHEDULE_COMPLETED", joined)
        self.assertIn("row_count=3", joined)

    def test_sync_failure_still_logged_at_error_level_unchanged(self):
        with mock.patch.object(
            app_module, "_check_ad_revenue_sync_scheduler_auth", return_value=(True, "")
        ), mock.patch.object(
            ad_revenue_module,
            "sync_ad_revenue_confirmed_values",
            side_effect=ad_revenue_module.AdRevenueReportError("sync_failed", status_code=500),
        ):
            with self.assertLogs(level="ERROR") as logs:
                resp = self.client.post("/admin/ad-revenue/scheduled-sync", json={})

        self.assertEqual(resp.status_code, 500)
        joined = "\n".join(logs.output)
        self.assertIn("ICE_REPORT_AD_REVENUE_SYNC_SCHEDULE_FAILED", joined)


if __name__ == "__main__":
    unittest.main()
