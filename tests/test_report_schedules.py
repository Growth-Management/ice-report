"""Schedules (common registry / live lookup / drift / Admin API / Admin UI).

CASE numbering follows the Schedules requirement's test list:
1 expected/live match, 2 cron drift, 3 timezone drift, 4 missing job,
5 permission/API error, 6 safe response, 7 stable sort, 8 report_definitions
integration, 9 coin ledger registry entry, 10 existing tabs regression.
"""

import json
import os
import re
import sys
import unittest
from datetime import date
from unittest import mock

_POLLUTABLE_MODULE_PREFIXES = ("flask", "app")
for _name in list(sys.modules):
    if any(_name == prefix or _name.startswith(prefix + ".") for prefix in _POLLUTABLE_MODULE_PREFIXES):
        del sys.modules[_name]

import app as app_module  # noqa: E402
import report_schedules as rs  # noqa: E402

BASE = rs.DEFAULT_TARGET_BASE_URL


def _raw_job(spec, **overrides):
    """A job shaped like Cloud Scheduler v1 `jobs.list` output, matching spec."""
    audience = rs.expected_audience(spec, BASE)
    job = {
        "name": f"projects/ice-sh/locations/asia-northeast1/jobs/{spec.scheduler_job_name}",
        "schedule": spec.expected_schedule or "0 3 * * *",
        "timeZone": spec.timezone,
        "state": "ENABLED",
        "httpTarget": {
            "uri": BASE + spec.endpoint,
            "httpMethod": "POST",
            "headers": {"X-Admin-Key": "super-secret-admin-key"},
            "body": "c2VjcmV0",
            "oidcToken": {"serviceAccountEmail": "sa@ice-sh.iam.gserviceaccount.com", **({"audience": audience} if audience else {})},
        },
        "lastAttemptTime": "2026-09-02T00:00:01Z",
        "scheduleTime": "2026-10-02T00:00:00Z",
        "status": {"code": 0, "message": "detail that must not leak"},
    }
    if spec.expected_attempt_deadline is not None:
        job["attemptDeadline"] = spec.expected_attempt_deadline
    if spec.expected_retry is not None:
        r = spec.expected_retry
        job["retryConfig"] = {
            "retryCount": r.max_retry_attempts,  # real Cloud Scheduler API v1 shape
            "maxRetryDuration": r.max_retry_duration,
            "minBackoffDuration": r.min_backoff,
            "maxBackoffDuration": r.max_backoff,
            "maxDoublings": r.max_doublings,
        }
    for key, value in overrides.items():
        if key in ("uri", "audience"):
            if key == "uri":
                job["httpTarget"]["uri"] = value
            else:
                job["httpTarget"]["oidcToken"]["audience"] = value
        else:
            job[key] = value
    return job


def _all_jobs(**per_job_overrides):
    return [_raw_job(spec, **per_job_overrides.get(spec.id, {})) for spec in rs.REPORT_SCHEDULE_SPECS]


def _spec(spec_id):
    return next(s for s in rs.REPORT_SCHEDULE_SPECS if s.id == spec_id)


def _view(jobs=None, *, definitions=None, error=None, definitions_error=""):
    def list_jobs(project, location):
        if error:
            raise error
        return jobs if jobs is not None else _all_jobs()

    live = rs.live_lookup(list_jobs=list_jobs)
    return rs.build_schedules_view(live=live, definitions=definitions or [], definitions_error=definitions_error)


def _row(view, row_id):
    return next(r for r in view["items"] if r["id"] == row_id)


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__("Request had insufficient authentication scopes. token=abc.def.ghi")
        self.resp = mock.Mock(status=status)


class HumanReadableScheduleTests(unittest.TestCase):
    def test_cron_shapes_in_use(self):
        self.assertEqual(rs.human_readable_schedule("0 7 1 * *"), "毎月1日 07:00")
        self.assertEqual(rs.human_readable_schedule("0 9 2 * *"), "毎月2日 09:00")
        self.assertEqual(rs.human_readable_schedule("5 8 * * *"), "毎日 08:05")
        self.assertEqual(rs.human_readable_schedule("*/30 * * * *"), "30分おき")
        self.assertEqual(rs.human_readable_schedule("0 3 * * *"), "毎日 03:00")

    def test_unknown_forms_fall_back_to_raw_cron(self):
        for cron in ("0 9 * * 1", "0 9 1-5 * *", "@daily", "0 9 1 1 *"):
            self.assertEqual(rs.human_readable_schedule(cron), cron)
        self.assertEqual(rs.human_readable_schedule(None), "-")


class DriftCaseTests(unittest.TestCase):
    def test_case1_expected_live_match(self):
        view = _view()
        self.assertEqual(view["live_lookup"]["status"], "ok")
        for spec in rs.REPORT_SCHEDULE_SPECS:
            row = _row(view, spec.id)
            expected = rs.DRIFT_OK if spec.expected_schedule else rs.DRIFT_NO_EXPECTED
            self.assertEqual(row["drift"], expected, spec.id)
            self.assertEqual(row["state"], "ENABLED")

    def test_case2_cron_drift(self):
        view = _view(_all_jobs(**{"thermae-romae": {"schedule": "0 10 2 * *"}}))
        row = _row(view, "thermae-romae")
        self.assertEqual(row["drift"], rs.DRIFT_CONFIG)
        self.assertEqual(row["drift_fields"], ["cron"])
        self.assertEqual(row["actual"]["cron"], "0 10 2 * *")
        self.assertEqual(row["expected"]["cron"], "0 9 2 * *")

    def test_case3_timezone_drift(self):
        view = _view(_all_jobs(**{"plus-browser-point-sales": {"timeZone": "Etc/UTC"}}))
        row = _row(view, "plus-browser-point-sales")
        self.assertEqual((row["drift"], row["drift_fields"]), (rs.DRIFT_CONFIG, ["timezone"]))

    def test_endpoint_and_audience_drift(self):
        spec = _spec("jumpplus-ad-revenue-web")
        view = _view(
            _all_jobs(
                **{
                    spec.id: {"uri": BASE + "/admin/reports/jumpplus-ad-revenue/app2/scheduled-generate"},
                    "thermae-romae": {"audience": BASE},
                    "ad-revenue-sync": {"uri": "https://other.example.run.app/admin/ad-revenue/scheduled-sync"},
                }
            )
        )
        self.assertEqual(_row(view, spec.id)["drift_fields"], ["endpoint"])
        self.assertEqual(_row(view, "thermae-romae")["drift_fields"], ["audience"])
        self.assertEqual(_row(view, "ad-revenue-sync")["drift_fields"], ["endpoint"])

    def test_case4_missing_job_is_not_created(self):
        jobs = [j for j in _all_jobs() if not j["name"].endswith("/jumpplus-coin-ledger-monthly-report")]
        row = _row(_view(jobs), "jumpplus-coin-ledger")
        self.assertEqual((row["drift"], row["state"]), (rs.DRIFT_NOT_CREATED, "NOT_CREATED"))
        self.assertIsNone(row["actual"])
        self.assertEqual(row["expected"]["cron"], "0 7 1 * *")

    def test_case5_permission_denied_and_api_error_degrade_to_unknown(self):
        for exc, reason in ((_HttpError(403), "permission_denied"), (_HttpError(500), "api_error_500"), (RuntimeError("boom"), "lookup_failed")):
            with self.subTest(reason=reason):
                view = _view(error=exc)
                self.assertEqual(view["live_lookup"], {**view["live_lookup"], "status": "unavailable", "reason": reason})
                for spec in rs.REPORT_SCHEDULE_SPECS:
                    row = _row(view, spec.id)
                    self.assertEqual((row["state"], row["drift"]), ("UNKNOWN", rs.DRIFT_UNKNOWN))
                    self.assertEqual(row["expected"]["cron"], spec.expected_schedule)
                self.assertNotIn("token=", json.dumps(view))
                self.assertNotIn("insufficient", json.dumps(view))

    def test_live_lookup_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"REPORT_SCHEDULES_LIVE_LOOKUP": "0"}):
            live = rs.live_lookup(list_jobs=lambda p, l: self.fail("must not call API"))
        self.assertEqual((live.status, live.reason), ("unavailable", "disabled"))

    def test_undocumented_expected_is_not_guessed(self):
        view = _view()
        for spec_id in ("report-definitions-schedule-runs", "ice-report-cleanup"):
            row = _row(view, spec_id)
            self.assertEqual(row["drift"], rs.DRIFT_NO_EXPECTED)
            self.assertIsNone(row["expected"]["cron"])
            self.assertIsNotNone(row["actual"]["cron"])

    def test_unregistered_job_on_this_service_is_surfaced(self):
        extra = {
            "name": "projects/ice-sh/locations/asia-northeast1/jobs/new-report-job",
            "schedule": "0 6 1 * *",
            "timeZone": "Asia/Tokyo",
            "state": "PAUSED",
            "httpTarget": {"uri": BASE + "/admin/reports/new/scheduled-generate"},
        }
        other_service = {**extra, "name": "projects/ice-sh/locations/asia-northeast1/jobs/other", "httpTarget": {"uri": "https://other.run.app/x"}}
        view = _view(_all_jobs() + [extra, other_service])
        row = _row(view, "new-report-job")
        self.assertEqual((row["kind"], row["drift"], row["state"]), (rs.KIND_UNREGISTERED, rs.DRIFT_UNREGISTERED, "PAUSED"))
        self.assertFalse(any(r["id"] == "other" for r in view["items"]))


class RetryDeadlineDriftTests(unittest.TestCase):
    COIN = "jumpplus-coin-ledger"

    def _coin_row(self, **overrides):
        spec = _spec(self.COIN)
        job = _raw_job(spec)
        if "attemptDeadline" in overrides:
            job["attemptDeadline"] = overrides["attemptDeadline"]
        job["retryConfig"] = {**job["retryConfig"], **overrides.get("retryConfig", {})}
        for key in overrides.get("drop", ()):
            job["retryConfig"].pop(key, None)
        return _row(_view([job]), self.COIN)

    def test_exact_match_is_ok_and_exposes_both_sides(self):
        row = self._coin_row()
        self.assertEqual((row["drift"], row["drift_fields"]), (rs.DRIFT_OK, []))
        self.assertEqual(row["expected"]["attempt_deadline"], "1800s")
        self.assertEqual(row["expected"]["retry"]["max_retry_attempts"], 5)
        self.assertEqual(row["actual"]["attempt_deadline"], "1800s")
        self.assertEqual(row["actual"]["retry"]["min_backoff"], "600s")

    def test_each_field_drifts_individually(self):
        cases = {
            "attempt_deadline": {"attemptDeadline": "180s"},
            "retry.max_retry_attempts": {"retryConfig": {"retryCount": 4}},
            "retry.max_retry_duration": {"retryConfig": {"maxRetryDuration": "7200s"}},
            "retry.min_backoff": {"retryConfig": {"minBackoffDuration": "5s"}},
            "retry.max_backoff": {"retryConfig": {"maxBackoffDuration": "300s"}},
            "retry.max_doublings": {"retryConfig": {"maxDoublings": 5}},
        }
        for field, overrides in cases.items():
            with self.subTest(field=field):
                row = self._coin_row(**overrides)
                self.assertEqual(row["drift"], rs.DRIFT_CONFIG)
                self.assertEqual(row["drift_fields"], [field])

    def test_production_state_before_fix_is_detected(self):
        row = self._coin_row(
            attemptDeadline="180s",
            retryConfig={"retryCount": 0, "minBackoffDuration": "5s", "maxDoublings": 5},
        )
        self.assertEqual(
            row["drift_fields"],
            ["attempt_deadline", "retry.max_retry_attempts", "retry.min_backoff", "retry.max_doublings"],
        )

    def test_missing_api_fields_do_not_crash(self):
        spec = _spec(self.COIN)
        job = _raw_job(spec)
        del job["attemptDeadline"]
        del job["retryConfig"]
        row = _row(_view([job]), self.COIN)
        self.assertEqual(row["drift"], rs.DRIFT_CONFIG)
        self.assertIn("attempt_deadline", row["drift_fields"])
        self.assertIn("retry.max_retry_attempts", row["drift_fields"])
        self.assertNotIn("retry.max_retry_duration", row["drift_fields"])  # omitted == proto default 0s
        job["retryConfig"] = "garbage"
        self.assertEqual(_row(_view([job]), self.COIN)["drift"], rs.DRIFT_CONFIG)

    def test_real_api_shape_retry_count_is_ok(self):
        row = self._coin_row(retryConfig={"retryCount": 5})
        self.assertEqual((row["drift"], row["drift_fields"]), (rs.DRIFT_OK, []))
        self.assertEqual(row["actual"]["retry"]["max_retry_attempts"], 5)

    def test_real_api_shape_retry_count_mismatch(self):
        row = self._coin_row(retryConfig={"retryCount": 4})
        self.assertEqual(row["drift"], rs.DRIFT_CONFIG)
        self.assertIn("retry.max_retry_attempts", row["drift_fields"])

    def test_max_retry_attempts_fallback_still_supported(self):
        spec = _spec(self.COIN)
        job = _raw_job(spec)
        job["retryConfig"]["maxRetryAttempts"] = job["retryConfig"].pop("retryCount")
        row = _row(_view([job]), self.COIN)
        self.assertEqual((row["drift"], row["drift_fields"]), (rs.DRIFT_OK, []))

    def test_legacy_spec_without_expectation_is_unchanged(self):
        legacy = _spec("thermae-romae")
        self.assertIsNone(legacy.expected_attempt_deadline)
        self.assertIsNone(legacy.expected_retry)
        job = _raw_job(legacy)
        job["attemptDeadline"] = "180s"
        job["retryConfig"] = {"maxRetryAttempts": 3}
        row = _row(_view([job]), "thermae-romae")
        self.assertEqual((row["drift"], row["drift_fields"]), (rs.DRIFT_OK, []))
        self.assertIsNone(row["expected"]["retry"])
        self.assertEqual(row["actual"]["retry"]["max_retry_attempts"], 3)

    def test_api_exposes_new_fields_without_leaking_secrets(self):
        view = _view()
        self.assertNotIn("super-secret-admin-key", json.dumps(view))


class SafeResponseTests(unittest.TestCase):
    def test_case6_no_secret_fields_in_view(self):
        text = json.dumps(_view())
        for forbidden in ("super-secret-admin-key", "X-Admin-Key", "c2VjcmV0", "serviceAccountEmail", "sa@ice-sh", "headers", "oauthToken", "must not leak"):
            self.assertNotIn(forbidden, text)

    def test_case6_api_requires_admin_and_returns_safe_json(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_check_admin", return_value=(False, ({"error": "unauthorized"}, 401))):
            self.assertEqual(client.get("/admin/report-schedules").status_code, 401)
        live = rs.live_lookup(list_jobs=lambda p, l: _all_jobs())
        with mock.patch.object(app_module, "_check_admin", return_value=(True, None)), mock.patch.object(
            rs, "live_lookup", return_value=live
        ) as lookup, mock.patch.object(app_module, "list_report_definitions", return_value=[]):
            resp = client.get("/admin/report-schedules?refresh=1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(lookup.call_args.kwargs["refresh"])
        body = resp.get_data(as_text=True)
        self.assertNotIn("super-secret-admin-key", body)
        self.assertNotIn("serviceAccountEmail", body)
        self.assertEqual(resp.get_json()["summary"]["total"], len(resp.get_json()["items"]))

    def test_api_survives_live_and_firestore_failure(self):
        client = app_module.app.test_client()
        live = rs.live_lookup(list_jobs=lambda p, l: (_ for _ in ()).throw(_HttpError(403)))
        with mock.patch.object(app_module, "_check_admin", return_value=(True, None)), mock.patch.object(
            rs, "live_lookup", return_value=live
        ), mock.patch.object(app_module, "list_report_definitions", side_effect=RuntimeError("firestore down")):
            resp = client.get("/admin/report-schedules")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["live_lookup"]["reason"], "permission_denied")
        self.assertEqual(data["report_definitions"]["status"], "unavailable")
        self.assertNotIn("firestore down", resp.get_data(as_text=True))


class SortAndDefinitionTests(unittest.TestCase):
    definitions = [
        {"report_id": "zeta", "name": "Z定義", "status": "active", "schedule": {"enabled": True, "frequency": "monthly", "day_of_month": 5, "time_of_day": "10:30", "timezone": "Asia/Tokyo"}},
        {"report_id": "alpha", "name": "A定義", "status": "active", "schedule": {"enabled": True, "frequency": "monthly", "day_of_month": 3, "time_of_day": "09:00", "timezone": "Asia/Tokyo"}},
        {"report_id": "off", "name": "停止中", "status": "active", "schedule": {"enabled": False, "day_of_month": 1, "time_of_day": "09:00"}},
        {"report_id": "old", "name": "archived", "status": "archived", "schedule": {"enabled": True, "day_of_month": 1, "time_of_day": "09:00"}},
    ]

    def test_case7_stable_sort_independent_of_input_order(self):
        a = [r["id"] for r in _view(_all_jobs(), definitions=self.definitions)["items"]]
        b = [r["id"] for r in _view(list(reversed(_all_jobs())), definitions=list(reversed(self.definitions)))["items"]]
        self.assertEqual(a, b)
        kinds = [r["kind"] for r in _view(definitions=self.definitions)["items"]]
        self.assertEqual(kinds, sorted(kinds, key=rs.KIND_ORDER.index))

    def test_case8_report_definitions_rows_have_no_scheduler_job(self):
        view = _view(definitions=self.definitions)
        rows = [r for r in view["items"] if r["kind"] == rs.KIND_REPORT_DEFINITION]
        self.assertEqual([r["id"] for r in rows], ["alpha", "zeta"])
        for row in rows:
            self.assertIsNone(row["scheduler_job_name"])
            self.assertIsNone(row["expected"]["cron"])
            self.assertEqual(row["drift"], rs.DRIFT_NOT_APPLICABLE)
            self.assertEqual(row["state"], "METADATA_ENABLED")
        self.assertEqual(_row(view, "zeta")["expected"]["schedule_text"], "毎月5日 10:30")
        # the generic executor is its own, separate row
        executor = _row(view, "report-definitions-schedule-runs")
        self.assertEqual(executor["kind"], rs.KIND_REPORT_DEFINITION_EXECUTOR)
        self.assertEqual(executor["scheduler_job_name"], "report-definitions-monthly-schedule-runs")


class RegistryConsistencyTests(unittest.TestCase):
    def test_case9_coin_ledger_registry_entry(self):
        spec = _spec("jumpplus-coin-ledger")
        self.assertEqual(spec.kind, rs.KIND_REPORT_GENERATION)
        self.assertEqual(spec.scheduler_job_name, "jumpplus-coin-ledger-monthly-report")
        self.assertEqual(spec.expected_schedule, "0 7 1 * *")
        self.assertEqual(rs.human_readable_schedule(spec.expected_schedule), "毎月1日 07:00")
        self.assertEqual(spec.timezone, "Asia/Tokyo")
        self.assertEqual(spec.endpoint, "/admin/reports/jumpplus-coin-ledger/scheduled-generate")
        self.assertIn("readiness", spec.target_month_rule)
        import jumpplus_coin_ledger_report as coin

        self.assertEqual(spec.report_type, coin.REPORT_ID)
        # target month rule in code: previous month in Asia/Tokyo
        self.assertEqual(coin.parse_target_month(None, today=date(2026, 10, 1)), date(2026, 9, 1))

    def test_every_registered_endpoint_is_a_real_post_route(self):
        rules = {}
        for rule in app_module.app.url_map.iter_rules():
            rules.setdefault(rule.rule, set()).update(rule.methods or ())
        for spec in rs.REPORT_SCHEDULE_SPECS:
            matched = [r for r in rules if re.fullmatch(re.sub(r"<[^>]+>", "[^/]+", r), spec.endpoint)]
            self.assertTrue(matched, spec.endpoint)
            self.assertTrue(any("POST" in rules[r] for r in matched), spec.endpoint)

    def test_every_scheduled_endpoint_in_app_is_registered(self):
        registered = {spec.endpoint for spec in rs.REPORT_SCHEDULE_SPECS}
        for rule in app_module.app.url_map.iter_rules():
            if "scheduled-" in rule.rule or rule.rule.endswith("/schedule-runs") and rule.rule.startswith("/admin/"):
                pattern = re.sub(r"<[^>]+>", "[^/]+", rule.rule)
                self.assertTrue(any(re.fullmatch(pattern, e) for e in registered), rule.rule)

    def test_registry_ids_and_job_names_unique(self):
        ids = [s.id for s in rs.REPORT_SCHEDULE_SPECS]
        jobs = [s.scheduler_job_name for s in rs.REPORT_SCHEDULE_SPECS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(jobs), len(set(jobs)))
        self.assertTrue(all(s.kind in rs.KIND_ORDER for s in rs.REPORT_SCHEDULE_SPECS))


class AdminUiSchedulesTabTests(unittest.TestCase):
    def setUp(self):
        self.html = app_module.render_admin_ui()

    def test_schedules_tab_is_registered_in_common_tab_map(self):
        self.assertIn('schedules: "Schedules"', self.html)
        self.assertIn('id="tabBtnSchedules"', self.html)
        self.assertIn("showAdminTab('schedules')", self.html)
        self.assertIn('id="tabPanelSchedules"', self.html)

    def test_summary_search_filter_refresh_and_table(self):
        for marker in (
            'id="reportSchedulesSummary"',
            'id="reportSchedulesSearch"',
            'id="reportSchedulesStateFilter"',
            'onclick="loadReportSchedules(true)"',
            'id="reportSchedules"',
            '"/admin/report-schedules"',
        ):
            self.assertIn(marker, self.html)
        for header in ("レポート名", "report_id / report_type", "kind", "Scheduler job", "timezone", "cron", "state", "endpoint", "対象月ルール", "drift", "備考"):
            self.assertIn("<th>" + header + "</th>", self.html)

    def test_no_per_report_markup(self):
        for spec in rs.REPORT_SCHEDULE_SPECS:
            self.assertNotIn(spec.scheduler_job_name, self.html)
        self.assertNotIn("scheduled-generate", self.html)

    def test_case10_existing_tabs_still_present(self):
        match = re.search(r"const ADMIN_TAB_SUFFIXES = \{(.*?)\};", self.html, re.S)
        suffixes = re.findall(r'"(\w+)"', match.group(1))
        self.assertEqual(suffixes, ["Create", "Deliveries", "Logs", "Definitions", "PlusPointSales", "Schedules"])
        for suffix in suffixes:
            self.assertIn(f'id="tabBtn{suffix}"', self.html)
            self.assertIn(f'id="tabPanel{suffix}"', self.html)


if __name__ == "__main__":
    unittest.main()
