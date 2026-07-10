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
    styles_stub = types.ModuleType("openpyxl.styles")

    class _Side:
        def __init__(self, style=None, color=None):
            self.style = style
            self.color = color

    class _Border:
        def __init__(
            self,
            left=None,
            right=None,
            top=None,
            bottom=None,
            diagonal=None,
            diagonal_direction=None,
            diagonalUp=False,
            diagonalDown=False,
            outline=True,
            vertical=None,
            horizontal=None,
            start=None,
            end=None,
        ):
            self.left = left
            self.right = right
            self.top = top
            self.bottom = bottom
            self.diagonal = diagonal
            self.diagonal_direction = diagonal_direction
            self.diagonalUp = diagonalUp
            self.diagonalDown = diagonalDown
            self.outline = outline
            self.vertical = vertical
            self.horizontal = horizontal
            self.start = start
            self.end = end

    styles_stub.Border = _Border
    styles_stub.Side = _Side
    utils_stub = types.ModuleType("openpyxl.utils")
    utils_stub.get_column_letter = lambda col: chr(64 + col)
    worksheet_package_stub = types.ModuleType("openpyxl.worksheet")
    worksheet_stub = types.ModuleType("openpyxl.worksheet.worksheet")
    worksheet_stub.Worksheet = object
    sys.modules["openpyxl"] = openpyxl_stub
    sys.modules["openpyxl.styles"] = styles_stub
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
        self.assertEqual(report.payment_due_text(date(2026, 7, 10)), "※御支払いは2026年10月末を予定しております。")
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
            target_month=date(2026, 6, 1),
            fixed_items=(("BOOK001", "テルマエ・ロマエ 1"), ("BOOK002", "テルマエ・ロマエ 2")),
        )
        summary = report.summarize_detail_rows(rows)

        self.assertEqual(rows[0]["書籍コード"], "BOOK001")
        self.assertEqual(rows[1]["単価（税抜）"], 200)
        self.assertEqual(summary["payment_total"], 1100)
        self.assertEqual(summary["tax"], 110)
        self.assertEqual(summary["total_with_tax"], 1210)
        self.assertEqual(summary["detail_row_count"], 2)

    def test_unexpected_title_name_is_explicit_error(self):
        report = _load_module()

        with self.assertRaises(report.ThermaeReportError) as ctx:
            report.build_detail_rows([{"title_name": "unknown"}], {}, fixed_items=(("BOOK001", "known"),))

        self.assertEqual(ctx.exception.code, "unexpected_title_name")
        self.assertEqual(ctx.exception.details["title_name"], "unknown")

    def test_fixed_detail_rows_include_zero_rows_for_missing_titles(self):
        report = _load_module()

        rows = report.build_detail_rows(
            [
                {
                    "sales_month_label": "2026年6月",
                    "publisher_name": "KADOKAWA",
                    "title_name": "テルマエ・ロマエ 2",
                    "unit_price_tax_excluded": 200,
                    "sales_count": 5,
                    "payment_amount_tax_excluded": 550,
                }
            ],
            {},
            target_month=date(2026, 6, 1),
            fixed_items=(("BOOK001", "テルマエ・ロマエ 1"), ("BOOK002", "テルマエ・ロマエ 2")),
        )

        self.assertEqual(rows[0]["書籍コード"], "BOOK001")
        self.assertEqual(rows[0]["タイトル名"], "テルマエ・ロマエ 1")
        self.assertEqual(rows[0]["売上月/売上日"], "2026年6月")
        self.assertEqual(rows[0]["売上件数"], 0)
        self.assertEqual(rows[1]["書籍コード"], "BOOK002")
        self.assertEqual(rows[1]["売上件数"], 5)

    def test_detail_sheet_writes_fixed_totals_to_f56_g58(self):
        report = _load_module()

        class _Cell:
            def __init__(self, value=None, row=1, column=1):
                self.value = value
                self.row = row
                self.column = column
                self.border = report.Border()

        class _Worksheet:
            max_row = 60

            def __init__(self):
                self.cells = {}
                headers = list(report.DETAIL_HEADERS)
                for idx, header in enumerate(headers, start=1):
                    self.cell(row=1, column=idx).value = header

            def cell(self, row, column):
                key = (row, column)
                if key not in self.cells:
                    self.cells[key] = _Cell(row=row, column=column)
                return self.cells[key]

            def iter_rows(self, min_row=1, max_row=30):
                for row_idx in range(min_row, max_row + 1):
                    yield [self.cell(row=row_idx, column=col_idx) for col_idx in range(1, 8)]

            def __getitem__(self, row_idx):
                return [self.cell(row=row_idx, column=col_idx) for col_idx in range(1, 8)]

        ws = _Worksheet()
        rows = []
        for index in range(54):
            rows.append(
                {
                    "売上月/売上日": "2026年6月",
                    "出版社名": "KADOKAWA",
                    "書籍コード": f"BOOK{index:03}",
                    "タイトル名": f"title {index}",
                    "単価（税抜）": 100,
                    "売上件数": 1,
                    "支払額（税抜）": 55,
                }
            )

        report.write_detail_sheet(
            ws,
            rows,
            {"payment_total": 2970, "tax": 297, "total_with_tax": 3267},
        )

        self.assertEqual(ws.cell(row=2, column=3).value, "BOOK000")
        self.assertEqual(ws.cell(row=55, column=3).value, "BOOK053")
        self.assertEqual(ws.cell(row=56, column=6).value, "支払額計")
        self.assertEqual(ws.cell(row=56, column=7).value, 2970)
        self.assertEqual(ws.cell(row=57, column=6).value, "消費税額（※支払額計×0.1）")
        self.assertEqual(ws.cell(row=58, column=6).value, "税込計")
        self.assertEqual(ws.cell(row=58, column=7).value, 3267)
        self.assertEqual(ws.cell(row=56, column=6).border.left.style, "thick")
        self.assertEqual(ws.cell(row=56, column=6).border.top.style, "thick")
        self.assertEqual(ws.cell(row=58, column=7).border.right.style, "thick")
        self.assertEqual(ws.cell(row=58, column=7).border.bottom.style, "thick")

    def test_invoice_sheet_writes_generated_date_and_generated_month_due_text(self):
        report = _load_module()

        class _PageSetup:
            paperSize = None
            orientation = None
            scale = None

        class _PrintOptions:
            horizontalCentered = False

        class _Worksheet:
            PAPERSIZE_A4 = "9"
            ORIENTATION_PORTRAIT = "portrait"

            def __init__(self):
                self.values = {}
                self.page_setup = _PageSetup()
                self.print_options = _PrintOptions()
                self.print_area = None

            def __setitem__(self, key, value):
                self.values[key] = value

            def __getitem__(self, key):
                return self.values.get(key)

        ws = _Worksheet()

        report.write_invoice_sheet(
            ws,
            date(2026, 6, 1),
            {"payment_total": 66862, "tax": 6686, "total_with_tax": 73548},
            generated_date=date(2026, 7, 10),
        )

        self.assertEqual(ws["G3"], date(2026, 7, 10))
        self.assertEqual(ws["B53"], "※御支払いは2026年10月末を予定しております。")
        self.assertEqual(ws["E42"], 66862)
        self.assertEqual(ws["E43"], 6686)
        self.assertEqual(ws["E44"], 73548)

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
        self.assertIn("cast(work_id as int64) in unnest(@work_ids)", captured["sql"])
        params = {param.name: param for param in captured["job_config"].query_parameters}
        self.assertEqual(params["target_month"].value, date(2026, 6, 1))
        self.assertEqual(params["work_ids"].values, [100040643, 100040644])


if __name__ == "__main__":
    unittest.main()
