import os
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

_POLLUTABLE_MODULE_PREFIXES = ("flask", "app")
for _name in list(sys.modules):
    if any(_name == prefix or _name.startswith(prefix + ".") for prefix in _POLLUTABLE_MODULE_PREFIXES):
        del sys.modules[_name]

import app as app_module  # noqa: E402
import distribution  # noqa: E402
import jumpplus_coin_ledger_report as report  # noqa: E402

NOW = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)


def _files(prefix="v1"):
    return [
        {"file_key": key, "label": label, "gcs_uri": f"gs://bucket/reports/jumpplus-coin-ledger/2026-08/{prefix}/{key}.xlsx"}
        for key, label in (("app", "App版"), ("web", "WEB版"), ("product", "話売商品一覧"))
    ]


def _plan(existing, *, files=None, idempotency_key=None):
    return distribution.plan_multi_file_delivery_write(
        existing,
        report_key="jumpplus-coin-ledger",
        report_title="ジャンプ＋コイン出納レポート",
        customer_name="集英社 少年ジャンプ＋",
        report_month="2026-08",
        files=files or _files(),
        allowed_domains=["shueisha.co.jp", "impress.co.jp"],
        expires_days=7,
        note="scheduled",
        idempotency_key=idempotency_key,
        now=NOW,
    )


class MultiFilePlanTests(unittest.TestCase):
    def test_create_builds_one_delivery_with_three_files(self):
        action, doc = _plan(None)
        self.assertEqual(action, "create")
        self.assertEqual(doc["delivery_kind"], "multi_file")
        self.assertEqual(doc["current_version"], 1)
        self.assertEqual(doc["expires_at"], NOW + timedelta(days=7))
        self.assertEqual(doc["allowed_domains"], ["impress.co.jp", "shueisha.co.jp"])
        version = doc["versions"][0]
        self.assertEqual([f["file_key"] for f in version["files"]], ["app", "web", "product"])
        self.assertEqual(version["files"][0]["bucket"], "bucket")
        self.assertEqual(version["files"][0]["file_name"], "app.xlsx")
        self.assertEqual(doc["token_hash"], distribution.hash_token(doc["token"]))
        self.assertTrue(distribution.is_multi_file_version(version))

    def test_append_adds_version_and_switches_current_keeping_url(self):
        _, doc = _plan(None)
        action, update = _plan(doc, files=_files("v2"))
        self.assertEqual(action, "append")
        self.assertEqual(update["current_version"], 2)
        self.assertEqual([v["version"] for v in update["versions"]], [1, 2])
        self.assertEqual(update["versions"][0], doc["versions"][0])
        self.assertNotIn("token", update)
        self.assertNotIn("expires_at", update)
        self.assertNotIn("active", update)

    def test_same_idempotency_key_is_a_noop(self):
        _, doc = _plan(None, idempotency_key="scheduled:2026-08")
        action, version = _plan(doc, idempotency_key="scheduled:2026-08")
        self.assertEqual(action, "noop")
        self.assertEqual(version["version"], 1)

    def test_single_file_delivery_is_never_touched(self):
        single = {"versions": [{"version": 1, "gcs_uri": "gs://b/o.xlsx"}], "current_version": 1}
        with self.assertRaises(distribution.MultiFileDeliveryError) as ctx:
            _plan(single)
        self.assertEqual(ctx.exception.code, "delivery_kind_mismatch")

    def test_invalid_or_duplicate_file_keys_are_rejected(self):
        for files in (
            [{"file_key": "app", "gcs_uri": "gs://b/a.xlsx"}, {"file_key": "app", "gcs_uri": "gs://b/b.xlsx"}],
            [{"file_key": "../x", "gcs_uri": "gs://b/a.xlsx"}],
            [],
        ):
            with self.subTest(files=files):
                with self.assertRaises(ValueError):
                    _plan(None, files=files) if files else distribution.plan_multi_file_delivery_write(
                        None, report_key="jumpplus-coin-ledger", report_title="t", customer_name="c",
                        report_month="2026-08", files=[], allowed_domains=[], expires_days=7, note="n",
                        idempotency_key=None, now=NOW,
                    )

    def test_delivery_id_is_deterministic_per_report_month(self):
        self.assertEqual(
            distribution.multi_file_delivery_id("jumpplus-coin-ledger", "2026-08"), "jumpplus-coin-ledger_2026-08"
        )
        with self.assertRaises(distribution.MultiFileDeliveryError):
            distribution.multi_file_delivery_id("jumpplus-coin-ledger", "2026-8")

    def test_legacy_single_file_version_is_not_multi_file(self):
        self.assertFalse(distribution.is_multi_file_version({"version": 1, "gcs_uri": "gs://b/o.xlsx"}))
        self.assertFalse(distribution.is_multi_file_version({"version": 1, "files": []}))


class SessionFileClaimTests(unittest.TestCase):
    keys = ["v1:app", "v1:web", "v1:product"]

    def test_each_file_once_then_session_is_used(self):
        session = {}
        for key in self.keys:
            allowed, update = app_module._plan_session_file_claim(session, claim_key=key, all_claim_keys=self.keys, now=NOW)
            self.assertTrue(allowed)
            session.update(update)
        self.assertTrue(session["used"])

    def test_first_file_does_not_consume_session(self):
        allowed, update = app_module._plan_session_file_claim({}, claim_key="v1:app", all_claim_keys=self.keys, now=NOW)
        self.assertTrue(allowed)
        self.assertNotIn("used", update)

    def test_same_file_twice_is_denied(self):
        allowed, _ = app_module._plan_session_file_claim(
            {"used_file_keys": ["v1:app"]}, claim_key="v1:app", all_claim_keys=self.keys, now=NOW
        )
        self.assertFalse(allowed)

    def test_used_session_is_denied(self):
        allowed, _ = app_module._plan_session_file_claim({"used": True}, claim_key="v1:web", all_claim_keys=self.keys, now=NOW)
        self.assertFalse(allowed)


def _delivery(multi=True):
    if multi:
        _, doc = _plan(None)
        return doc
    return {
        "active": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=3),
        "allowed_domains": ["impress.co.jp"],
        "allowed_emails": [],
        "current_version": 1,
        "versions": [{"version": 1, "gcs_uri": "gs://b/o.xlsx", "bucket": "b", "object_name": "o.xlsx", "file_name": "o.xlsx"}],
    }


class DownloadRouteTests(unittest.TestCase):
    token = "tok"

    def setUp(self):
        self.client = app_module.app.test_client()
        self.session = {"email": "user@impress.co.jp", "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
        patches = [
            mock.patch.object(app_module, "_find_download_session", return_value=("sess-1", self.session)),
            mock.patch.object(app_module, "_log_security_event"),
            mock.patch.object(app_module, "make_signed_download_url", return_value="https://signed.example/x"),
            mock.patch.object(app_module, "log_download"),
            mock.patch.object(app_module, "firestore"),
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.sign = self.mocks[2]
        self.log_download = self.mocks[3]
        self.firestore = self.mocks[4]

    def _with_delivery(self, delivery):
        p = mock.patch.object(app_module, "find_delivery_by_token", return_value=("jumpplus-coin-ledger_2026-08", delivery))
        p.start()
        self.addCleanup(p.stop)

    def test_multi_file_download_shows_list_without_consuming_session(self):
        self._with_delivery(_delivery())
        resp = self.client.get(f"/d/{self.token}/download")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        for label in ("App版", "WEB版", "話売商品一覧", "ジャンプ＋コイン出納レポート"):
            self.assertIn(label, body)
        for key in ("app", "web", "product"):
            self.assertIn(f"/d/{self.token}/download/{key}", body)
        self.sign.assert_not_called()
        self.firestore.Client.return_value.collection.return_value.document.return_value.update.assert_not_called()

    def test_legacy_single_file_download_still_redirects_and_consumes_session(self):
        self._with_delivery(_delivery(multi=False))
        resp = self.client.get(f"/d/{self.token}/download")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "https://signed.example/x")
        update = self.firestore.Client.return_value.collection.return_value.document.return_value.update
        update.assert_called_once()
        self.assertTrue(update.call_args[0][0]["used"])

    def test_each_file_is_downloaded_and_logged_individually(self):
        self._with_delivery(_delivery())
        with mock.patch.object(app_module, "_claim_download_session_file", return_value=True) as claim:
            resp = self.client.get(f"/d/{self.token}/download/web")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(claim.call_args.kwargs["claim_key"], "v1:web")
        self.assertEqual(claim.call_args.kwargs["all_claim_keys"], ["v1:app", "v1:web", "v1:product"])
        signed_entry = self.sign.call_args[0][0]
        self.assertEqual(signed_entry["file_key"], "web")
        logged_version = self.log_download.call_args.kwargs["version"]
        self.assertEqual((logged_version["file_key"], logged_version["version"]), ("web", 1))

    def test_already_downloaded_file_is_denied(self):
        self._with_delivery(_delivery())
        with mock.patch.object(app_module, "_claim_download_session_file", return_value=False):
            resp = self.client.get(f"/d/{self.token}/download/app")
        self.assertEqual(resp.status_code, 403)
        self.sign.assert_not_called()
        self.log_download.assert_not_called()

    def test_unknown_file_key_is_404(self):
        self._with_delivery(_delivery())
        resp = self.client.get(f"/d/{self.token}/download/secret")
        self.assertEqual(resp.status_code, 404)
        self.sign.assert_not_called()

    def test_file_route_requires_session(self):
        self._with_delivery(_delivery())
        with mock.patch.object(app_module, "_find_download_session", return_value=(None, None)):
            resp = self.client.get(f"/d/{self.token}/download/app")
        self.assertEqual(resp.status_code, 403)
        self.sign.assert_not_called()

    def test_file_route_on_single_file_delivery_is_404(self):
        self._with_delivery(_delivery(multi=False))
        resp = self.client.get(f"/d/{self.token}/download/app")
        self.assertEqual(resp.status_code, 404)

    def test_disallowed_email_is_denied(self):
        self._with_delivery(_delivery())
        self.session["email"] = "someone@example.com"
        resp = self.client.get(f"/d/{self.token}/download/app")
        self.assertEqual(resp.status_code, 403)
        self.sign.assert_not_called()


class _FakeDoc:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def get(self):
        data = self.store.get(self.key)
        return types.SimpleNamespace(exists=data is not None, to_dict=lambda: dict(data or {}))

    def set(self, data, merge=False):
        self.store[self.key] = {**(self.store.get(self.key) or {}), **data} if merge else dict(data)

    def update(self, data):
        self.store[self.key] = {**self.store[self.key], **data}


class _FakeFirestore:
    def __init__(self):
        self.store = {}

    def Client(self):
        fake = self

        class _Client:
            def collection(self, name):
                return types.SimpleNamespace(document=lambda doc_id: _FakeDoc(fake.store, (name, doc_id)))

        return _Client()


class ScheduledEndpointTests(unittest.TestCase):
    url = "/admin/reports/jumpplus-coin-ledger/scheduled-generate"
    run_key = ("jumpplus_coin_ledger_scheduled_runs", "2026-09")

    def setUp(self):
        self.client = app_module.app.test_client()
        self.fs = _FakeFirestore()
        patches = [
            mock.patch.object(app_module, "_check_coin_ledger_scheduler_auth", return_value=(True, "")),
            mock.patch.object(app_module, "firestore", self.fs),
            mock.patch.object(app_module, "find_multi_file_version_by_idempotency_key", return_value=None),
            mock.patch.object(report, "tokyo_today", return_value=date(2026, 10, 1)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _ok_result(self, **kwargs):
        return {
            "status": "ok",
            "report": "jumpplus-coin-ledger",
            "target_month": kwargs["target_month"].strftime("%Y-%m"),
            "trigger": kwargs["trigger"],
            "files": [{"file_key": "app", "drive_file_id": "d1", "has_gcs_object": True}],
            "delivery": {"delivery_id": "jumpplus-coin-ledger_2026-09", "version": 1, "public_download_url": "https://x/d/secret"},
        }

    def test_requires_scheduler_oidc(self):
        with mock.patch.object(app_module, "_check_coin_ledger_scheduler_auth", return_value=(False, "invalid_oidc_token")):
            resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 401)

    def test_source_not_ready_then_retry_succeeds_once(self):
        not_ready = report.ReadinessResult(ready=False, reasons=["content_platform_missing"])
        ready = report.ReadinessResult(ready=True)
        generate = mock.Mock(side_effect=lambda **kw: self._ok_result(**kw))
        with mock.patch.object(report, "check_source_readiness", side_effect=[not_ready, ready]), mock.patch.object(
            report, "generate_coin_ledger_report", generate
        ):
            first = self.client.post(self.url, json={})
            self.assertEqual(first.status_code, 503)
            self.assertEqual(first.get_json()["error"], "source_not_ready")
            self.assertTrue(first.get_json()["retryable"])
            self.assertEqual(self.fs.store[self.run_key]["status"], "source_not_ready")
            generate.assert_not_called()

            second = self.client.post(self.url, json={})
        self.assertEqual(second.status_code, 200)
        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["target_month"], date(2026, 9, 1))
        self.assertEqual(generate.call_args.kwargs["trigger"], "scheduled")
        self.assertEqual(self.fs.store[self.run_key]["status"], "succeeded")
        self.assertEqual(self.fs.store[self.run_key]["attempt_count"], 2)
        self.assertNotIn("public_download_url", second.get_data(as_text=True))

    def test_retry_after_success_is_skipped_without_generating(self):
        self.fs.store[self.run_key] = {"status": "succeeded"}
        with mock.patch.object(report, "generate_coin_ledger_report") as generate, mock.patch.object(
            report, "check_source_readiness"
        ) as readiness:
            resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["reason"], "already_generated")
        generate.assert_not_called()
        readiness.assert_not_called()

    def test_succeeded_run_with_scheduled_version_skips_without_new_version(self):
        # State after the first production run (2026-09): run succeeded and the
        # delivery already holds version 1 with idempotency_key scheduled:2026-09.
        self.fs.store[self.run_key] = {"status": "succeeded"}
        before = dict(self.fs.store)
        with mock.patch.object(app_module, "_coin_ledger_deliver") as deliver, mock.patch.object(
            app_module, "find_multi_file_version_by_idempotency_key", return_value={"version": 1}
        ) as find, mock.patch.object(report, "generate_coin_ledger_report") as generate:
            resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual((body["status"], body["reason"]), ("skipped", "already_generated"))
        deliver.assert_not_called()
        generate.assert_not_called()
        find.assert_not_called()
        self.assertEqual(self.fs.store, before)

    def test_existing_scheduled_version_is_not_delivered_twice(self):
        with mock.patch.object(app_module, "find_multi_file_version_by_idempotency_key", return_value={"version": 1}), mock.patch.object(
            report, "generate_coin_ledger_report"
        ) as generate:
            resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["reason"], "already_delivered")
        self.assertEqual(self.fs.store[self.run_key]["status"], "succeeded")
        generate.assert_not_called()

    def test_concurrent_running_is_409(self):
        self.fs.store[self.run_key] = {"status": "running", "updated_at": datetime.now(timezone.utc)}
        with mock.patch.object(report, "check_source_readiness", return_value=report.ReadinessResult(ready=True)), mock.patch.object(
            report, "generate_coin_ledger_report"
        ) as generate:
            resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 409)
        generate.assert_not_called()

    def test_stale_running_record_is_reclaimed(self):
        self.fs.store[self.run_key] = {"status": "running", "updated_at": datetime.now(timezone.utc) - timedelta(hours=2)}
        with mock.patch.object(report, "check_source_readiness", return_value=report.ReadinessResult(ready=True)), mock.patch.object(
            report, "generate_coin_ledger_report", side_effect=lambda **kw: self._ok_result(**kw)
        ):
            resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 200)

    def test_generation_failure_is_retryable_failed(self):
        with mock.patch.object(report, "check_source_readiness", return_value=report.ReadinessResult(ready=True)), mock.patch.object(
            report, "generate_coin_ledger_report", side_effect=report.CoinLedgerReportError("drive_upload_failed", status_code=502)
        ):
            resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.fs.store[self.run_key]["status"], "failed")
        with mock.patch.object(report, "check_source_readiness", return_value=report.ReadinessResult(ready=True)), mock.patch.object(
            report, "generate_coin_ledger_report", side_effect=lambda **kw: self._ok_result(**kw)
        ):
            retry = self.client.post(self.url, json={})
        self.assertEqual(retry.status_code, 200)

    def test_scheduled_delivery_uses_month_idempotency_key(self):
        captured = {}

        def fake_generate(**kw):
            captured["deliver"] = kw["deliver"]
            return self._ok_result(**kw)

        with mock.patch.object(report, "check_source_readiness", return_value=report.ReadinessResult(ready=True)), mock.patch.object(
            report, "generate_coin_ledger_report", side_effect=fake_generate
        ), mock.patch.object(app_module, "upsert_multi_file_delivery", return_value={}) as upsert:
            self.client.post(self.url, json={})
            files = [report.GeneratedFile(s.file_key, s.label, "f.xlsx", None, {}, gcs_uri=f"gs://b/{s.file_key}") for s in report.OUTPUT_FILES]
            captured["deliver"](files)
        kwargs = upsert.call_args.kwargs
        self.assertEqual(kwargs["idempotency_key"], "scheduled:2026-09")
        self.assertEqual(kwargs["report_month"], "2026-09")
        self.assertEqual(kwargs["allowed_domains"], list(report.DEFAULT_ALLOWED_DOMAINS))
        self.assertEqual([f["file_key"] for f in kwargs["files"]], ["app", "web", "product"])


class SchedulerOidcTests(unittest.TestCase):
    url = "/admin/reports/jumpplus-coin-ledger/scheduled-generate"

    def test_oidc_uses_dedicated_env_prefix(self):
        with mock.patch.object(app_module, "_check_scheduler_oidc_auth", return_value=(True, "")) as check:
            app_module._check_coin_ledger_scheduler_auth()
        self.assertEqual(check.call_args.kwargs["env_prefix"], "JUMPPLUS_COIN_LEDGER_SCHEDULER")

    def test_fail_closed_when_not_configured_or_no_bearer(self):
        client = app_module.app.test_client()
        with mock.patch.dict(os.environ, {"JUMPPLUS_COIN_LEDGER_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS": ""}):
            self.assertEqual(client.post(self.url, json={}).status_code, 401)
        with mock.patch.dict(
            os.environ,
            {"JUMPPLUS_COIN_LEDGER_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS": "jumpplus-coin-ledger-scheduler@ice-sh.iam.gserviceaccount.com"},
        ):
            resp = client.post(self.url, json={})
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(resp.get_json()["reason"], "missing_bearer_token")
            with mock.patch.object(
                app_module, "_verify_scheduler_oidc_token", return_value={"email": "other@ice-sh.iam.gserviceaccount.com"}
            ):
                resp = client.post(self.url, json={}, headers={"Authorization": "Bearer x"})
            self.assertEqual(resp.get_json()["reason"], "service_account_not_allowed")


class ClaimScheduledRunCompatibilityTests(unittest.TestCase):
    def test_default_behavior_unchanged_existing_record_blocks(self):
        fs = _FakeFirestore()
        fs.store[("c", "r")] = {"status": "failed"}
        with mock.patch.object(app_module, "firestore", fs):
            claimed, status = app_module._claim_scheduled_run(collection_name="c", run_id="r", initial_fields={})
        self.assertFalse(claimed)
        self.assertEqual(status, "failed")

    def test_new_claim_payload_unchanged(self):
        fs = _FakeFirestore()
        with mock.patch.object(app_module, "firestore", fs):
            claimed, _ = app_module._claim_scheduled_run(collection_name="c", run_id="r", initial_fields={"report": "x"})
        self.assertTrue(claimed)
        self.assertEqual(set(fs.store[("c", "r")]), {"report", "status", "created_at", "updated_at"})


class ManualEndpointTests(unittest.TestCase):
    url = "/admin/reports/jumpplus-coin-ledger/generate"

    def setUp(self):
        self.client = app_module.app.test_client()
        patches = [
            mock.patch.object(app_module, "_check_admin", return_value=(True, None)),
            mock.patch.object(app_module, "_log_admin_audit_event"),
            mock.patch.object(report, "tokyo_today", return_value=date(2026, 9, 28)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_target_month_is_required(self):
        resp = self.client.post(self.url, json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "target_month_required")

    def test_explicit_past_month_with_delivery(self):
        result = {"status": "ok", "target_month": "2026-08", "files": [], "delivery": {"delivery_id": "d", "version": 2, "public_download_url": "https://x/d/t"}}
        with mock.patch.object(report, "generate_coin_ledger_report", return_value=result) as generate:
            resp = self.client.post(self.url, json={"target_month": "2026-08"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(generate.call_args.kwargs["target_month"], date(2026, 8, 1))
        self.assertEqual(generate.call_args.kwargs["trigger"], "manual")
        self.assertIsNotNone(generate.call_args.kwargs["deliver"])
        self.assertEqual(resp.get_json()["delivery"]["public_download_url"], "https://x/d/t")

    def test_generation_without_delivery(self):
        with mock.patch.object(report, "generate_coin_ledger_report", return_value={"files": []}) as generate:
            resp = self.client.post(self.url, json={"target_month": "2026-08", "create_delivery": False})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(generate.call_args.kwargs["deliver"])

    def test_source_not_ready_is_retryable_503(self):
        exc = report.CoinLedgerReportError("source_not_ready", status_code=503, retryable=True, reasons=["product_master_empty"])
        with mock.patch.object(report, "generate_coin_ledger_report", side_effect=exc):
            resp = self.client.post(self.url, json={"target_month": "2026-08"})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {"error": "source_not_ready", "retryable": True, "reasons": ["product_master_empty"]})


class VersionsEndpointGuardTests(unittest.TestCase):
    def test_single_file_versions_endpoint_rejects_multi_file_delivery(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_check_admin", return_value=(True, None)), mock.patch.object(
            app_module, "_log_admin_audit_event"
        ), mock.patch.object(app_module, "_is_multi_file_delivery", return_value=True):
            resp = client.post("/deliveries/jumpplus-coin-ledger_2026-08/versions", json={})
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
