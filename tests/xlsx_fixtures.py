"""Builds synthetic XLSX templates for tests -- via openpyxl for the parts
it understands, then augmented with hand-written OOXML parts openpyxl has no
model for (xl/connections.xml, xl/queryTables/*, customXml/*, queryTable
relationships) so tests can verify xlsx_package_writer actually preserves
them. Test-only: production code never builds a package this way."""

from __future__ import annotations

import zipfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.worksheet.table import Table, TableColumn

QUERY_TABLE_XML_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<queryTable xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'name="{name}" connectionId="{connection_id}" autoFormatId="16" applyNumberFormats="0" '
    'applyBorderFormats="0" applyFontFormats="0" applyPatternFormats="0" applyAlignmentFormats="0" '
    'applyWidthHeightFormats="1"/>\n'
)

TABLE_RELS_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/queryTable" '
    'Target="../queryTables/{query_table_file}"/>'
    "</Relationships>\n"
)

# Real Power Query DataMashup parts are UTF-16, with the base64-encoded
# mini-OPC package (containing Formulas/Section1.m, the actual M code) as
# the element's text content. This stub doesn't need a decodable payload --
# only the <DataMashup> wrapper matters, since
# xlsx_package_writer._is_data_mashup_custom_xml only checks for that tag
# after a UTF-16 decode, exactly like the real official templates' own
# customXml/item1.xml (confirmed by decoding them directly).
CUSTOM_XML_ITEM = (
    '<?xml version="1.0" encoding="utf-16"?>'
    '<DataMashup sqmid="00000000-0000-0000-0000-000000000000" '
    'xmlns="http://schemas.microsoft.com/DataMashup">AAAAAAAA</DataMashup>'
).encode("utf-16")

CUSTOM_XML_ITEM_PROPS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ds:datastoreItem ds:itemID="{7B9A2F1A-0000-0000-0000-000000000001}" xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml"/>
"""

CUSTOM_XML_ITEM_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXmlProps" '
    'Target="itemProps1.xml"/>'
    "</Relationships>\n"
).encode("utf-8")

# The openpyxl version this repo pins (3.1.5) writes string cells as
# inlineStr and never produces xl/sharedStrings.xml at all -- unlike real
# Excel-authored templates, which normally do. Inject one so tests can
# verify it survives generation byte-for-byte (xlsx_package_writer reads it
# read-only via _read_shared_strings(), which must never mark it dirty).
SHARED_STRINGS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">
  <si><t>template-origin-shared-string</t></si>
</sst>
""".encode("utf-8")

# APP_2's real 全体 F/G formulas reference the サマリ sheet's 広告単価 block
# via structured references (=IFERROR(SUM(ROUND(広告売上[広告売上]*広告単価
# [[#This Row],[割合]],0))/広告表示数[[#Totals],[広告表示数]],"-") is the
# サマリ-level unit price formula this repo already confirmed against the
# real template -- the per-row 全体 formula multiplies that unit price by
# the row's own 広告表示数). This fixture doesn't need the exact real text,
# just A representative structured-reference formula, to prove the writer
# clones it verbatim rather than overwriting it with a Python value.
APP2_F_FORMULA = '=[@広告表示数]*広告単価!$C$17'
APP2_G_FORMULA = '=[@広告表示数]*広告単価!$C$18'
WEB_F_FORMULA = '=[@広告表示数]*広告単価!$C$17'
VIDEO_REWARD_C_FORMULA = '=[@コイン消費数]/SUBTOTAL(109,話データ_コイン消費数_作品別[コイン消費数])'
VIDEO_REWARD_D_FORMULA = '=著者還元額!$C$17*[@コイン消費割合]'


def _add_detail_sheet(
    wb: Workbook,
    name: str,
    headers: tuple[str, ...],
    *,
    freeze: str | None = None,
    formulas: dict[str, str] | None = None,
) -> "Worksheet":
    ws = wb.create_sheet(name)
    for col, header in enumerate(headers, start=1):
        ws.cell(row=3, column=col, value=header)
    for col in range(1, len(headers) + 1):
        ws.cell(row=4, column=col).number_format = "#,##0"
    if formulas:
        for header, formula in formulas.items():
            col = headers.index(header) + 1
            ws.cell(row=4, column=col).value = formula
    last_col_letter = ws.cell(row=3, column=len(headers)).column_letter
    table = Table(displayName=f"table_{name}".replace("　", "_"), ref=f"A3:{last_col_letter}4")
    table.tableColumns = [TableColumn(id=i, name=h) for i, h in enumerate(headers, start=1)]
    ws.add_table(table)
    if freeze:
        ws.freeze_panes = freeze
    return ws


def _table_file_for_display_name(entries: dict[str, bytes], display_name: str) -> str:
    for name, data in entries.items():
        if name.startswith("xl/tables/table") and display_name.encode("utf-8") in data:
            return name.rsplit("/", 1)[1]
    raise AssertionError(f"no table part found for displayName {display_name!r}")


def _inject_power_query_parts(path: Path, *, query_table_displays: list[str]) -> None:
    """Marks each sheet in `query_table_displays` (given as its Table's
    displayName) as queryTable-backed and injects the supporting OOXML parts
    (connections.xml, one xl/queryTables/queryTableN.xml + table
    relationship per entry, the DataMashup customXml, and a real
    sharedStrings.xml) -- exactly the parts an openpyxl round trip silently
    drops. Connection ids are assigned 2, 3, 4... in order, matching the
    real APP_2 template's numbering for its two queryTables."""
    with zipfile.ZipFile(path) as zf:
        entries = {name: zf.read(name) for name in zf.namelist()}

    connections = []
    for idx, display_name in enumerate(query_table_displays, start=1):
        conn_id = idx + 1
        table_file = _table_file_for_display_name(entries, display_name)
        xml = entries[f"xl/tables/{table_file}"].decode("utf-8")
        xml = xml.replace("<table ", '<table tableType="queryTable" ', 1)
        entries[f"xl/tables/{table_file}"] = xml.encode("utf-8")

        query_table_file = f"queryTable{idx}.xml"
        entries[f"xl/queryTables/{query_table_file}"] = QUERY_TABLE_XML_TEMPLATE.format(
            name=f"query{idx}", connection_id=conn_id
        ).encode("utf-8")
        entries[f"xl/tables/_rels/{table_file}.rels"] = TABLE_RELS_TEMPLATE.format(
            query_table_file=query_table_file
        ).encode("utf-8")
        connections.append(
            f'<connection id="{conn_id}" name="conn{conn_id}" type="5" refreshedVersion="8" background="1"/>'
        )

    entries["xl/connections.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<connections xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(connections)
        + "</connections>\n"
    ).encode("utf-8")

    entries["customXml/item1.xml"] = CUSTOM_XML_ITEM
    entries["customXml/itemProps1.xml"] = CUSTOM_XML_ITEM_PROPS
    entries["customXml/_rels/item1.xml.rels"] = CUSTOM_XML_ITEM_RELS
    entries["xl/sharedStrings.xml"] = SHARED_STRINGS_XML

    workbook_rels = entries["xl/_rels/workbook.xml.rels"].decode("utf-8")
    shared_strings_rel = (
        '<Relationship Id="rIdSharedStringsFixture" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
    )
    workbook_rels = workbook_rels.replace("</Relationships>", shared_strings_rel + "</Relationships>")
    entries["xl/_rels/workbook.xml.rels"] = workbook_rels.encode("utf-8")

    content_types = entries["[Content_Types].xml"].decode("utf-8")
    query_table_overrides = "".join(
        f'<Override PartName="/xl/queryTables/queryTable{idx}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.queryTable+xml"/>'
        for idx in range(1, len(query_table_displays) + 1)
    )
    extra_overrides = (
        '<Override PartName="/xl/connections.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.connections+xml"/>'
        + query_table_overrides
        + '<Override PartName="/customXml/item1.xml" ContentType="application/xml"/>'
        '<Override PartName="/customXml/itemProps1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.customXmlProperties+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    )
    content_types = content_types.replace("</Types>", extra_overrides + "</Types>")
    entries["[Content_Types].xml"] = content_types.encode("utf-8")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


AD_VIEW_HEADERS_10 = (
    "コンテンツID_Raise",
    "コンテンツID",
    "コンテンツ名",
    "JDCN",
    "広告表示数",
    "作品名",
    "コミックスJDCN",
    "コミックス巻数",
    "タイトルID",
    "デジタルタイトル名",
)


def build_app2_like_template(output_path: str | Path) -> Path:
    """A template shaped like the real APP_2 official template: サマリ +
    iOS/Android/全体 (the sheets jumpplus_ad_revenue_report writes) plus
    作品別/作品別_2 (Power-Query-backed, never written by this module) --
    with connections.xml/queryTables/customXml/table rels injected
    afterward, exactly the parts an openpyxl round trip would silently
    drop. 全体's F/G columns carry a representative Excel formula (as the
    real Golden Master does), which this module must never overwrite."""
    wb = Workbook()
    wb.remove(wb.active)

    summary = wb.create_sheet("サマリ")
    summary["A1"] = "広告売上"
    summary["B3"] = "OS区分"
    summary["C3"] = "広告売上"
    summary["B4"] = "総計"
    summary["C4"] = None

    _add_detail_sheet(wb, "iOS", AD_VIEW_HEADERS_10, freeze="D4")
    _add_detail_sheet(wb, "Android", AD_VIEW_HEADERS_10, freeze="D4")
    # Real APP_2 template's 全体 sheet is 13 columns (A-M): our first 5
    # written headers, then F/G (広告売上/広告売上_原資50, Excel formulas in
    # the real Golden Master) and H (作品ID, a plain Python-written value) --
    # then our remaining 5 written headers at I-M.
    zentai_headers = AD_VIEW_HEADERS_10[:5] + ("広告売上", "広告売上_原資50", "作品ID") + AD_VIEW_HEADERS_10[5:]
    _add_detail_sheet(
        wb,
        "全体",
        zentai_headers,
        freeze="I4",
        formulas={"広告売上": APP2_F_FORMULA, "広告売上_原資50": APP2_G_FORMULA},
    )

    # 作品別=A3:D4 (4 cols) / 作品別_2=A3:C4 (3 cols) in the real template --
    # real header text confirmed by direct inspection of the official
    # templates' 話データ_広告売上_作品別/_2 tables.
    sakuhin = _add_detail_sheet(wb, "作品別", ("作品名", "タイトルID", "デジタルタイトル名", "広告売上"))
    sakuhin2 = _add_detail_sheet(wb, "作品別_2", ("作品ID", "作品名", "広告売上_原資50"))

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)

    _inject_power_query_parts(
        out_path, query_table_displays=[f"table_{sakuhin.title}", f"table_{sakuhin2.title}"]
    )
    return out_path


def build_web_like_template(output_path: str | Path) -> Path:
    """A template shaped like the real WEB official template: サマリ + 全体
    (11 columns: our 10 written headers plus F=広告売上, an Excel formula in
    the real Golden Master this module must never overwrite) + 作品別
    (Power-Query-backed, single queryTable -- confirmed by inspecting the
    real template's actual header row via a natural-language read, since the
    raw binary could not be reliably retrieved in this session; see
    docs/jumpplus-ad-revenue-report.md)."""
    wb = Workbook()
    wb.remove(wb.active)

    summary = wb.create_sheet("サマリ")
    summary["A1"] = "広告売上"
    summary["B3"] = "OS区分"
    summary["C3"] = "広告売上"
    summary["B4"] = "総計"
    summary["C4"] = None

    zentai_headers = AD_VIEW_HEADERS_10[:5] + ("広告売上",) + AD_VIEW_HEADERS_10[5:]
    _add_detail_sheet(wb, "全体", zentai_headers, formulas={"広告売上": WEB_F_FORMULA})

    sakuhin = _add_detail_sheet(wb, "作品別", ("作品名", "タイトルID", "デジタルタイトル名", "広告売上"))

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)

    _inject_power_query_parts(out_path, query_table_displays=[f"table_{sakuhin.title}"])
    return out_path


VIDEO_REWARD_HEADERS_8 = (
    "コンテンツID_Raise",
    "コンテンツID",
    "コンテンツ名",
    "JDCN",
    "コイン消費数",
    "作品名",
    "コミックスJDCN",
    "コミックス巻数",
)


def build_video_reward_like_template(output_path: str | Path) -> Path:
    """A template shaped like the real video-reward official template:
    サマリ + iOS/Android/全体 (8 columns each) + 作品別 (Power-Query-backed,
    single queryTable; 作品名/コイン消費数 are the M-code's own output, while
    コイン消費割合/広告還元額 are Excel-native formulas this module must
    preserve rather than overwrite with the Decimal values it still
    computes internally for Golden Master comparison)."""
    wb = Workbook()
    wb.remove(wb.active)

    summary = wb.create_sheet("サマリ")
    summary["A1"] = "広告売上"
    summary["B3"] = "区分"
    summary["C3"] = "広告売上"
    summary["B4"] = "総計"
    summary["C4"] = None

    _add_detail_sheet(wb, "iOS", VIDEO_REWARD_HEADERS_8, freeze="D4")
    _add_detail_sheet(wb, "Android", VIDEO_REWARD_HEADERS_8, freeze="D4")
    _add_detail_sheet(wb, "全体", VIDEO_REWARD_HEADERS_8, freeze="D4")

    sakuhin = _add_detail_sheet(
        wb,
        "作品別",
        ("作品名", "コイン消費数", "コイン消費割合", "広告還元額"),
        formulas={"コイン消費割合": VIDEO_REWARD_C_FORMULA, "広告還元額": VIDEO_REWARD_D_FORMULA},
    )

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)

    _inject_power_query_parts(out_path, query_table_displays=[f"table_{sakuhin.title}"])
    return out_path


POWER_QUERY_PARTS = (
    "xl/connections.xml",
    "xl/queryTables/queryTable1.xml",
    "xl/queryTables/queryTable2.xml",
    "customXml/item1.xml",
    "customXml/itemProps1.xml",
    "customXml/_rels/item1.xml.rels",
)

# For single-queryTable templates (web, video-reward) -- only one
# xl/queryTables/queryTableN.xml exists.
POWER_QUERY_PARTS_SINGLE = (
    "xl/connections.xml",
    "xl/queryTables/queryTable1.xml",
    "customXml/item1.xml",
    "customXml/itemProps1.xml",
    "customXml/_rels/item1.xml.rels",
)
