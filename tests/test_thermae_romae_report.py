import importlib
import sys
import types
import unittest
from datetime import date


def _install_stubs():
    google_stub = sys.modules.get("google") or types.ModuleType("google")
    google_cloud_stub = types.ModuleType("google.cloud")
    bigquery_stub = types.ModuleType("google.cloud.bigquery")

    class _QueryJobConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.query_parameters = kwargs.get("query_parameters", [])

    class _ScalarQueryParameter:
        def __init__(self, name, parameter_type, value):
            self.name = name
            self.parameter_type = parameter_type
            self.value = value

    class _ArrayQueryParameter:
        def __init__(self, name, parameter_type, values):
            self.name = name
            self.parameter_type = parameter_type
            self.values = values

    bigquery_stub.QueryJobConfig = _QueryJobConfig
    bigquery_stub.ScalarQueryParameter = _ScalarQueryParameter
    bigquery_stub.ArrayQueryParameter = _ArrayQueryParameter
    bigquery_stub.Client = object
    google_cloud_stub.bigquery = bigquery_stub
    google_stub.cloud = google_cloud_stub
    sys.modules["google"] = google_stub
    sys.modules["google.cloud"] = google_cloud_stub
    sys.modules["google.cloud.bigquery"] = bigquery_stub

    openpyxl_stub = types.ModuleType("openpyxl")
    openpyxl_stub.load_workbook = lambda *args, **kwargs: None
    utils_stub = types.ModuleType("openpyxl.utils")
    utils_stub.get_column_letter = lambda col: chr(64 + col)
    worksheet_package_stub = types.ModuleType("openpyxl.worksheet")
    worksheet_stub = types.ModuleType("openpyxl.worksheet.worksheet")
    worksheet_stub.Worksheet = object
    sys.modules["openpyxl"] = openpyxl_stub
    sys.modules["openpyxl.utils"] = utils_stub
    sys.modules["openpyxl.worksheet"] = worksheet_package_stub
    sys.modules["openpyxl.worksheet.worksheet"] = worksheet_stub


def _load_module():
    _install_stubs()
    sys.modules.pop("thermae_romae_report", None)
    return importlib.import_module("thermae_romae_report")


class ThermaeRomaeReportTest(unittest.TestCase):
    def test_target_month_and_payment_labels(self):
        report = _load_module()

        target = report.parse_target_month(today=date(2026, 7, 15))

        self.assertEqual(target, date(2026, 6, 1))
        self.assertEqual(report.period_label(target), "2026年06月01日〜06月30日")
        self.assertEqual(report.payment_due_month_end(target), date(2026, 9, 30))
        self.assertEqual(report.payment_due_text(target), "お支払い予定：2026年9月末")
        self.assertEqual(
            report.output_file_name(target),
            "KADOKAWA様_少年ジャンプ＋「テルマエ・ロマエ」販売報告書_2026年6月分.xlsx",
        )

    def test_target_month_must_be_first_day(self):
        report = _load_module()

        with self.assertRaisesRegex(report.ThermaeReportError, "target_month"):
            report.parse_target_month("2026-06-15")

    def test_detail_rows_use_template_book_code_mapping_and_summarize(self):
        report = _load_module()
        records = [
            {
                "sales_month_label": "2026年6月",
                "publisher_name": "KADOKAWA",
                "title_name": "テルマエ・ロマエ 1",
                "unit_price_tax_excluded": 100,
                "sales_count": 10,
                "payment_amount_tax_excluded": 550,
            },
            {
                "sales_month_label": "2026年6月",
                "publisher_name": "KADOKAWA",
                "title_name": "テルマエ・ロマエ 2",
                "unit_price_tax_excluded": 200,
                "sales_count": 5,
                "payment_amount_tax_excluded": 550,
            },
        ]

        rows = report.build_detail_rows(
            records,
            {
                "テルマエ・ロマエ 1": "BOOK001",
                "テルマエ・ロマエ 2": "BOOK002",
            },
        )
        summary = report.summarize_detail_rows(rows)

        self.assertEqual(rows[0]["書籍コード"], "BOOK001")
        self.assertEqual(rows[1]["単価（税抜）"], 200)
        self.assertEqual(summary["payment_total"], 1100)
        self.assertEqual(summary["tax"], 110)
        self.assertEqual(summary["total_with_tax"], 1210)
        self.assertEqual(summary["detail_row_count"], 2)

    def test_missing_book_code_is_explicit_error(self):
        report = _load_module()

        with self.assertRaises(report.ThermaeReportError) as ctx:
            report.build_detail_rows([{"title_name": "unknown"}], {})

        self.assertEqual(ctx.exception.code, "book_code_not_found")
        self.assertEqual(ctx.exception.details["title_name"], "unknown")

    def test_bigquery_query_uses_target_month_and_work_ids(self):
        report = _load_module()
        captured = {}

        class _Frame:
            def to_dict(self, orient):
                self.orient = orient
                return [{"title_name": "テルマエ・ロマエ 1"}]

        class _Job:
            def to_dataframe(self):
                return _Frame()

        class _Client:
            def __init__(self, project):
                captured["project"] = project

            def query(self, sql, job_config=None):
                captured["sql"] = sql
                captured["job_config"] = job_config
                return _Job()

        report.bigquery.Client = _Client

        rows = report.run_thermae_query(
            project_id="ice-sh",
            target_month=date(2026, 6, 1),
            table="project.dataset.table",
            work_ids=[100040643, 100040644],
        )

        self.assertEqual(rows[0]["title_name"], "テルマエ・ロマエ 1")
        self.assertIn("`project.dataset.table`", captured["sql"])
        params = {param.name: param for param in captured["job_config"].query_parameters}
        self.assertEqual(params["target_month"].value, date(2026, 6, 1))
        self.assertEqual(params["work_ids"].values, [100040643, 100040644])


if __name__ == "__main__":
    unittest.main()
