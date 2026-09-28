"""Template-specific XLSX writers for the ジャンプ＋コイン出納レポート
(jumpplus-coin-ledger). All three builders use xlsx_package_writer's
package-preserving approach -- never an openpyxl load()->save() round trip
of the production workbook -- so styles, column widths, freeze panes, zoom,
Table totals rows and every part this module never edits are copied through
byte-for-byte.

Template structure (official Drive templates, analysed 2026-09-28, see
docs/jumpplus-coin-ledger-report.md "Template structure"):

App (8 sheets)
- 出納: three fixed blocks 合計 / Apple / Google, header row with A=コイン種別
  at rows 3 / 15 / 27, coin rows 4-9 / 16-21 / 28-33, 総計 row 10 / 22 / 34.
  Only B-E (発行 / 繰越 / 消費 / システム調整) are written; F-H (残高 /
  当月消費率 / 繰越込消費率) and the 総計 row stay template formulas.
- サマリ / サマリ (Apple) / サマリ (Google): Excel Tables サマリ_Total (A:O,
  with 種別) / サマリ_Apple / サマリ_Google (A:N), each with a SUBTOTAL
  totals row, pre-sized with blank formatted rows -> resized to the works.
- 有料話消費コイン（Apple/Google）, 有料巻消費コイン（Apple/Google）: one
  18-column Table each (話明細_* / 巻明細_*), frozen at E4.
WEB (4 sheets): 出納 (one block, rows 4-9, 総計 row 10), サマリ, 有料話消費ポイント,
有料巻消費ポイント -- same layout with ポイント wording.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import xlsx_package_writer as pkg_writer
from jumpplus_coin_ledger_report import (
    COIN_TYPES,
    CoinLedgerReportError,
    SourceData,
    content_rows_for,
)

# ---------------------------------------------------------------------------
# 出納
# ---------------------------------------------------------------------------

LEDGER_SHEET = "出納"
LEDGER_VALUE_COLUMNS = (
    ("B", "issue_coins_m"),  # 発行
    ("C", "carried_over_coins_m"),  # 繰越（※前月以前）
    ("D", "use_coins_m"),  # 消費
    ("E", "cancellation_coins_m"),  # システム調整※棚卸し
)
LEDGER_HEADER_TEXTS = {
    "B": "発行",
    "C": "繰越（※前月以前）",
    "D": "消費",
    "E": "システム調整※棚卸し",
    "F": "残高",
}
# Row labels in template order, aligned with COIN_TYPES.
APP_COIN_LABELS = ("有償コイン", "購入お得コイン", "無償広告コイン", "無償ボーナスコイン", "贈答用購入コイン", "動画リワード広告コイン")
WEB_COIN_LABELS = (
    "有償ポイント",
    "購入お得ポイント",
    "無償広告ポイント",
    "無償ボーナスポイント",
    "贈答用購入ポイント",
    "動画リワード広告ポイント",
)


@dataclass(frozen=True)
class LedgerBlock:
    title_ref: str
    title: str
    header_row: int
    first_coin_row: int
    platforms: tuple[str, ...]


APP_LEDGER_BLOCKS = (
    LedgerBlock("A1", "コイン出納（合計）", 3, 4, ("iOS", "And")),
    LedgerBlock("A13", "コイン出納（Apple）", 15, 16, ("iOS",)),
    LedgerBlock("A25", "コイン出納（Google）", 27, 28, ("And",)),
)
WEB_LEDGER_BLOCKS = (LedgerBlock("A1", "ポイント出納", 3, 4, ("Web",)),)


def _num(value: Any) -> int | float | None:
    """BigQuery NUMERIC -> Decimal; pandas may give float/NaN. Whole numbers
    are written as int (no trailing .0 in the cell)."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else value
    return value


def _sum(values) -> int | float:
    total: int | float = 0
    for v in values:
        n = _num(v)
        if n is not None:
            total += n
    return total


def ledger_block_values(ledger: list[dict[str, Any]], platforms: tuple[str, ...]) -> list[dict[str, int | float]]:
    """One dict per coin type (COIN_TYPES order) with the four input columns
    plus balance_coins_m (used only for the balance check), summed over
    `platforms`."""
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger:
        if row.get("app_pf") in platforms:
            by_type[row.get("coin_type")].append(row)
    result = []
    for coin_type in COIN_TYPES:
        rows = by_type.get(coin_type) or []
        if len(rows) != len(platforms):
            raise CoinLedgerReportError("ledger_grain_mismatch", status_code=500)
        values = {field: _sum(r.get(field) for r in rows) for _, field in LEDGER_VALUE_COLUMNS}
        values["balance_coins_m"] = _sum(r.get("balance_coins_m") for r in rows)
        result.append(values)
    return result


def _check_balance(values: dict[str, int | float]) -> None:
    # Mirrors the template's own 残高 formula: F = C + B - D + E.
    computed = (
        values["carried_over_coins_m"] + values["issue_coins_m"] - values["use_coins_m"] + values["cancellation_coins_m"]
    )
    if computed != values["balance_coins_m"]:
        raise CoinLedgerReportError("ledger_balance_mismatch", status_code=500)


def write_ledger_sheet(
    package: pkg_writer.XlsxPackage,
    *,
    ledger: list[dict[str, Any]],
    blocks: tuple[LedgerBlock, ...],
    coin_labels: tuple[str, ...],
    require_type_header: bool,
) -> None:
    for block in blocks:
        expected: dict[str, str] = {block.title_ref: block.title}
        for col, text in LEDGER_HEADER_TEXTS.items():
            expected[f"{col}{block.header_row}"] = text
        if require_type_header:
            expected[f"A{block.header_row}"] = "コイン種別"
        for offset, label in enumerate(coin_labels):
            expected[f"A{block.first_coin_row + offset}"] = label
        expected[f"A{block.first_coin_row + len(coin_labels)}"] = "総計"
        actual = pkg_writer.read_cell_texts(package, LEDGER_SHEET, list(expected))
        if any(actual[ref].strip() != text for ref, text in expected.items()):
            raise CoinLedgerReportError("ledger_template_layout_mismatch", status_code=500)

        cells: dict[str, Any] = {}
        for offset, values in enumerate(ledger_block_values(ledger, block.platforms)):
            _check_balance(values)
            row = block.first_coin_row + offset
            for col, field in LEDGER_VALUE_COLUMNS:
                cells[f"{col}{row}"] = values[field]
        try:
            pkg_writer.write_fixed_cells(package, LEDGER_SHEET, cells)
        except pkg_writer.XlsxPackageError as exc:
            raise CoinLedgerReportError("ledger_template_layout_mismatch", status_code=500) from exc


# ---------------------------------------------------------------------------
# サマリ (work-level summary tables)
# ---------------------------------------------------------------------------

SUMMARY_COMPONENTS = (
    ("有償", "pay_coins_total"),
    ("購入お得", "pay_bonus_coins_total"),
    ("広告", "free_ad_coins_total"),
    ("無償", "free_bonus_coins_total"),
    ("贈答", "pay_gift_coins_total"),
    ("動画リワード広告", "reward_video_ad_coin_count"),
)
PURCHASE_TYPE_PREFIX = {"episode": "話", "book": "巻"}


def summary_headers(unit: str, *, with_type: bool) -> tuple[str, ...]:
    headers = ["作品名", f"{unit}消費合計"]
    for purchase_type in ("episode", "book"):
        for label, _ in SUMMARY_COMPONENTS:
            headers.append(f"{PURCHASE_TYPE_PREFIX[purchase_type]}\n{label}{unit}消費数")
    if with_type:
        headers.append("種別")
    return tuple(headers)


def _work_key(row: dict[str, Any]) -> str:
    name = row.get("ex_work_name")
    if name is None or (isinstance(name, float) and name != name):
        return ""
    return str(name)


def summary_work_names(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({_work_key(r) for r in rows})


def build_summary_rows(
    rows: list[dict[str, Any]], *, unit: str, works: list[str], with_type: bool
) -> list[dict[str, Any]]:
    """One row per work in `works` (every work of the file, so the per-
    platform sheets list the same works as the total sheet, with zeros where
    a platform had no consumption). コイン消費合計 is SUM(total_use_coins);
    the 12 component columns are the per purchase_type sums. Sorted by
    消費合計 descending, then 作品名."""
    totals: dict[str, int | float] = defaultdict(int)
    components: dict[str, dict[str, int | float]] = defaultdict(lambda: defaultdict(int))
    comic_types: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        key = _work_key(row)
        totals[key] += _sum([row.get("total_use_coins")])
        prefix = PURCHASE_TYPE_PREFIX.get(row.get("purchase_type"))
        if prefix is None:
            continue
        for label, field in SUMMARY_COMPONENTS:
            components[key][f"{prefix}\n{label}{unit}消費数"] += _sum([row.get(field)])
        comic_type = row.get("ex_comic_type")
        if isinstance(comic_type, str) and comic_type:
            comic_types[key][comic_type] += 1

    headers = summary_headers(unit, with_type=with_type)
    result = []
    for work in works:
        out: dict[str, Any] = {"作品名": work, f"{unit}消費合計": totals.get(work, 0)}
        for header in headers[2:14]:
            out[header] = components[work].get(header, 0)
        if with_type:
            counts = comic_types.get(work)
            out["種別"] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if counts else None
        result.append(out)
    result.sort(key=lambda r: (-r[f"{unit}消費合計"], r["作品名"]))
    return result


# ---------------------------------------------------------------------------
# 明細 (per content detail tables)
# ---------------------------------------------------------------------------


def detail_headers(unit: str, purchase_type: str) -> tuple[str, ...]:
    tail = ("コミックスJDCN", "コミックス巻数") if purchase_type == "episode" else ("雑誌", "種別")
    return (
        "コンテンツID_Raise",
        "コンテンツID",
        "コンテンツ名",
        "JDCN",
        f"価格（{unit}）",
        "DL数",
        f"消費{unit}",
        "有償",
        "購入お得",
        "無償広告",
        "無償ボーナス",
        "贈答",
        "動画リワード広告",
        "配信開始日",
        "備考",
        "作品名",
    ) + tail


def _date_text(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "date") and callable(getattr(value, "date")):  # pandas Timestamp
        try:
            return value.date().isoformat()
        except Exception:
            return value
    if isinstance(value, float) and value != value:
        return None
    return value


def _text(value: Any) -> Any:
    if value is None or (isinstance(value, float) and value != value):
        return None
    return value


def detail_row(record: dict[str, Any], *, unit: str, purchase_type: str) -> dict[str, Any]:
    row = {
        "コンテンツID_Raise": _text(record.get("prefixed_id")),
        "コンテンツID": _text(record.get("v2_content_id_token")),
        "コンテンツ名": _text(record.get("name")),
        "JDCN": _text(record.get("jdcn")),
        f"価格（{unit}）": _num(record.get("unit_price")),
        "DL数": _num(record.get("download_count")),
        f"消費{unit}": _num(record.get("total_use_coins")),
        "有償": _num(record.get("pay_coins_total")),
        "購入お得": _num(record.get("pay_bonus_coins_total")),
        "無償広告": _num(record.get("free_ad_coins_total")),
        "無償ボーナス": _num(record.get("free_bonus_coins_total")),
        "贈答": _num(record.get("pay_gift_coins_total")),
        "動画リワード広告": _num(record.get("reward_video_ad_coin_count")),
        "配信開始日": _date_text(record.get("ex_sales_start_date")),
        "備考": _text(record.get("ex_note")),
        "作品名": _text(record.get("ex_work_name")),
    }
    if purchase_type == "episode":
        row["コミックスJDCN"] = _text(record.get("ex_comics_jdcn"))
        row["コミックス巻数"] = _num(record.get("ex_episode_package_no"))
    else:
        row["雑誌"] = _text(record.get("ex_magazine"))
        row["種別"] = _text(record.get("ex_file_type"))
    return row


# ---------------------------------------------------------------------------
# Table writing
# ---------------------------------------------------------------------------


def _replace_table(package: pkg_writer.XlsxPackage, sheet_name: str, headers: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    # An Excel Table needs at least one data row; an empty month for a
    # sheet keeps a single blank (still formatted) row instead of producing
    # a header-only ref Excel would flag for repair.
    try:
        pkg_writer.replace_detail_rows(package, sheet_name, headers, rows or [{}], required_headers=headers)
    except pkg_writer.XlsxPackageError as exc:
        code = {
            "sheet_not_found": "template_sheet_not_found",
            "table_not_found": "template_table_not_found",
            "missing_required_headers": "template_header_missing",
        }.get(exc.code)
        if code:
            raise CoinLedgerReportError(code, status_code=500) from exc
        raise


def _finish(package: pkg_writer.XlsxPackage, output_path: str | Path) -> None:
    pkg_writer.sync_filter_database_names(package)
    pkg_writer.force_recalculation_on_load(package)
    package.save(output_path)


# ---------------------------------------------------------------------------
# Builders (called by jumpplus_coin_ledger_report.build_workbooks)
# ---------------------------------------------------------------------------

APP_SUMMARY_SHEETS = (
    ("サマリ", (31, 32), True),
    ("サマリ (Apple)", (31,), False),
    ("サマリ (Google)", (32,), False),
)
APP_DETAIL_SHEETS = (
    ("有料話消費コイン（Apple）", 31, "episode"),
    ("有料話消費コイン（Google）", 32, "episode"),
    ("有料巻消費コイン（Apple）", 31, "book"),
    ("有料巻消費コイン（Google）", 32, "book"),
)
WEB_SUMMARY_SHEET = "サマリ"
WEB_DETAIL_SHEETS = (
    ("有料話消費ポイント", 101, "episode"),
    ("有料巻消費ポイント", 101, "book"),
)


def _rows_for_apps(content: list[dict[str, Any]], app_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    wanted = set(app_ids)
    return [r for r in content if r.get("app_id") is not None and int(r["app_id"]) in wanted]


def build_app_workbook(*, template_path: Path, output_path: Path, target_month: date, source: SourceData) -> dict[str, int]:
    package = pkg_writer.XlsxPackage.load(template_path)
    write_ledger_sheet(
        package, ledger=source.ledger, blocks=APP_LEDGER_BLOCKS, coin_labels=APP_COIN_LABELS, require_type_header=True
    )

    counts: dict[str, int] = {}
    app_rows = _rows_for_apps(source.content, (31, 32))
    works = summary_work_names(app_rows)
    for sheet_name, app_ids, with_type in APP_SUMMARY_SHEETS:
        rows = build_summary_rows(_rows_for_apps(source.content, app_ids), unit="コイン", works=works, with_type=with_type)
        _replace_table(package, sheet_name, summary_headers("コイン", with_type=with_type), rows)
        counts[sheet_name] = len(rows)

    for sheet_name, app_id, purchase_type in APP_DETAIL_SHEETS:
        records = content_rows_for(source.content, app_id=app_id, purchase_type=purchase_type)
        rows = [detail_row(r, unit="コイン", purchase_type=purchase_type) for r in records]
        _replace_table(package, sheet_name, detail_headers("コイン", purchase_type), rows)
        counts[sheet_name] = len(rows)

    _finish(package, output_path)
    return counts


def build_web_workbook(*, template_path: Path, output_path: Path, target_month: date, source: SourceData) -> dict[str, int]:
    package = pkg_writer.XlsxPackage.load(template_path)
    # The WEB template's 出納 header row has no "コイン種別" label in A3.
    write_ledger_sheet(
        package, ledger=source.ledger, blocks=WEB_LEDGER_BLOCKS, coin_labels=WEB_COIN_LABELS, require_type_header=False
    )

    counts: dict[str, int] = {}
    web_rows = _rows_for_apps(source.content, (101,))
    rows = build_summary_rows(web_rows, unit="ポイント", works=summary_work_names(web_rows), with_type=True)
    _replace_table(package, WEB_SUMMARY_SHEET, summary_headers("ポイント", with_type=True), rows)
    counts[WEB_SUMMARY_SHEET] = len(rows)

    for sheet_name, app_id, purchase_type in WEB_DETAIL_SHEETS:
        records = content_rows_for(source.content, app_id=app_id, purchase_type=purchase_type)
        rows = [detail_row(r, unit="ポイント", purchase_type=purchase_type) for r in records]
        _replace_table(package, sheet_name, detail_headers("ポイント", purchase_type), rows)
        counts[sheet_name] = len(rows)

    _finish(package, output_path)
    return counts


PRODUCT_HEADERS = (
    ("コンテンツID_Raise", "prefixed_id"),
    ("コンテンツID", "v2_content_id_token"),
    ("コンテンツ名", "name"),
    ("作品名", "work_title"),
    ("著者名", "author_name"),
    ("JDCN", "jdcn"),
    ("価格（コイン）", "price_in_coin"),
    ("配信開始日", "ex_sales_start_date"),
    ("コミックスJDCN", "ex_comics_jdcn"),
    ("コミックス巻数", "ex_episode_package_no"),
)
_PRODUCT_NUMERIC_FIELDS = {"price_in_coin", "ex_episode_package_no"}


def product_row(record: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for header, field in PRODUCT_HEADERS:
        value = record.get(field)
        if field in _PRODUCT_NUMERIC_FIELDS:
            out[header] = _num(value)
        elif field == "ex_sales_start_date":
            out[header] = _date_text(value)
        else:
            out[header] = _text(value)
    return out


def _single_table_sheet(package: pkg_writer.XlsxPackage) -> str:
    wb_root = package.xml("xl/workbook.xml")
    sheets = wb_root.find(pkg_writer._qn("main", "sheets"))
    with_table = []
    for sheet_el in sheets.findall(pkg_writer._qn("main", "sheet")):
        name = sheet_el.get("name")
        if pkg_writer._sheet_table_parts(package, pkg_writer._sheet_name_to_part(package, name)):
            with_table.append(name)
    if len(with_table) != 1:
        raise CoinLedgerReportError("template_table_not_found", status_code=500)
    return with_table[0]


def build_product_workbook(*, template_path: Path, output_path: Path, target_month: date, source: SourceData) -> dict[str, int]:
    """話売商品_一覧: the current episode master (raise_master_contents_works,
    content_type='episode'), not purchase history -- so the file lists every
    sellable episode, independent of target_month's sales."""
    package = pkg_writer.XlsxPackage.load(template_path)
    sheet_name = _single_table_sheet(package)
    rows = [product_row(r) for r in source.product_master]
    _replace_table(package, sheet_name, tuple(h for h, _ in PRODUCT_HEADERS), rows)
    _finish(package, output_path)
    return {sheet_name: len(rows)}
