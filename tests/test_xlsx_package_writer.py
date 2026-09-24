import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table, TableColumn

import jumpplus_ad_revenue_report as report
import xlsx_package_writer as pkg_writer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xlsx_fixtures import POWER_QUERY_PARTS, build_app2_like_template  # noqa: E402

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_plain_template(tmp_path: Path, *, headers: tuple[str, ...], sheet_name: str = "全体") -> Path:
    """A minimal template with just サマリ + one detail sheet -- no Power
    Query parts -- for tests that only care about row/table-ref mechanics."""
    wb = Workbook()
    wb.remove(wb.active)
    summary = wb.create_sheet(report.SUMMARY_SHEET)
    summary["A1"] = "広告売上"
    summary["B3"] = "区分"
    summary["C3"] = "金額"
    summary["B4"] = "総計"
    summary["C4"] = None

    ws = wb.create_sheet(sheet_name)
    for col, header in enumerate(headers, start=1):
        ws.cell(row=3, column=col, value=header)
    for col in range(1, len(headers) + 1):
        ws.cell(row=4, column=col).number_format = "#,##0"
    last_col_letter = ws.cell(row=3, column=len(headers)).column_letter
    table = Table(displayName=f"table_{sheet_name}", ref=f"A3:{last_col_letter}4")
    table.tableColumns = [TableColumn(id=i, name=h) for i, h in enumerate(headers, start=1)]
    ws.add_table(table)

    out_path = tmp_path / "plain_template.xlsx"
    wb.save(out_path)
    return out_path


class UpdateSummaryValueTests(unittest.TestCase):
    def test_writes_value_next_to_first_total_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            pkg_writer.update_summary_value(pkg, report.SUMMARY_SHEET, 734139)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            wb = load_workbook(out)
            self.assertEqual(wb[report.SUMMARY_SHEET]["C4"].value, 734139)

    def test_missing_label_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            wb = Workbook()
            wb.remove(wb.active)
            wb.create_sheet(report.SUMMARY_SHEET)["A1"] = "no total here"
            path = Path(tmp) / "t.xlsx"
            wb.save(path)

            pkg = pkg_writer.XlsxPackage.load(path)
            with self.assertRaises(pkg_writer.XlsxPackageError) as ctx:
                pkg_writer.update_summary_value(pkg, report.SUMMARY_SHEET, 1)
            self.assertEqual(ctx.exception.code, "total_label_not_found")

    def test_missing_sheet_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            with self.assertRaises(pkg_writer.XlsxPackageError) as ctx:
                pkg_writer.update_summary_value(pkg, "ないシート", 1)
            self.assertEqual(ctx.exception.code, "sheet_not_found")


class ReplaceDetailRowsTests(unittest.TestCase):
    def _rows(self, headers, count, *, value_header):
        return [
            {
                h: (f"v{i}-{h}" if h != value_header else i)
                for h in headers
            }
            for i in range(1, count + 1)
        ]

    def test_grows_table_and_writes_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            rows = self._rows(report.AD_VIEW_DETAIL_HEADERS, 5, value_header="広告表示数")
            pkg_writer.replace_detail_rows(pkg, "全体", report.AD_VIEW_DETAIL_HEADERS, rows)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            wb = load_workbook(out)
            ws = wb["全体"]
            table = ws.tables[f"table_全体"]
            self.assertEqual(table.ref, "A3:J8")
            self.assertEqual(ws.cell(row=8, column=5).value, 5)
            self.assertEqual(ws.cell(row=4, column=5).value, 1)

    def test_shrinks_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            # first grow to 5 so there is something to shrink from
            rows5 = self._rows(report.AD_VIEW_DETAIL_HEADERS, 5, value_header="広告表示数")
            pkg_writer.replace_detail_rows(pkg, "全体", report.AD_VIEW_DETAIL_HEADERS, rows5)
            rows2 = self._rows(report.AD_VIEW_DETAIL_HEADERS, 2, value_header="広告表示数")
            pkg_writer.replace_detail_rows(pkg, "全体", report.AD_VIEW_DETAIL_HEADERS, rows2)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            wb = load_workbook(out)
            ws = wb["全体"]
            table = ws.tables[f"table_全体"]
            self.assertEqual(table.ref, "A3:J5")
            self.assertEqual(ws.cell(row=5, column=5).value, 2)

    def test_missing_required_headers_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=("コンテンツID_Raise", "コンテンツ名"))
            pkg = pkg_writer.XlsxPackage.load(path)
            with self.assertRaises(pkg_writer.XlsxPackageError) as ctx:
                pkg_writer.replace_detail_rows(
                    pkg,
                    "全体",
                    report.AD_VIEW_DETAIL_HEADERS,
                    [{}],
                    required_headers=report._REQUIRED_DETAIL_HEADERS,
                )
            self.assertEqual(ctx.exception.code, "missing_required_headers")

    def test_missing_sheet_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            with self.assertRaises(pkg_writer.XlsxPackageError) as ctx:
                pkg_writer.replace_detail_rows(pkg, "ない", report.AD_VIEW_DETAIL_HEADERS, [])
            self.assertEqual(ctx.exception.code, "sheet_not_found")

    def test_nan_value_writes_as_empty_cell_not_literal_nan(self):
        """Regression: BigQuery numeric columns come back through pandas as
        float NaN for missing values (not None/null), e.g.
        コミックス巻数/ex_episode_package_no. Writing "nan" as <v> text is
        invalid OOXML numeric content and made Excel refuse to open the file
        at all (not just a repair prompt) the first time this was tried
        against real full-scale 2026-08 data."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            rows = [
                {h: (float("nan") if h == "コミックス巻数" else f"v-{h}") for h in report.AD_VIEW_DETAIL_HEADERS}
            ]
            pkg_writer.replace_detail_rows(pkg, "全体", report.AD_VIEW_DETAIL_HEADERS, rows)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            with zipfile.ZipFile(out) as zf:
                sheet_xml = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
                for name in sheet_xml:
                    self.assertNotIn(b"nan", zf.read(name).lower())

            wb = load_workbook(out)  # must not raise
            ws = wb["全体"]
            comic_vol_col = report.AD_VIEW_DETAIL_HEADERS.index("コミックス巻数") + 1
            self.assertIsNone(ws.cell(row=4, column=comic_vol_col).value)

    def test_table_ref_matches_golden_master_row_counts(self):
        """Locks in the exact table refs from the acceptance plan: header
        row 3, 8 columns (video-reward's headers), 59614 real iOS rows."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(
                Path(tmp), headers=report.VIDEO_REWARD_DETAIL_HEADERS, sheet_name="iOS"
            )
            pkg = pkg_writer.XlsxPackage.load(path)
            rows = self._rows(report.VIDEO_REWARD_DETAIL_HEADERS, 59614, value_header="コイン消費数")
            pkg_writer.replace_detail_rows(pkg, "iOS", report.VIDEO_REWARD_DETAIL_HEADERS, rows)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())
            wb = load_workbook(out)
            table = wb["iOS"].tables["table_iOS"]
            self.assertEqual(table.ref, "A3:H59617")

    def test_formula_column_preserved_across_growth(self):
        headers = report.AD_VIEW_DETAIL_HEADERS
        with tempfile.TemporaryDirectory() as tmp:
            wb = Workbook()
            wb.remove(wb.active)
            wb.create_sheet(report.SUMMARY_SHEET)["B4"] = "総計"
            ws = wb.create_sheet("全体")
            all_headers = list(headers) + ["広告売上"]
            for col, header in enumerate(all_headers, start=1):
                ws.cell(row=3, column=col, value=header)
            formula_col = len(headers) + 1
            for row in (4, 5):
                ws.cell(row=row, column=formula_col).value = '=IFERROR([@広告表示数]*2,"-")'
            last_col_letter = ws.cell(row=3, column=len(all_headers)).column_letter
            table = Table(displayName="table_全体", ref=f"A3:{last_col_letter}5")
            table.tableColumns = [TableColumn(id=i, name=h) for i, h in enumerate(all_headers, start=1)]
            ws.add_table(table)
            path = Path(tmp) / "t.xlsx"
            wb.save(path)

            pkg = pkg_writer.XlsxPackage.load(path)
            rows = self._rows(headers, 3, value_header="広告表示数")
            pkg_writer.replace_detail_rows(pkg, "全体", headers, rows)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            wb2 = load_workbook(out)
            ws2 = wb2["全体"]
            self.assertEqual(ws2.cell(row=6, column=formula_col).value, '=IFERROR([@広告表示数]*2,"-")')


class PowerQueryPreservationTests(unittest.TestCase):
    """Reproduces the actual production symptom: APP_2's 作品別/作品別_2
    sheets are Power-Query-backed tables this module never writes to, and
    the real official template's Excel-repair log named exactly the parts
    these tests check for."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.template_path = build_app2_like_template(Path(self.tmp_dir.name) / "app2_template.xlsx")
        self.output_path = Path(self.tmp_dir.name) / "app2_output.xlsx"

    def _generate(self, *, ios=2, android=2, zentai=2):
        headers = report.AD_VIEW_DETAIL_HEADERS

        def _rows(count):
            return [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"content-{i}",
                    "JDCN": f"jdcn-{i}",
                    "広告表示数": i * 10,
                    "作品名": "work",
                    "コミックスJDCN": "comic-jdcn",
                    "コミックス巻数": 1,
                    "タイトルID": 999,
                    "デジタルタイトル名": "digital-title",
                }
                for i in range(1, count + 1)
            ]

        detail_rows = {"iOS": _rows(ios), "Android": _rows(android), "全体": _rows(zentai)}
        result = report.create_ad_revenue_workbook(
            report_type="app2",
            template_path=self.template_path,
            output_path=self.output_path,
            revenue_yen=9789547,
            detail_rows=detail_rows,
        )
        return result

    def test_power_query_parts_survive_byte_for_byte(self):
        self._generate()
        preserved = pkg_writer.validate_preserved_parts(
            self.template_path, self.output_path, list(POWER_QUERY_PARTS)
        )
        for part, ok in preserved.items():
            self.assertTrue(ok, f"{part} was not preserved byte-for-byte")

    def test_table_relationship_to_query_table_preserved(self):
        self._generate()
        with zipfile.ZipFile(self.template_path) as zf:
            template_names = zf.namelist()
        rels_names = [n for n in template_names if n.startswith("xl/tables/_rels/") and n.endswith(".rels")]
        self.assertEqual(len(rels_names), 2)

        with zipfile.ZipFile(self.output_path) as zf:
            for rels_name in rels_names:
                self.assertIn(rels_name, zf.namelist())
                root = etree.fromstring(zf.read(rels_name))
                rel = root.find(f"{REL_NS}Relationship")
                self.assertIsNotNone(rel)
                self.assertTrue(rel.get("Target", "").startswith("../queryTables/queryTable"))
                self.assertTrue(rel.get("Type", "").endswith("/queryTable"))

    def test_connection_id_consistency(self):
        self._generate()
        with zipfile.ZipFile(self.output_path) as zf:
            connections_root = etree.fromstring(zf.read("xl/connections.xml"))
            connection_ids = {c.get("id") for c in connections_root.findall(f"{MAIN_NS}connection")}
            qt1 = etree.fromstring(zf.read("xl/queryTables/queryTable1.xml"))
            qt2 = etree.fromstring(zf.read("xl/queryTables/queryTable2.xml"))
        self.assertEqual(qt1.get("connectionId"), "2")
        self.assertEqual(qt2.get("connectionId"), "3")
        self.assertIn(qt1.get("connectionId"), connection_ids)
        self.assertIn(qt2.get("connectionId"), connection_ids)

    def test_query_table_sheets_are_byte_for_byte_untouched(self):
        """作品別/作品別_2 worksheet XML itself -- not just connections/
        queryTables -- must be untouched, since this module never calls
        replace_detail_rows for them."""
        self._generate()
        with zipfile.ZipFile(self.template_path) as zf:
            sheet_names = zf.namelist()
            template_pkg = pkg_writer.XlsxPackage.load(self.template_path)
        sakuhin_part = pkg_writer._sheet_name_to_part(template_pkg, "作品別")
        sakuhin2_part = pkg_writer._sheet_name_to_part(template_pkg, "作品別_2")

        preserved = pkg_writer.validate_preserved_parts(
            self.template_path, self.output_path, [sakuhin_part, sakuhin2_part]
        )
        self.assertTrue(preserved[sakuhin_part])
        self.assertTrue(preserved[sakuhin2_part])

    def test_golden_master_values_and_row_counts(self):
        result = self._generate(ios=3, android=3, zentai=3)
        self.assertEqual(result["detail_row_count"], 9)

        wb = load_workbook(self.output_path)
        self.assertEqual(wb["サマリ"]["C4"].value, 9789547)
        self.assertEqual(wb["iOS"].cell(row=4, column=5).value, 10)
        self.assertEqual(wb["Android"].cell(row=6, column=5).value, 30)

    def test_freeze_panes_preserved(self):
        self._generate()
        wb = load_workbook(self.output_path)
        self.assertEqual(wb["iOS"].freeze_panes, "D4")
        self.assertEqual(wb["Android"].freeze_panes, "D4")
        self.assertEqual(wb["全体"].freeze_panes, "I4")

    def test_zip_and_xml_are_valid(self):
        self._generate()
        with zipfile.ZipFile(self.output_path) as zf:
            self.assertIsNone(zf.testzip())
            for name in zf.namelist():
                if name.endswith(".xml") or name.endswith(".rels"):
                    etree.fromstring(zf.read(name))  # raises on malformed XML

    def test_reopens_cleanly_with_openpyxl(self):
        self._generate()
        wb = load_workbook(self.output_path)
        self.assertEqual(
            set(wb.sheetnames),
            {"サマリ", "iOS", "Android", "全体", "作品別", "作品別_2"},
        )


if __name__ == "__main__":
    unittest.main()
