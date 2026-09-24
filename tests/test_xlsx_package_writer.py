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
        workbook.xml, workbook.xml.rels, sharedStrings.xml and the iOS
        sheet's own _rels part while resolving sheet names and table
        relationships -- none of those may be re-serialized on save() just
        because they were read, only the sheet/table parts actually passed
        to set_xml() should be."""
        self._generate()
        template_pkg = pkg_writer.XlsxPackage.load(self.template_path)
        ios_part = pkg_writer._sheet_name_to_part(template_pkg, "iOS")
        ios_rels_part = pkg_writer._part_rels_path(ios_part)

        parts_to_check = [
            "xl/workbook.xml",
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
