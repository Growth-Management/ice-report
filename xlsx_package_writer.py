"""Package-preserving OOXML writer for XLSX templates.

Why this exists (see docs/jumpplus-ad-revenue-report.md, "openpyxl root
cause"): openpyxl's `load_workbook()` -> `save()` round trip silently drops
OOXML parts it does not model -- `xl/connections.xml`, `xl/queryTables/*`,
`customXml/*`, table relationship files, `xl/calcChain.xml`, and others.
Templates that use Power Query (queryTable-backed Excel Tables) lose the
part that says *which* connection/query a table is bound to while the table
itself still declares `tableType="queryTable"`, which is exactly what Excel
Desktop's repair dialog reports as removed "external data ranges".

This module never round-trips the whole workbook through an object model.
It reads the template as a plain zip, edits only the specific worksheet and
table XML parts a report actually needs to change, and copies every other
zip entry through unmodified, byte-for-byte. Parts this module has no
business touching -- connections, queryTables, customXml, calcChain,
styles, sharedStrings, etc. -- are therefore never at risk of being altered
or dropped, regardless of what future templates add to them.

Use openpyxl freely elsewhere (template structure analysis, validation,
tests, Golden Master comparisons) -- just never as the writer for a
production XLSX this module hands back to Drive.
"""

from __future__ import annotations

import copy
import re
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
XML_NS = "http://www.w3.org/XML/1998/namespace"

_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


class XlsxPackageError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def _qn(ns_key: str, tag: str) -> str:
    return f"{{{NS[ns_key]}}}{tag}"


def _col_letter_to_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index


def _index_to_col_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _split_ref(ref: str) -> tuple[str, int]:
    match = _REF_RE.match(ref)
    if not match:
        raise XlsxPackageError("unparseable_cell_ref", ref=ref)
    return match.group(1), int(match.group(2))


def _normalize_part_path(path: str) -> str:
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


class XlsxPackage:
    """In-memory view of an XLSX zip package: original bytes for every part,
    plus a small cache of parsed XML trees for the parts this module has
    actually touched. Parts never fetched via `xml()` are written back
    exactly as read."""

    def __init__(self, parts: dict[str, bytes]) -> None:
        self._parts = parts
        self._trees: dict[str, etree._Element] = {}

    @classmethod
    def load(cls, source: Any) -> "XlsxPackage":
        with zipfile.ZipFile(source) as zf:
            parts = {name: zf.read(name) for name in zf.namelist()}
        return cls(parts)

    def has(self, name: str) -> bool:
        return name in self._parts

    def raw(self, name: str) -> bytes:
        return self._parts[name]

    def xml(self, name: str) -> etree._Element:
        if name not in self._trees:
            if name not in self._parts:
                raise XlsxPackageError("part_not_found", part=name)
            self._trees[name] = etree.fromstring(self._parts[name])
        return self._trees[name]

    def set_xml(self, name: str, root: etree._Element) -> None:
        self._trees[name] = root

    def save(self, destination: Any) -> None:
        if isinstance(destination, (str, Path)):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in self._parts.items():
                if name in self._trees:
                    data = etree.tostring(
                        self._trees[name], xml_declaration=True, encoding="UTF-8", standalone=True
                    )
                zf.writestr(name, data)


def _workbook_rels(pkg: XlsxPackage) -> dict[str, str]:
    root = pkg.xml("xl/_rels/workbook.xml.rels")
    return {rel.get("Id"): rel.get("Target") for rel in root.findall(_qn("rel", "Relationship"))}


def _sheet_name_to_part(pkg: XlsxPackage, sheet_name: str) -> str:
    root = pkg.xml("xl/workbook.xml")
    sheets_el = root.find(_qn("main", "sheets"))
    if sheets_el is not None:
        for sheet_el in sheets_el.findall(_qn("main", "sheet")):
            if sheet_el.get("name") == sheet_name:
                rid = sheet_el.get(_qn("r", "id"))
                target = _workbook_rels(pkg).get(rid)
                if not target:
                    raise XlsxPackageError("sheet_not_found", sheet=sheet_name)
                if target.startswith("/"):
                    return _normalize_part_path(target[1:])
                return _normalize_part_path(f"xl/{target}")
    raise XlsxPackageError("sheet_not_found", sheet=sheet_name)


def _part_rels_path(part_path: str) -> str:
    if "/" in part_path:
        dir_name, base_name = part_path.rsplit("/", 1)
        return f"{dir_name}/_rels/{base_name}.rels"
    return f"_rels/{part_path}.rels"


def _sheet_table_parts(pkg: XlsxPackage, sheet_part: str) -> list[str]:
    root = pkg.xml(sheet_part)
    table_parts_el = root.find(_qn("main", "tableParts"))
    if table_parts_el is None:
        return []
    rels_path = _part_rels_path(sheet_part)
    if not pkg.has(rels_path):
        return []
    rels_root = pkg.xml(rels_path)
    rel_map = {rel.get("Id"): rel.get("Target") for rel in rels_root.findall(_qn("rel", "Relationship"))}
    base_dir = sheet_part.rsplit("/", 1)[0] if "/" in sheet_part else ""
    resolved = []
    for table_part_el in table_parts_el.findall(_qn("main", "tablePart")):
        rid = table_part_el.get(_qn("r", "id"))
        target = rel_map.get(rid)
        if not target:
            continue
        if target.startswith("/"):
            resolved.append(_normalize_part_path(target[1:]))
        else:
            resolved.append(_normalize_part_path(f"{base_dir}/{target}" if base_dir else target))
    return resolved


def _extract_text(container_el: etree._Element) -> str:
    return "".join(t.text or "" for t in container_el.iter(_qn("main", "t")))


def _read_shared_strings(pkg: XlsxPackage) -> list[str]:
    if not pkg.has("xl/sharedStrings.xml"):
        return []
    root = pkg.xml("xl/sharedStrings.xml")
    return [_extract_text(si) for si in root.findall(_qn("main", "si"))]


def _cell_text(cell_el: etree._Element, shared_strings: list[str]) -> str:
    cell_type = cell_el.get("t")
    if cell_type == "s":
        v_el = cell_el.find(_qn("main", "v"))
        if v_el is None or v_el.text is None:
            return ""
        index = int(v_el.text)
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    if cell_type == "inlineStr":
        is_el = cell_el.find(_qn("main", "is"))
        return _extract_text(is_el) if is_el is not None else ""
    v_el = cell_el.find(_qn("main", "v"))
    return (v_el.text or "") if v_el is not None else ""


def _reorder_row_cells(row_el: etree._Element) -> None:
    cells = row_el.findall(_qn("main", "c"))

    def _key(cell_el: etree._Element) -> int:
        col_letter, _ = _split_ref(cell_el.get("r"))
        return _col_letter_to_index(col_letter)

    ordered = sorted(cells, key=_key)
    for cell_el in cells:
        row_el.remove(cell_el)
    for cell_el in ordered:
        row_el.append(cell_el)


def _find_cell(row_el: etree._Element, ref: str) -> etree._Element | None:
    for cell_el in row_el.findall(_qn("main", "c")):
        if cell_el.get("r") == ref:
            return cell_el
    return None


def _format_number(value: float) -> str:
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return repr(value)


def _set_cell_value(row_el: etree._Element, col_idx: int, row_num: int, value: Any) -> None:
    col_letter = _index_to_col_letter(col_idx)
    ref = f"{col_letter}{row_num}"
    cell_el = _find_cell(row_el, ref)
    if cell_el is None:
        cell_el = etree.SubElement(row_el, _qn("main", "c"))
        cell_el.set("r", ref)
        _reorder_row_cells(row_el)

    style = cell_el.get("s")
    for attr in list(cell_el.attrib):
        del cell_el.attrib[attr]
    cell_el.set("r", ref)
    if style is not None:
        cell_el.set("s", style)
    for child in list(cell_el):
        cell_el.remove(child)

    if value is None:
        return
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        # NaN/inf are not valid OOXML numeric content (pandas represents a
        # missing/nullable BigQuery numeric column as float NaN, not None);
        # treat the same as a missing value -> empty cell.
        return
    if isinstance(value, bool):
        cell_el.set("t", "b")
        v_el = etree.SubElement(cell_el, _qn("main", "v"))
        v_el.text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        v_el = etree.SubElement(cell_el, _qn("main", "v"))
        v_el.text = _format_number(value)
    else:
        cell_el.set("t", "inlineStr")
        is_el = etree.SubElement(cell_el, _qn("main", "is"))
        t_el = etree.SubElement(is_el, _qn("main", "t"))
        t_el.set(f"{{{XML_NS}}}space", "preserve")
        t_el.text = str(value)


def _clone_row_as(source_row_el: etree._Element | None, new_row_num: int, min_col: int, max_col: int) -> etree._Element:
    new_row_el = etree.Element(_qn("main", "row"))
    if source_row_el is not None:
        for key, val in source_row_el.attrib.items():
            new_row_el.set(key, val)
    new_row_el.set("r", str(new_row_num))
    if source_row_el is not None:
        source_cells: dict[int, etree._Element] = {}
        for cell_el in source_row_el.findall(_qn("main", "c")):
            col_letter, _ = _split_ref(cell_el.get("r"))
            source_cells[_col_letter_to_index(col_letter)] = cell_el
        for col_idx in range(min_col, max_col + 1):
            src = source_cells.get(col_idx)
            if src is None:
                continue
            new_cell = copy.deepcopy(src)
            new_cell.set("r", f"{_index_to_col_letter(col_idx)}{new_row_num}")
            new_row_el.append(new_cell)
    return new_row_el


def _renumber_row(row_el: etree._Element, new_row_num: int) -> etree._Element:
    new_row_el = copy.deepcopy(row_el)
    new_row_el.set("r", str(new_row_num))
    for cell_el in new_row_el.findall(_qn("main", "c")):
        col_letter, _ = _split_ref(cell_el.get("r"))
        cell_el.set("r", f"{col_letter}{new_row_num}")
    return new_row_el


def update_summary_value(pkg: XlsxPackage, sheet_name: str, value: int, *, label: str = "総計") -> None:
    """Writes `value` into the cell immediately to the right of the first
    `label` cell found scanning top-to-bottom, left-to-right within the
    first 25 rows / 6 columns -- the same heuristic
    jumpplus_ad_revenue_report._find_summary_total_cell uses, since several
    of these templates repeat the label for unrelated summary blocks below
    the revenue one."""
    sheet_part = _sheet_name_to_part(pkg, sheet_name)
    shared_strings = _read_shared_strings(pkg)
    root = pkg.xml(sheet_part)
    sheet_data = root.find(_qn("main", "sheetData"))
    if sheet_data is None:
        raise XlsxPackageError("sheet_data_not_found", sheet=sheet_name)

    rows = sorted(sheet_data.findall(_qn("main", "row")), key=lambda r: int(r.get("r")))

    target: tuple[etree._Element, str] | None = None
    for row_el in rows:
        row_num = int(row_el.get("r"))
        if row_num > 25:
            continue
        cells = sorted(
            row_el.findall(_qn("main", "c")),
            key=lambda c: _col_letter_to_index(_split_ref(c.get("r"))[0]),
        )
        for cell_el in cells:
            col_letter, _ = _split_ref(cell_el.get("r"))
            if _col_letter_to_index(col_letter) > 6:
                continue
            if _cell_text(cell_el, shared_strings).strip() == label:
                target_col = _col_letter_to_index(col_letter) + 1
                target = (row_el, f"{_index_to_col_letter(target_col)}{row_num}")
                break
        if target:
            break

    if target is None:
        raise XlsxPackageError("total_label_not_found", sheet=sheet_name, label=label)

    row_el, ref = target
    col_letter, row_num = _split_ref(ref)
    _set_cell_value(row_el, _col_letter_to_index(col_letter), row_num, int(value))
    pkg.set_xml(sheet_part, root)


def replace_detail_rows(
    pkg: XlsxPackage,
    sheet_name: str,
    headers: tuple[str, ...],
    rows: list[dict[str, Any]],
    *,
    required_headers: tuple[str, ...] = (),
) -> None:
    """Rewrites a detail sheet's single Excel Table so its data rows exactly
    match `rows`, writing only the columns named in `headers` (by header
    text, not position) and leaving every other column -- including
    Power-Query-only sheets this function is never called for at all --
    untouched. Any row past the table's original span (trailing template
    rows) is shifted to stay after the resized table instead of being
    overwritten."""
    sheet_part = _sheet_name_to_part(pkg, sheet_name)

    table_parts = _sheet_table_parts(pkg, sheet_part)
    if len(table_parts) != 1:
        raise XlsxPackageError("table_not_found", sheet=sheet_name, table_count=len(table_parts))
    table_part = table_parts[0]
    table_root = pkg.xml(table_part)

    ref = table_root.get("ref")
    start_ref, end_ref = ref.split(":")
    min_col_letter, min_row = _split_ref(start_ref)
    max_col_letter, max_row = _split_ref(end_ref)
    min_col = _col_letter_to_index(min_col_letter)
    max_col = _col_letter_to_index(max_col_letter)
    header_row = min_row
    totals_row_count = int(table_root.get("totalsRowCount", "0") or "0")
    last_data_row = max_row - totals_row_count
    current_data_row_count = last_data_row - header_row

    sheet_root = pkg.xml(sheet_part)
    sheet_data = sheet_root.find(_qn("main", "sheetData"))
    if sheet_data is None:
        raise XlsxPackageError("sheet_data_not_found", sheet=sheet_name)
    shared_strings = _read_shared_strings(pkg)

    row_index: dict[int, etree._Element] = {}
    for row_el in sheet_data.findall(_qn("main", "row")):
        row_index[int(row_el.get("r"))] = row_el

    header_row_el = row_index.get(header_row)
    if header_row_el is None:
        raise XlsxPackageError("header_row_not_found", sheet=sheet_name, row=header_row)

    header_map: dict[str, int] = {}
    for cell_el in header_row_el.findall(_qn("main", "c")):
        col_letter, _ = _split_ref(cell_el.get("r"))
        col_idx = _col_letter_to_index(col_letter)
        if col_idx < min_col or col_idx > max_col:
            continue
        text = _cell_text(cell_el, shared_strings).strip()
        if text:
            header_map[text] = col_idx

    missing_required = [h for h in required_headers if h in headers and h not in header_map]
    if missing_required:
        raise XlsxPackageError("missing_required_headers", sheet=sheet_name, missing=missing_required)

    write_columns = {h: header_map[h] for h in headers if h in header_map}

    style_source_row_num = last_data_row if current_data_row_count >= 1 else header_row
    style_source_row_el = row_index.get(style_source_row_num)

    target_row_count = len(rows)
    delta = target_row_count - current_data_row_count

    new_rows: list[etree._Element] = []
    for offset, row_values in enumerate(rows):
        new_row_num = header_row + 1 + offset
        new_row_el = _clone_row_as(style_source_row_el, new_row_num, min_col, max_col)
        for header, col_idx in write_columns.items():
            _set_cell_value(new_row_el, col_idx, new_row_num, row_values.get(header))
        new_rows.append(new_row_el)

    new_last_data_row = header_row + target_row_count
    new_max_row = new_last_data_row + totals_row_count

    totals_row_el = None
    if totals_row_count > 0:
        old_totals_row_num = last_data_row + 1
        old_totals_row = row_index.get(old_totals_row_num)
        if old_totals_row is not None:
            totals_row_el = _renumber_row(old_totals_row, new_last_data_row + 1)

    trailing_rows = [
        _renumber_row(row_index[r_num], r_num + delta) for r_num in sorted(row_index) if r_num > max_row
    ]
    kept_before = [row_index[r_num] for r_num in sorted(row_index) if r_num <= header_row]

    for child in list(sheet_data):
        sheet_data.remove(child)
    for row_el in kept_before:
        sheet_data.append(row_el)
    for row_el in new_rows:
        sheet_data.append(row_el)
    if totals_row_el is not None:
        sheet_data.append(totals_row_el)
    for row_el in trailing_rows:
        sheet_data.append(row_el)

    dimension_el = sheet_root.find(_qn("main", "dimension"))
    if dimension_el is not None and dimension_el.get("ref") and ":" in dimension_el.get("ref", ""):
        try:
            dim_start, dim_end = dimension_el.get("ref").split(":")
            dim_end_col, dim_end_row = _split_ref(dim_end)
            dimension_el.set("ref", f"{dim_start}:{dim_end_col}{dim_end_row + delta}")
        except XlsxPackageError:
            pass

    pkg.set_xml(sheet_part, sheet_root)

    new_ref = f"{min_col_letter}{header_row}:{max_col_letter}{new_max_row}"
    table_root.set("ref", new_ref)
    autofilter_el = table_root.find(_qn("main", "autoFilter"))
    if autofilter_el is not None:
        autofilter_el.set("ref", f"{min_col_letter}{header_row}:{max_col_letter}{new_last_data_row}")
    pkg.set_xml(table_part, table_root)


def sha256_of(source: Any, part_name: str) -> str:
    import hashlib

    with zipfile.ZipFile(source) as zf:
        return hashlib.sha256(zf.read(part_name)).hexdigest()


def list_parts(source: Any) -> list[str]:
    with zipfile.ZipFile(source) as zf:
        return zf.namelist()


def validate_preserved_parts(template_source: Any, output_source: Any, part_names: list[str]) -> dict[str, bool]:
    """For each of `part_names`, True if the part is present in both the
    template and the generated output with byte-identical content. Used by
    tests and by the manual production acceptance check -- never called from
    the write path itself."""
    results: dict[str, bool] = {}
    with zipfile.ZipFile(template_source) as template_zip, zipfile.ZipFile(output_source) as output_zip:
        template_names = set(template_zip.namelist())
        output_names = set(output_zip.namelist())
        for name in part_names:
            if name not in template_names or name not in output_names:
                results[name] = False
                continue
            results[name] = template_zip.read(name) == output_zip.read(name)
    return results
