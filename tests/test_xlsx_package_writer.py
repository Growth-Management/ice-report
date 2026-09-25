import hashlib
import sys
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

from lxml import etree
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table, TableColumn

import jumpplus_ad_revenue_report as report
import xlsx_package_writer as pkg_writer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xlsx_fixtures import (  # noqa: E402
    POWER_QUERY_PARTS,
    POWER_QUERY_PARTS_SINGLE,
    build_app2_like_template,
    build_video_reward_like_template,
    build_web_like_template,
)

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

    def _generate(self, *, ios=2, android=2, zentai=2, revenue_yen=9789547):
        def _rows(count):
            return [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"content-{i}",
                    "JDCN": f"jdcn-{i}",
                    "広告表示数": i * 10,
                    "作品名": f"work-{i}",
                    "コミックスJDCN": "comic-jdcn",
                    "コミックス巻数": 1,
                    "タイトルID": 900 + i,
                    "デジタルタイトル名": f"digital-title-{i}",
                    "作品ID": i,
                }
                for i in range(1, count + 1)
            ]

        detail_rows = {"iOS": _rows(ios), "Android": _rows(android), "全体": _rows(zentai)}
        detail_rows = report.add_work_summary_rows(
            report_type="app2", revenue_yen=revenue_yen, detail_rows=detail_rows
        )
        result = report.create_ad_revenue_workbook(
            report_type="app2",
            template_path=self.template_path,
            output_path=self.output_path,
            revenue_yen=revenue_yen,
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

    def test_query_table_sheets_now_get_python_computed_values(self):
        """作品別/作品別_2 are no longer left untouched: create_ad_revenue_workbook
        now writes the Python-computed work-summary rows into them (replacing
        what used to be Power Query), while their Power Query *metadata*
        (connections.xml, queryTables/*, the tableType="queryTable" marker)
        is untouched by this same call -- covered separately by the other
        tests in this class. Removing that metadata is a distinct migration
        step (xlsx_package_writer.remove_power_query_dependency), not
        something create_ad_revenue_workbook does at runtime."""
        self._generate(ios=3, android=3, zentai=3)
        wb = load_workbook(self.output_path)
        sakuhin = wb["作品別"]
        sakuhin_table = list(sakuhin.tables.values())[0]
        # 3 全体 rows, each a distinct work -> 3 作品別 groups
        self.assertEqual(sakuhin_table.ref, "A3:D6")
        self.assertIsNotNone(sakuhin.cell(row=4, column=1).value)  # 作品名
        self.assertIsNotNone(sakuhin.cell(row=4, column=4).value)  # 広告売上

        with zipfile.ZipFile(self.template_path) as zf:
            template_pkg = pkg_writer.XlsxPackage.load(self.template_path)
        sakuhin_part = pkg_writer._sheet_name_to_part(template_pkg, "作品別")
        table_part = pkg_writer._sheet_table_parts(template_pkg, sakuhin_part)[0]
        preserved = pkg_writer.validate_preserved_parts(
            self.template_path, self.output_path, list(POWER_QUERY_PARTS)
        )
        for part, ok in preserved.items():
            self.assertTrue(ok, f"{part} was not preserved byte-for-byte")
        with zipfile.ZipFile(self.output_path) as zf:
            table_xml = zf.read(table_part).decode("utf-8")
        self.assertIn('tableType="queryTable"', table_xml)  # metadata untouched here on purpose

    def test_read_only_parsed_parts_are_byte_for_byte_preserved(self):
        """Dirty-tracking regression: xml() is called (for lookup only) on
        workbook.xml.rels, sharedStrings.xml and the iOS sheet's own _rels
        part while resolving sheet names and table relationships -- none of
        those may be re-serialized on save() just because they were read,
        only the sheet/table parts actually passed to set_xml() should be.

        xl/workbook.xml is deliberately NOT in this list: create_ad_revenue_
        workbook() calls force_recalculation_on_load(), which intentionally
        sets calcPr's fullCalcOnLoad/forceFullCalc so Excel recomputes the
        Excel-native formulas this module never writes to (サマリ's per-
        platform totals, 著者還元額) instead of showing whatever the
        template's stale cached values were -- see the dedicated
        RecalculationOnLoadTests below."""
        self._generate()
        template_pkg = pkg_writer.XlsxPackage.load(self.template_path)
        ios_part = pkg_writer._sheet_name_to_part(template_pkg, "iOS")
        ios_rels_part = pkg_writer._part_rels_path(ios_part)

        parts_to_check = [
            "xl/_rels/workbook.xml.rels",
            "xl/sharedStrings.xml",
            ios_rels_part,
            *POWER_QUERY_PARTS,
        ]
        preserved = pkg_writer.validate_preserved_parts(self.template_path, self.output_path, parts_to_check)
        for part, ok in preserved.items():
            self.assertTrue(ok, f"{part} was not preserved byte-for-byte")

    def test_app2_full_scale_table_refs_match_corrected_golden_master(self):
        """2026-08 real row counts (9,548 per sheet): iOS/Android are 10
        columns (A3:J...), but 全体 is 13 columns (A3:M...) in the real
        official template -- F/G (広告売上/広告売上_原資50) and H (作品ID)
        are untouched placeholder columns this module never writes to."""
        result = self._generate(ios=9548, android=9548, zentai=9548)
        self.assertEqual(result["detail_row_count"], 9548 * 3)

        wb = load_workbook(self.output_path)
        self.assertEqual(wb["iOS"].tables["table_iOS"].ref, "A3:J9551")
        self.assertEqual(wb["Android"].tables["table_Android"].ref, "A3:J9551")
        self.assertEqual(wb["全体"].tables["table_全体"].ref, "A3:M9551")

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


class DecimalNumericCellTests(unittest.TestCase):
    """ad_revenue_work_summaries.py returns Decimal for 広告売上/
    広告売上_原資50/コイン消費割合/広告還元額; the writer must store these as
    real OOXML numeric cells (t absent/"n"), not inlineStr, or every one of
    those columns would come out as text in Excel."""

    def _write_and_reload(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            rows = [{h: (value if h == "広告表示数" else f"v-{h}") for h in report.AD_VIEW_DETAIL_HEADERS}]
            pkg_writer.replace_detail_rows(pkg, "全体", report.AD_VIEW_DETAIL_HEADERS, rows)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            with zipfile.ZipFile(out) as zf:
                sheet_xml = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
                raw_xmls = {n: zf.read(n) for n in sheet_xml}

            wb = load_workbook(out)
            cell = wb["全体"].cell(row=4, column=5)  # 広告表示数 column
            return cell, raw_xmls

    def test_decimal_cell_has_numeric_data_type(self):
        cell, _ = self._write_and_reload(Decimal("123.456"))
        self.assertEqual(cell.data_type, "n")

    def test_decimal_cell_has_no_inline_str_marker_in_raw_xml(self):
        # The template's own サマリ sheet legitimately has t="inlineStr" for
        # its own text header labels (unrelated to this test) -- only the
        # 全体 sheet's row 4 (where the Decimal was written) matters here.
        with tempfile.TemporaryDirectory() as tmp:
            path = _build_plain_template(Path(tmp), headers=report.AD_VIEW_DETAIL_HEADERS)
            pkg = pkg_writer.XlsxPackage.load(path)
            rows = [
                {h: (Decimal("123.456") if h == "広告表示数" else f"v-{h}") for h in report.AD_VIEW_DETAIL_HEADERS}
            ]
            pkg_writer.replace_detail_rows(pkg, "全体", report.AD_VIEW_DETAIL_HEADERS, rows)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)

            zentai_part = pkg_writer._sheet_name_to_part(pkg, "全体")
            with zipfile.ZipFile(out) as zf:
                data = zf.read(zentai_part)
            # 広告表示数 is the 5th header -> column E
            cell_xml = data.split(b'<c r="E4"')[1].split(b"</c>")[0]
            self.assertNotIn(b't="inlineStr"', cell_xml)

    def test_decimal_value_is_numeric_after_reload(self):
        cell, _ = self._write_and_reload(Decimal("123.456"))
        self.assertEqual(float(cell.value), 123.456)

    def test_large_decimal_does_not_use_scientific_notation(self):
        _, raw_xmls = self._write_and_reload(Decimal("12722390"))
        combined = b"".join(raw_xmls.values())
        self.assertNotIn(b"E+", combined)
        self.assertNotIn(b"e+", combined)

    def test_non_finite_decimal_is_empty_cell_not_text(self):
        cell, _ = self._write_and_reload(Decimal("NaN"))
        self.assertIsNone(cell.value)

    def test_app2_work_summary_ad_revenue_column_is_numeric(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_app2_like_template(Path(tmp) / "app2_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": "ec1",
                    "コンテンツID": 1,
                    "コンテンツ名": "c1",
                    "JDCN": "j1",
                    "広告表示数": 100,
                    "作品名": "work-1",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                    "タイトルID": 901,
                    "デジタルタイトル名": "dt1",
                    "作品ID": 1,
                }
            ]
            detail_rows = {"iOS": [], "Android": [], "全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="app2", revenue_yen=9789547, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="app2",
                template_path=path,
                output_path=output,
                revenue_yen=9789547,
                detail_rows=detail_rows,
            )
            wb = load_workbook(output)
            sakuhin = wb["作品別"]
            revenue_cell = sakuhin.cell(row=4, column=4)  # 広告売上 column
            self.assertEqual(revenue_cell.data_type, "n")

    def test_web_work_summary_ad_revenue_column_is_numeric(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_web_like_template(Path(tmp) / "web_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": "ec1",
                    "コンテンツID": 1,
                    "コンテンツ名": "c1",
                    "JDCN": "j1",
                    "広告表示数": 100,
                    "作品名": "work-1",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                    "タイトルID": 901,
                    "デジタルタイトル名": "dt1",
                }
            ]
            detail_rows = {"全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="web", revenue_yen=734139, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="web",
                template_path=path,
                output_path=output,
                revenue_yen=734139,
                detail_rows=detail_rows,
            )
            wb = load_workbook(output)
            sakuhin = wb["作品別"]
            revenue_cell = sakuhin.cell(row=4, column=4)  # 広告売上 column
            self.assertEqual(revenue_cell.data_type, "n")


class FormulaPreservationTests(unittest.TestCase):
    """Power Query is gone, but existing Excel formulas unrelated to Power
    Query must survive: APP_2's 全体 F/G, WEB's 全体 F, and video-reward's
    作品別 C/D are all Excel-native formulas in the real Golden Master, never
    written by this module (see jumpplus_ad_revenue_report.py's
    APP2_ZENTAI_EXTRA_HEADERS/WEB_ZENTAI_EXTRA_HEADERS/
    VIDEO_REWARD_WORK_SUMMARY_HEADERS comments)."""

    def _detail_row(self, i, headers, value_header):
        row = {h: f"v{i}-{h}" for h in headers}
        row[value_header] = i * 10
        return row

    def test_app2_zentai_f_and_g_formulas_survive_row_growth(self):
        from xlsx_fixtures import APP2_F_FORMULA, APP2_G_FORMULA

        with tempfile.TemporaryDirectory() as tmp:
            path = build_app2_like_template(Path(tmp) / "app2_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"c{i}",
                    "JDCN": f"j{i}",
                    "広告表示数": i * 10,
                    "作品名": f"work-{i}",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                    "タイトルID": 900 + i,
                    "デジタルタイトル名": f"dt{i}",
                    "作品ID": i,
                }
                for i in range(1, 4)
            ]
            detail_rows = {"iOS": [], "Android": [], "全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="app2", revenue_yen=9789547, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="app2",
                template_path=path,
                output_path=output,
                revenue_yen=9789547,
                detail_rows=detail_rows,
            )
            wb = load_workbook(output)
            ws = wb["全体"]
            for row in (4, 5, 6):
                self.assertEqual(ws.cell(row=row, column=6).value, APP2_F_FORMULA)  # F
                self.assertEqual(ws.cell(row=row, column=7).value, APP2_G_FORMULA)  # G
                self.assertIsInstance(ws.cell(row=row, column=8).value, int)  # H = 作品ID

    def test_web_zentai_f_formula_survives_row_growth(self):
        from xlsx_fixtures import WEB_F_FORMULA

        with tempfile.TemporaryDirectory() as tmp:
            path = build_web_like_template(Path(tmp) / "web_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"c{i}",
                    "JDCN": f"j{i}",
                    "広告表示数": i * 10,
                    "作品名": f"work-{i}",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                    "タイトルID": 900 + i,
                    "デジタルタイトル名": f"dt{i}",
                }
                for i in range(1, 4)
            ]
            detail_rows = {"全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="web", revenue_yen=734139, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="web",
                template_path=path,
                output_path=output,
                revenue_yen=734139,
                detail_rows=detail_rows,
            )
            wb = load_workbook(output)
            ws = wb["全体"]
            for row in (4, 5, 6):
                self.assertEqual(ws.cell(row=row, column=6).value, WEB_F_FORMULA)  # F

    def test_video_reward_sakuhin_c_and_d_formulas_survive_row_growth(self):
        from xlsx_fixtures import VIDEO_REWARD_C_FORMULA, VIDEO_REWARD_D_FORMULA

        with tempfile.TemporaryDirectory() as tmp:
            path = build_video_reward_like_template(Path(tmp) / "video_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"c{i}",
                    "JDCN": f"j{i}",
                    "コイン消費数": i * 100,
                    "作品名": f"work-{i}",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                }
                for i in range(1, 4)
            ]
            detail_rows = {"iOS": [], "Android": [], "全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="video-reward", revenue_yen=14017945, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="video-reward",
                template_path=path,
                output_path=output,
                revenue_yen=14017945,
                detail_rows=detail_rows,
            )
            wb = load_workbook(output)
            ws = wb["作品別"]
            for row in (4, 5, 6):
                self.assertEqual(ws.cell(row=row, column=3).value, VIDEO_REWARD_C_FORMULA)  # C
                self.assertEqual(ws.cell(row=row, column=4).value, VIDEO_REWARD_D_FORMULA)  # D
                self.assertIsInstance(ws.cell(row=row, column=1).value, str)  # A = 作品名 (Python)
                self.assertIsInstance(ws.cell(row=row, column=2).value, int)  # B = コイン消費数 (Python)


class FormulaMaterializationFullScaleTests(unittest.TestCase):
    """Excel Desktop acceptance found that calculatedColumnFormula alone
    isn't enough -- see xlsx_package_writer._set_formula_cell /
    force_recalculation_on_load. FormulaPreservationTests above checks a
    handful of rows; these check every single data row at real production
    scale (798/9,548/9,060), since a bug specific to the style-source row,
    the last row, or a boundary case would not show up with only 3 rows."""

    def _formula_and_cached_value(self, sheet_root, ref: str):
        col_letter = "".join(ch for ch in ref if ch.isalpha())
        row_num = ref[len(col_letter) :]
        row_el = sheet_root.find(f'{MAIN_NS}sheetData/{MAIN_NS}row[@r="{row_num}"]')
        self.assertIsNotNone(row_el, f"row {row_num} missing")
        cell_el = None
        for c in row_el.findall(f"{MAIN_NS}c"):
            if c.get("r") == ref:
                cell_el = c
                break
        self.assertIsNotNone(cell_el, f"cell {ref} missing")
        f_el = cell_el.find(f"{MAIN_NS}f")
        v_el = cell_el.find(f"{MAIN_NS}v")
        self.assertIsNotNone(f_el, f"{ref} has no <f> -- calculatedColumnFormula was not materialized")
        return f_el.text, (v_el.text if v_el is not None else None)

    def test_video_reward_798_rows_all_have_materialized_c_and_d_formulas(self):
        from xlsx_fixtures import VIDEO_REWARD_C_FORMULA, VIDEO_REWARD_D_FORMULA

        with tempfile.TemporaryDirectory() as tmp:
            path = build_video_reward_like_template(Path(tmp) / "video_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"c{i}",
                    "JDCN": f"j{i}",
                    "コイン消費数": i,
                    "作品名": f"work-{i:03d}",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                }
                for i in range(1, 799)
            ]
            detail_rows = {"iOS": [], "Android": [], "全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="video-reward", revenue_yen=14017945, detail_rows=detail_rows
            )
            self.assertEqual(len(detail_rows["作品別"]), 798)
            report.create_ad_revenue_workbook(
                report_type="video-reward",
                template_path=path,
                output_path=output,
                revenue_yen=14017945,
                detail_rows=detail_rows,
            )

            pkg = pkg_writer.XlsxPackage.load(output)
            sheet_part = pkg_writer._sheet_name_to_part(pkg, "作品別")
            sheet_root = pkg.xml(sheet_part)

            table = pkg.xml(pkg_writer._sheet_table_parts(pkg, sheet_part)[0])
            self.assertEqual(table.get("ref"), "A3:D801")

            expected_c = VIDEO_REWARD_C_FORMULA.lstrip("=")
            expected_d = VIDEO_REWARD_D_FORMULA.lstrip("=")
            for row_num in (4, 5, 402, 800, 801):  # first / near-first / middle / near-last / last
                c_formula, c_cached = self._formula_and_cached_value(sheet_root, f"C{row_num}")
                d_formula, d_cached = self._formula_and_cached_value(sheet_root, f"D{row_num}")
                self.assertEqual(c_formula, expected_c)
                self.assertEqual(d_formula, expected_d)
                self.assertIsNotNone(c_cached, f"C{row_num} has no cached <v>")
                self.assertIsNotNone(d_cached, f"D{row_num} has no cached <v>")
                float(c_cached)
                float(d_cached)

            # every single data row, not just the sampled ones above
            missing = []
            for row_num in range(4, 802):
                for col in ("C", "D"):
                    formula, cached = self._formula_and_cached_value(sheet_root, f"{col}{row_num}")
                    if formula is None or cached is None:
                        missing.append(f"{col}{row_num}")
            self.assertEqual(missing, [], f"{len(missing)} cells missing formula/cached value")

    def test_app2_9548_rows_all_have_materialized_f_and_g_formulas(self):
        from xlsx_fixtures import APP2_F_FORMULA, APP2_G_FORMULA

        with tempfile.TemporaryDirectory() as tmp:
            path = build_app2_like_template(Path(tmp) / "app2_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"c{i}",
                    "JDCN": f"j{i}",
                    "広告表示数": i,
                    "作品名": f"work-{i}",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                    "タイトルID": 900 + i,
                    "デジタルタイトル名": f"dt{i}",
                    "作品ID": i,
                }
                for i in range(1, 9549)
            ]
            detail_rows = {"iOS": [], "Android": [], "全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="app2", revenue_yen=9789547, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="app2",
                template_path=path,
                output_path=output,
                revenue_yen=9789547,
                detail_rows=detail_rows,
            )

            pkg = pkg_writer.XlsxPackage.load(output)
            sheet_part = pkg_writer._sheet_name_to_part(pkg, "全体")
            sheet_root = pkg.xml(sheet_part)
            table = pkg.xml(pkg_writer._sheet_table_parts(pkg, sheet_part)[0])
            self.assertEqual(table.get("ref"), "A3:M9551")

            expected_f = APP2_F_FORMULA.lstrip("=")
            expected_g = APP2_G_FORMULA.lstrip("=")
            missing = []
            for row_num in range(4, 9552):
                f_formula, f_cached = self._formula_and_cached_value(sheet_root, f"F{row_num}")
                g_formula, g_cached = self._formula_and_cached_value(sheet_root, f"G{row_num}")
                if f_formula != expected_f or f_cached is None:
                    missing.append(f"F{row_num}")
                if g_formula != expected_g or g_cached is None:
                    missing.append(f"G{row_num}")
            self.assertEqual(missing, [], f"{len(missing)} F/G cells missing formula/cached value")

            # H (作品ID) stays a plain Python value, never a formula
            for row_num in (4, 9551):
                row_el = sheet_root.find(f'{MAIN_NS}sheetData/{MAIN_NS}row[@r="{row_num}"]')
                h_cell = next(c for c in row_el.findall(f"{MAIN_NS}c") if c.get("r") == f"H{row_num}")
                self.assertIsNone(h_cell.find(f"{MAIN_NS}f"))
                self.assertIsNotNone(h_cell.find(f"{MAIN_NS}v"))

    def test_web_9060_rows_all_have_materialized_f_formula(self):
        from xlsx_fixtures import WEB_F_FORMULA

        with tempfile.TemporaryDirectory() as tmp:
            path = build_web_like_template(Path(tmp) / "web_template.xlsx")
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": f"ec{i}",
                    "コンテンツID": i,
                    "コンテンツ名": f"c{i}",
                    "JDCN": f"j{i}",
                    "広告表示数": i,
                    "作品名": f"work-{i}",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                    "タイトルID": 900 + i,
                    "デジタルタイトル名": f"dt{i}",
                }
                for i in range(1, 9061)
            ]
            detail_rows = {"全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="web", revenue_yen=734139, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="web",
                template_path=path,
                output_path=output,
                revenue_yen=734139,
                detail_rows=detail_rows,
            )

            pkg = pkg_writer.XlsxPackage.load(output)
            sheet_part = pkg_writer._sheet_name_to_part(pkg, "全体")
            sheet_root = pkg.xml(sheet_part)
            table = pkg.xml(pkg_writer._sheet_table_parts(pkg, sheet_part)[0])
            self.assertEqual(table.get("ref"), "A3:K9063")

            expected_f = WEB_F_FORMULA.lstrip("=")
            missing = []
            for row_num in range(4, 9064):
                f_formula, f_cached = self._formula_and_cached_value(sheet_root, f"F{row_num}")
                if f_formula != expected_f or f_cached is None:
                    missing.append(f"F{row_num}")
            self.assertEqual(missing, [], f"{len(missing)} F cells missing formula/cached value")


def _inject_synthetic_calc_chain(path: Path) -> None:
    """openpyxl doesn't write xl/calcChain.xml itself, but the real official
    templates do (confirmed: a handful of entries in the pristine template,
    hundreds once a real Golden Master's tables are fully populated) --
    inject a minimal one so tests can verify force_recalculation_on_load()
    actually removes it, not just no-ops because it was never there."""
    with zipfile.ZipFile(path) as zf:
        entries = {name: zf.read(name) for name in zf.namelist()}

    entries["xl/calcChain.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<c r="C4" i="1"/></calcChain>\n'
    ).encode("utf-8")

    workbook_rels = entries["xl/_rels/workbook.xml.rels"].decode("utf-8")
    calc_chain_rel = (
        '<Relationship Id="rIdCalcChainFixture" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain" '
        'Target="calcChain.xml"/>'
    )
    workbook_rels = workbook_rels.replace("</Relationships>", calc_chain_rel + "</Relationships>")
    entries["xl/_rels/workbook.xml.rels"] = workbook_rels.encode("utf-8")

    content_types = entries["[Content_Types].xml"].decode("utf-8")
    calc_chain_override = (
        '<Override PartName="/xl/calcChain.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"/>'
    )
    content_types = content_types.replace("</Types>", calc_chain_override + "</Types>")
    entries["[Content_Types].xml"] = content_types.encode("utf-8")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


class RecalculationOnLoadTests(unittest.TestCase):
    """The second Excel Desktop acceptance failure: サマリ's own SUM/SUBTOTAL/
    IFERROR formulas (never written by this module) showed 0 because their
    cached <v> was still whatever the near-empty template had, and nothing
    told Excel to recompute them on open. force_recalculation_on_load()
    (called once by create_ad_revenue_workbook) must set calcPr's
    fullCalcOnLoad/forceFullCalc and drop the template's now-stale
    calcChain.xml."""

    def test_calc_pr_forces_full_recalculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_video_reward_like_template(Path(tmp) / "video_template.xlsx")
            pkg = pkg_writer.XlsxPackage.load(path)
            pkg_writer.force_recalculation_on_load(pkg)
            wb_root = pkg.xml("xl/workbook.xml")
            calc_pr = wb_root.find(f"{MAIN_NS}calcPr")
            self.assertIsNotNone(calc_pr)
            self.assertEqual(calc_pr.get("fullCalcOnLoad"), "1")
            self.assertEqual(calc_pr.get("forceFullCalc"), "1")

    def test_stale_calc_chain_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_video_reward_like_template(Path(tmp) / "video_template.xlsx")
            _inject_synthetic_calc_chain(path)
            pkg = pkg_writer.XlsxPackage.load(path)
            self.assertTrue(pkg.has("xl/calcChain.xml"))
            pkg_writer.force_recalculation_on_load(pkg)
            out = Path(tmp) / "out.xlsx"
            pkg.save(out)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
                self.assertNotIn("xl/calcChain.xml", names)
                ct = zf.read("[Content_Types].xml").decode("utf-8")
                self.assertNotIn("calcChain", ct)
                rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
                self.assertNotIn("calcChain", rels)

    def test_end_to_end_generation_forces_recalc_and_drops_calc_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_video_reward_like_template(Path(tmp) / "video_template.xlsx")
            _inject_synthetic_calc_chain(path)
            output = Path(tmp) / "out.xlsx"
            zentai_rows = [
                {
                    "コンテンツID_Raise": "ec1",
                    "コンテンツID": 1,
                    "コンテンツ名": "c1",
                    "JDCN": "j1",
                    "コイン消費数": 10,
                    "作品名": "work-1",
                    "コミックスJDCN": "cj",
                    "コミックス巻数": 1,
                }
            ]
            detail_rows = {"iOS": [], "Android": [], "全体": zentai_rows}
            detail_rows = report.add_work_summary_rows(
                report_type="video-reward", revenue_yen=14017945, detail_rows=detail_rows
            )
            report.create_ad_revenue_workbook(
                report_type="video-reward",
                template_path=path,
                output_path=output,
                revenue_yen=14017945,
                detail_rows=detail_rows,
            )

            pkg = pkg_writer.XlsxPackage.load(output)
            wb_root = pkg.xml("xl/workbook.xml")
            calc_pr = wb_root.find(f"{MAIN_NS}calcPr")
            self.assertEqual(calc_pr.get("fullCalcOnLoad"), "1")
            self.assertEqual(calc_pr.get("forceFullCalc"), "1")
            self.assertFalse(pkg.has("xl/calcChain.xml"))


class WebPowerQueryMigrationTests(unittest.TestCase):
    """Item 4 of the review: WEB's real official template also carries
    connections.xml/queryTables/DataMashup/table-to-queryTable relationship
    (confirmed via the Google Drive connector's natural-language read of the
    real file: 作品別's table headers matched exactly). This session could
    not reliably retrieve the real template's raw bytes (two attempts at
    manual transcription of the connector's base64 output produced
    differently-corrupted zips both times), so this uses a
    structurally-faithful synthetic WEB template instead -- same sheets,
    real header text, single queryTable-backed 作品別, F=広告売上 formula in
    全体. remove_power_query_dependency() itself is already verified against
    the *real* app2/video-reward templates (see the module docstring and
    docs/jumpplus-ad-revenue-report.md)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.template_path = build_web_like_template(Path(self.tmp_dir.name) / "web_template.xlsx")
        self.output_path = Path(self.tmp_dir.name) / "web_pq_free.xlsx"

    def _convert(self):
        pkg = pkg_writer.XlsxPackage.load(self.template_path)
        removed = pkg_writer.remove_power_query_dependency(pkg)
        pkg.save(self.output_path)
        return removed

    def test_removes_connections_query_tables_and_data_mashup(self):
        removed = self._convert()
        self.assertEqual(len(removed["query_tables"]), 1)
        self.assertEqual(set(removed["connections"]), {"2"})
        self.assertEqual(removed["custom_xml"], ["customXml/item1.xml"])
        with zipfile.ZipFile(self.output_path) as zf:
            names = zf.namelist()
        for part in POWER_QUERY_PARTS_SINGLE:
            self.assertNotIn(part, names)

    def test_table_type_marker_stripped(self):
        self._convert()
        with zipfile.ZipFile(self.output_path) as zf:
            for name in zf.namelist():
                if name.startswith("xl/tables/table") and name.endswith(".xml"):
                    self.assertNotIn("queryTable", zf.read(name).decode("utf-8"))

    def test_no_dangling_relationships_or_content_types_overrides(self):
        self._convert()
        CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
        with zipfile.ZipFile(self.output_path) as zf:
            names = set(zf.namelist())
            ct_root = etree.fromstring(zf.read("[Content_Types].xml"))
            for el in ct_root.findall(f"{CT_NS}Override"):
                part = el.get("PartName").lstrip("/")
                self.assertIn(part, names)
            wb_rels = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in wb_rels.findall(f"{REL_NS}Relationship"):
                target = rel.get("Target")
                resolved = (
                    pkg_writer._normalize_part_path(target[1:])
                    if target.startswith("/")
                    else pkg_writer._normalize_part_path(f"xl/{target}")
                )
                self.assertIn(resolved, names)

    def test_zip_xml_valid_and_reopens(self):
        self._convert()
        with zipfile.ZipFile(self.output_path) as zf:
            self.assertIsNone(zf.testzip())
            for name in zf.namelist():
                if name.endswith(".xml") or name.endswith(".rels"):
                    etree.fromstring(zf.read(name))
        wb = load_workbook(self.output_path)
        self.assertEqual(set(wb.sheetnames), {"サマリ", "全体", "作品別"})

    def test_sakuhin_is_normal_excel_table_after_conversion(self):
        self._convert()
        wb = load_workbook(self.output_path)
        ws = wb["作品別"]
        table = list(ws.tables.values())[0]
        self.assertEqual(table.ref, "A3:D4")

    def test_zentai_f_calculated_column_formula_preserved_through_conversion(self):
        """Converting to Power-Query-free must not disturb 全体's own F
        calculatedColumnFormula -- it's a completely separate part
        (xl/tables/tableN.xml's tableColumns) from the Power Query metadata
        this function targets. The formula lives only in the table
        definition at this point (see _add_detail_sheet) -- the worksheet's
        own F4 cell is genuinely blank until a real write materializes it
        (test_converted_template_works_with_normal_runtime_write_path)."""
        from xlsx_fixtures import WEB_F_FORMULA

        self._convert()
        pkg = pkg_writer.XlsxPackage.load(self.output_path)
        ns = pkg_writer.NS
        for name in pkg.part_names():
            if name.startswith("xl/tables/table") and name.endswith(".xml"):
                root = pkg.xml(name)
                for col in root.iter(f"{{{ns['main']}}}tableColumn"):
                    if col.get("name") == "広告売上":
                        calc = col.find(f"{{{ns['main']}}}calculatedColumnFormula")
                        self.assertIsNotNone(calc)
                        self.assertEqual(calc.text, WEB_F_FORMULA.lstrip("="))
                        return
        self.fail("広告売上 tableColumn not found")

    def test_converted_template_works_with_normal_runtime_write_path(self):
        from xlsx_fixtures import WEB_F_FORMULA

        self._convert()
        zentai_rows = [
            {
                "コンテンツID_Raise": "ec1",
                "コンテンツID": 1,
                "コンテンツ名": "c1",
                "JDCN": "j1",
                "広告表示数": 100,
                "作品名": "work-1",
                "コミックスJDCN": "cj",
                "コミックス巻数": 1,
                "タイトルID": 901,
                "デジタルタイトル名": "dt1",
            }
        ]
        detail_rows = {"全体": zentai_rows}
        detail_rows = report.add_work_summary_rows(
            report_type="web", revenue_yen=734139, detail_rows=detail_rows
        )
        final_path = Path(self.tmp_dir.name) / "final.xlsx"
        result = report.create_ad_revenue_workbook(
            report_type="web",
            template_path=self.output_path,
            output_path=final_path,
            revenue_yen=734139,
            detail_rows=detail_rows,
        )
        self.assertEqual(result["detail_row_count"], 1)
        wb = load_workbook(final_path)
        self.assertEqual(wb["全体"]["F4"].value, WEB_F_FORMULA)


class RemovePowerQueryDependencyTests(unittest.TestCase):
    """xlsx_package_writer.remove_power_query_dependency: the one-time
    template migration step (not called at runtime by
    create_ad_revenue_workbook) that strips Power Query so the resulting
    template needs no Power Query refresh at all, matching the "Power Query
    廃止" acceptance criteria."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.template_path = build_app2_like_template(Path(self.tmp_dir.name) / "app2_template.xlsx")
        self.output_path = Path(self.tmp_dir.name) / "app2_pq_free.xlsx"

    def _convert(self):
        pkg = pkg_writer.XlsxPackage.load(self.template_path)
        removed = pkg_writer.remove_power_query_dependency(pkg)
        pkg.save(self.output_path)
        return removed

    def test_removes_both_query_tables_and_their_connections(self):
        removed = self._convert()
        self.assertEqual(len(removed["query_tables"]), 2)
        self.assertEqual(set(removed["connections"]), {"2", "3"})
        self.assertEqual(removed["custom_xml"], ["customXml/item1.xml"])

    def test_no_power_query_parts_remain_in_output(self):
        self._convert()
        with zipfile.ZipFile(self.output_path) as zf:
            names = zf.namelist()
        for part in POWER_QUERY_PARTS:
            self.assertNotIn(part, names)

    def test_table_type_and_field_id_markers_are_stripped(self):
        self._convert()
        with zipfile.ZipFile(self.output_path) as zf:
            for name in zf.namelist():
                if name.startswith("xl/tables/table") and name.endswith(".xml"):
                    xml = zf.read(name).decode("utf-8")
                    self.assertNotIn("queryTable", xml)

    def test_no_dangling_relationships_or_content_types_overrides(self):
        self._convert()
        CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
        with zipfile.ZipFile(self.output_path) as zf:
            names = set(zf.namelist())
            ct_root = etree.fromstring(zf.read("[Content_Types].xml"))
            for el in ct_root.findall(f"{CT_NS}Override"):
                part = el.get("PartName").lstrip("/")
                self.assertIn(part, names, f"dangling Content_Types Override -> {part}")
            wb_rels = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in wb_rels.findall(f"{REL_NS}Relationship"):
                target = rel.get("Target")
                resolved = (
                    pkg_writer._normalize_part_path(target[1:])
                    if target.startswith("/")
                    else pkg_writer._normalize_part_path(f"xl/{target}")
                )
                self.assertIn(resolved, names, f"dangling workbook.xml.rels Relationship -> {target}")

    def test_zip_and_xml_still_valid_and_reopens(self):
        self._convert()
        with zipfile.ZipFile(self.output_path) as zf:
            self.assertIsNone(zf.testzip())
            for name in zf.namelist():
                if name.endswith(".xml") or name.endswith(".rels"):
                    etree.fromstring(zf.read(name))
        wb = load_workbook(self.output_path)
        self.assertEqual(
            set(wb.sheetnames), {"サマリ", "iOS", "Android", "全体", "作品別", "作品別_2"}
        )

    def test_other_sheets_and_non_power_query_parts_are_untouched(self):
        self._convert()
        preserved = pkg_writer.validate_preserved_parts(
            self.template_path,
            self.output_path,
            ["xl/sharedStrings.xml", "xl/styles.xml", "xl/worksheets/sheet1.xml"],
        )
        for part, ok in preserved.items():
            self.assertTrue(ok, f"{part} was not preserved byte-for-byte")

    def test_can_then_be_used_normally_by_create_ad_revenue_workbook(self):
        """The converted, Power-Query-free template still works with the
        exact same runtime write path as before -- no special-casing needed
        once a template has been migrated."""
        self._convert()
        result = report.create_ad_revenue_workbook(
            report_type="app2",
            template_path=self.output_path,
            output_path=Path(self.tmp_dir.name) / "final.xlsx",
            revenue_yen=9789547,
            detail_rows={
                "iOS": [],
                "Android": [],
                "全体": [
                    {
                        "コンテンツID_Raise": "ec1",
                        "コンテンツID": 1,
                        "コンテンツ名": "c1",
                        "JDCN": "j1",
                        "広告表示数": 100,
                        "作品名": "work-1",
                        "コミックスJDCN": "cj1",
                        "コミックス巻数": 1,
                        "タイトルID": 901,
                        "デジタルタイトル名": "dt1",
                        "作品ID": 1,
                        "広告売上": 50,
                        "広告売上_原資50": 25,
                    }
                ],
                "作品別": [{"作品名": "work-1", "タイトルID": 901, "デジタルタイトル名": "dt1", "広告売上": 50}],
                "作品別_2": [{"作品ID": 1, "作品名": "work-1", "広告売上_原資50": 25}],
            },
        )
        self.assertEqual(result["detail_row_count"], 1)


if __name__ == "__main__":
    unittest.main()
