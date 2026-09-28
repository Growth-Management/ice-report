import json
import logging
import os
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import jumpplus_coin_ledger_report as report


def _ledger_grain(*, skip=(), duplicate=(), use_coins=None):
    use_coins = use_coins or {"iOS": 600, "And": 240, "Web": 60}
    rows = []
    for pf in report.LEDGER_PLATFORMS:
        for ct in report.COIN_TYPES:
            if (pf, ct) in skip:
                continue
            rows.append(
                {
                    "app_pf": pf,
                    "coin_type": ct,
                    "row_count": 2 if (pf, ct) in duplicate else 1,
                    "use_coins": use_coins[pf] // len(report.COIN_TYPES),
                }
            )
    return rows


def _content_totals(*, skip_app_ids=(), totals=None):
    totals = totals or {31: 600, 32: 240, 101: 60}
    return [
        {"app_id": app_id, "row_count": 10, "total_use_coins": totals[app_id]}
        for app_id in (31, 32, 101)
        if app_id not in skip_app_ids
    ]


def _master(row_count=100, distinct_ids=100, null_ids=0):
    return {"row_count": row_count, "distinct_ids": distinct_ids, "null_ids": null_ids}


class TargetMonthTests(unittest.TestCase):
    def test_scheduled_default_is_previous_month(self):
        self.assertEqual(report.parse_target_month(None, today=date(2026, 10, 1)), date(2026, 9, 1))

    def test_year_boundary(self):
        self.assertEqual(report.parse_target_month("", today=date(2027, 1, 1)), date(2026, 12, 1))
        self.assertEqual(report.previous_month_first(date(2027, 1, 15)), date(2026, 12, 1))

    def test_explicit_month_forms(self):
        today = date(2026, 9, 28)
        self.assertEqual(report.parse_target_month("2026-08", today=today), date(2026, 8, 1))
        self.assertEqual(report.parse_target_month("2026-08-01", today=today), date(2026, 8, 1))
        self.assertEqual(report.parse_target_month("2025-12", today=today), date(2025, 12, 1))

    def test_invalid_months_are_rejected(self):
        today = date(2026, 9, 28)
        for value in ("2026-13", "2026/08", "2026-08-15", "202608", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(report.CoinLedgerReportError) as ctx:
                    report.parse_target_month(value, today=today)
                self.assertEqual(ctx.exception.code, "invalid_target_month")

    def test_incomplete_month_is_rejected(self):
        with self.assertRaises(report.CoinLedgerReportError):
            report.parse_target_month("2026-09", today=date(2026, 9, 28))

    def test_manual_requires_explicit_month(self):
        with self.assertRaises(report.CoinLedgerReportError) as ctx:
            report.parse_target_month("", today=date(2026, 9, 28), required=True)
        self.assertEqual(ctx.exception.code, "target_month_required")

    def test_output_file_names_match_spec_example(self):
        names = report.output_file_names(date(2026, 8, 1), date(2026, 9, 1))
        self.assertEqual(names["app"], "【少年ジャンプ＋】消費コイン_2026年08月期_260901.xlsx")
        self.assertEqual(names["web"], "【少年ジャンプ＋】消費コイン_WEB_2026年08月期_260901.xlsx")
        self.assertEqual(names["product"], "話売商品_一覧_2608.xlsx")


class ReadinessTests(unittest.TestCase):
    def evaluate(self, **overrides):
        kwargs = {"ledger_grain": _ledger_grain(), "content_totals": _content_totals(), "master_stats": _master()}
        kwargs.update(overrides)
        return report.evaluate_readiness(**kwargs)

    def test_ready_when_all_checks_pass(self):
        result = self.evaluate()
        self.assertTrue(result.ready, result.reasons)
        self.assertEqual(result.checks["ledger_rows"], 18)
        self.assertEqual(result.status, "ready")

    def test_six_coin_types_required_per_platform(self):
        result = self.evaluate(ledger_grain=_ledger_grain(skip={("Web", "reward_video_ad_coin")}))
        self.assertFalse(result.ready)
        self.assertIn("ledger_coin_type_missing", result.reasons)

    def test_missing_ledger_platform(self):
        grain = [r for r in _ledger_grain() if r["app_pf"] != "And"]
        result = self.evaluate(ledger_grain=grain)
        self.assertIn("ledger_platform_missing", result.reasons)

    def test_duplicate_grain_fails_closed(self):
        result = self.evaluate(ledger_grain=_ledger_grain(duplicate={("iOS", "pay_coin")}))
        self.assertIn("ledger_duplicate_grain", result.reasons)

    def test_unexpected_coin_type_fails_closed(self):
        grain = _ledger_grain() + [{"app_pf": "iOS", "coin_type": "new_coin", "row_count": 1, "use_coins": 0}]
        result = self.evaluate(ledger_grain=grain)
        self.assertIn("ledger_unexpected_coin_type", result.reasons)

    def test_each_content_app_id_required(self):
        for app_id in (31, 32, 101):
            with self.subTest(app_id=app_id):
                result = self.evaluate(content_totals=_content_totals(skip_app_ids={app_id}))
                self.assertIn("content_platform_missing", result.reasons)
                self.assertIn("ledger_content_reconciliation_mismatch", result.reasons)

    def test_reconciliation_mismatch_per_platform(self):
        result = self.evaluate(content_totals=_content_totals(totals={31: 600, 32: 239, 101: 60}))
        self.assertIn("ledger_content_reconciliation_mismatch", result.reasons)
        self.assertTrue(result.checks["reconciled_iOS"])
        self.assertFalse(result.checks["reconciled_And"])

    def test_product_master_empty_or_duplicated(self):
        self.assertIn("product_master_empty", self.evaluate(master_stats=_master(0, 0)).reasons)
        self.assertIn("product_master_duplicate_id", self.evaluate(master_stats=_master(100, 99)).reasons)
        self.assertIn("product_master_duplicate_id", self.evaluate(master_stats=_master(100, 99, 1)).reasons)

    def test_checks_never_contain_coin_totals(self):
        result = self.evaluate()
        for key in result.checks:
            self.assertNotIn("coins", key)


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return [mock.Mock(items=lambda row=row: row.items()) for row in self._rows]

    def to_dataframe(self):
        frame = mock.Mock()
        frame.to_dict.return_value = list(self._rows)
        return frame


class _RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []

    def query(self, query, job_config=None):
        params = {p.name: getattr(p, "value", getattr(p, "values", None)) for p in (job_config.query_parameters if job_config else [])}
        self.queries.append((query, params))
        return _FakeQueryJob(self.responses.pop(0))


class SourceQueryTests(unittest.TestCase):
    def test_mom_table_can_never_be_configured(self):
        with mock.patch.dict(os.environ, {"JUMPPLUS_COIN_LEDGER_CONTENT_TABLE": "p.d.report_plus_monthly_coin_content_report_mom"}):
            with self.assertRaises(report.CoinLedgerReportError) as ctx:
                report.content_table()
        self.assertEqual(ctx.exception.code, "forbidden_source_table")

    def test_readiness_queries_are_target_month_scoped_and_mom_free(self):
        client = _RecordingClient([_ledger_grain(), _content_totals(), [_master()]])
        result = report.check_source_readiness(project_id="p", target_month=date(2026, 8, 1), client=client)
        self.assertTrue(result.ready, result.reasons)
        ledger_q, content_q, master_q = (q for q, _ in client.queries)
        self.assertIn("month_jst = @target_month", ledger_q)
        self.assertIn("purchase_date_month_jst = @target_month", content_q)
        self.assertIn("app_id in (31, 32, 101)", content_q)
        self.assertIn("content_type = 'episode'", master_q)
        for query, params in client.queries[:2]:
            self.assertNotIn("_mom", query)
            self.assertEqual(params["target_month"], date(2026, 8, 1))

    def test_content_fetch_reads_base_table_for_any_month(self):
        client = _RecordingClient([[{"app_id": 31}]])
        report.fetch_content_rows(client=client, target_month=date(2025, 12, 1))
        query, params = client.queries[0]
        self.assertIn(f"`{report.DEFAULT_CONTENT_TABLE}`", query)
        self.assertNotIn("_mom", query)
        self.assertEqual(params["target_month"], date(2025, 12, 1))
        self.assertEqual(params["purchase_types"], ["episode", "book"])

    def test_product_master_reads_episode_master_not_purchases(self):
        client = _RecordingClient([[]])
        report.fetch_product_master_rows(client=client)
        query, _ = client.queries[0]
        self.assertIn(report.DEFAULT_PRODUCT_MASTER_TABLE, query)
        self.assertNotIn("coin_content_report", query)
        self.assertNotIn("ws_ex_mailaddress", query)

    def test_content_rows_for_splits_by_app_id_and_purchase_type(self):
        rows = [
            {"app_id": 31, "purchase_type": "episode"},
            {"app_id": 31.0, "purchase_type": "book"},
            {"app_id": 32, "purchase_type": "episode"},
            {"app_id": 101, "purchase_type": "book"},
        ]
        self.assertEqual(len(report.content_rows_for(rows, app_id=31, purchase_type="episode")), 1)
        self.assertEqual(len(report.content_rows_for(rows, app_id=31, purchase_type="book")), 1)
        self.assertEqual(len(report.content_rows_for(rows, app_id=101, purchase_type="episode")), 0)


def _generated(tmp: Path) -> list:
    files = []
    for spec in report.OUTPUT_FILES:
        path = tmp / f"{spec.file_key}.xlsx"
        path.write_bytes(b"x")
        files.append(report.GeneratedFile(spec.file_key, spec.label, f"{spec.file_key}.xlsx", path, {}))
    return files


class UploadAtomicityTests(unittest.TestCase):
    def test_all_files_go_to_gcs_then_drive(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            files = _generated(Path(tmp))
            calls = []
            with mock.patch.dict(os.environ, {"BUCKET_NAME": "bucket"}):
                report.upload_generated_files(
                    files,
                    target_month=date(2026, 8, 1),
                    folder_id="folder",
                    gcs_upload=lambda path, obj: calls.append(("gcs", obj)) or f"gs://bucket/{obj}",
                    drive_upload=lambda path, **kw: calls.append(("drive", kw["file_name"])) or {"id": kw["file_name"]},
                    drive_trash=lambda file_id: calls.append(("trash", file_id)),
                )
        self.assertEqual([c[0] for c in calls], ["gcs"] * 3 + ["drive"] * 3)
        self.assertTrue(all(c[1].startswith("reports/jumpplus-coin-ledger/2026-08/") for c in calls[:3]))
        self.assertTrue(all(f.gcs_uri and f.drive_file_id for f in files))

    def test_drive_failure_trashes_this_runs_earlier_uploads(self):
        import tempfile

        trashed = []

        def drive_upload(path, **kw):
            if kw["file_name"] == "web.xlsx":
                raise RuntimeError("drive down")
            return {"id": "id-" + kw["file_name"]}

        with tempfile.TemporaryDirectory() as tmp:
            files = _generated(Path(tmp))
            with self.assertRaises(RuntimeError):
                report.upload_generated_files(
                    files,
                    target_month=date(2026, 8, 1),
                    folder_id="folder",
                    gcs_upload=lambda path, obj: f"gs://bucket/{obj}",
                    drive_upload=drive_upload,
                    drive_trash=trashed.append,
                )
        self.assertEqual(trashed, ["id-app.xlsx"])


class OrchestrationTests(unittest.TestCase):
    def _ready(self):
        return report.ReadinessResult(ready=True, checks={"ledger_rows": 18})

    def test_source_not_ready_generates_and_uploads_nothing(self):
        deliver = mock.Mock()
        not_ready = report.ReadinessResult(ready=False, reasons=["content_platform_missing"])
        with mock.patch.object(report, "check_source_readiness", return_value=not_ready), mock.patch.object(
            report, "fetch_source_data"
        ) as fetch, mock.patch.object(report, "build_workbooks") as build, mock.patch.object(
            report, "upload_generated_files"
        ) as upload:
            with self.assertRaises(report.CoinLedgerReportError) as ctx:
                report.generate_coin_ledger_report(
                    project_id="p", target_month=date(2026, 8, 1), generated_date=date(2026, 9, 1),
                    trigger="scheduled", deliver=deliver,
                )
        self.assertEqual(ctx.exception.code, "source_not_ready")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertTrue(ctx.exception.retryable)
        fetch.assert_not_called()
        build.assert_not_called()
        upload.assert_not_called()
        deliver.assert_not_called()

    def test_build_failure_on_any_file_leaves_no_upload_or_delivery(self):
        deliver = mock.Mock()
        with mock.patch.object(report, "check_source_readiness", return_value=self._ready()), mock.patch.object(
            report, "fetch_source_data", return_value=report.SourceData([], [], [])
        ), mock.patch("drive_io.download_drive_file", side_effect=lambda fid, path: Path(path)), mock.patch.object(
            report, "build_workbooks", side_effect=report.CoinLedgerReportError("template_header_missing", status_code=500)
        ), mock.patch.object(report, "upload_generated_files") as upload:
            with self.assertRaises(report.CoinLedgerReportError):
                report.generate_coin_ledger_report(
                    project_id="p", target_month=date(2026, 8, 1), generated_date=date(2026, 9, 1),
                    trigger="manual", deliver=deliver,
                )
        upload.assert_not_called()
        deliver.assert_not_called()

    def test_deliver_runs_only_after_all_uploads(self):
        order = []

        def fake_build(**kwargs):
            return [
                report.GeneratedFile(s.file_key, s.label, f"{s.file_key}.xlsx", kwargs["output_dir"] / "x", {"rows": 1})
                for s in report.OUTPUT_FILES
            ]

        def fake_upload(files, **kwargs):
            order.append("upload")
            for f in files:
                f.gcs_uri, f.drive_file_id = f"gs://b/{f.file_key}", f"d-{f.file_key}"

        def deliver(files):
            order.append("deliver")
            self.assertTrue(all(f.gcs_uri and f.drive_file_id for f in files))
            return {"delivery_id": "jumpplus-coin-ledger_2026-08", "version": 1}

        with mock.patch.object(report, "check_source_readiness", return_value=self._ready()), mock.patch.object(
            report, "fetch_source_data", return_value=report.SourceData([], [], [])
        ), mock.patch("drive_io.download_drive_file", side_effect=lambda fid, path: Path(path)), mock.patch.object(
            report, "build_workbooks", side_effect=fake_build
        ), mock.patch.object(report, "upload_generated_files", side_effect=fake_upload):
            result = report.generate_coin_ledger_report(
                project_id="p", target_month=date(2026, 8, 1), generated_date=date(2026, 9, 1),
                trigger="manual", deliver=deliver,
            )
        self.assertEqual(order, ["upload", "deliver"])
        self.assertEqual(result["target_month"], "2026-08")
        self.assertEqual([f["file_key"] for f in result["files"]], ["app", "web", "product"])
        self.assertEqual(result["delivery"]["version"], 1)


class LoggingTests(unittest.TestCase):
    def test_info_events_are_emitted_as_structured_json(self):
        handler = next(h for h in report.logger.handlers if getattr(h, "_coin_ledger_handler", False))
        with self.assertLogs(report.LOGGER_NAME, level="INFO") as logs:
            report.log_event("READINESS", target_month="2026-08", trigger="scheduled", ledger_rows=18)
        record = logs.records[0]
        self.assertEqual(record.levelno, logging.INFO)
        payload = json.loads(handler.formatter.format(record))
        self.assertEqual(payload["severity"], "INFO")
        self.assertEqual(payload["event"], "READINESS")
        self.assertEqual(payload["target_month"], "2026-08")
        self.assertEqual(report.logger.level, logging.INFO)
        self.assertFalse(report.logger.propagate)


if __name__ == "__main__":
    unittest.main()
