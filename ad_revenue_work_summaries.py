"""Pure Python replacements for the Power Query 作品別/作品別_2 aggregations
that used to live inside the ad-revenue XLSX templates. See
docs/jumpplus-ad-revenue-report.md ("Power Query廃止") for how each of these
was verified against the actual Power Query M source decoded from the real
official templates' customXml DataMashup part, not just against Excel's
displayed behavior.

None of these functions touch BigQuery, openpyxl, or the OOXML writer --
each takes plain dicts/lists and returns plain dicts/lists, so they can be
unit tested against Golden Master numbers with no file involved.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def round_yen(amount: Decimal) -> int:
    """Matches Excel's ROUND(x, 0): round half away from zero. Python's
    own round() and Decimal's default quantize both round half-to-even,
    which disagrees with Excel on exact .5 yen boundaries."""
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def unit_price(revenue_yen: int, total_view_count: int, *, ratio: Decimal = Decimal("1")) -> Decimal:
    """The real APP_2 template computes this via a native Excel array
    formula this module never touches (サマリ sheet's 広告単価 block):
    `=IFERROR(SUM(ROUND(広告売上[広告売上]*広告単価[[#This Row],[割合]],0))
    /広告表示数[[#Totals],[広告表示数]],"-")` -- i.e. ROUND(revenue*ratio,0)
    divided by the total view count, extracted directly from the official
    template and confirmed against the real 2026-08 Golden Master's 原資50
    numerator (ROUND(9789547*0.5, 0) == 4894774)."""
    if total_view_count == 0:
        return Decimal(0)
    numerator = round_yen(Decimal(revenue_yen) * ratio)
    return Decimal(numerator) / Decimal(total_view_count)


def ad_revenue_for_row(view_count: int, price: Decimal) -> Decimal:
    return Decimal(view_count) * price


def _tiebreak(row: dict[str, Any], headers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple("" if row.get(h) is None else str(row.get(h)) for h in headers)


def _sort_desc_by_value(rows: list[dict[str, Any]], value_header: str, tiebreak_headers: tuple[str, ...]) -> None:
    rows.sort(key=lambda r: (-r[value_header], _tiebreak(r, tiebreak_headers)))


def build_app2_work_summary(zentai_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """作品別: group by (作品名, タイトルID, デジタルタイトル名), SUM(広告売上).
    Matches the real Power Query M code exactly:
        Table.Group(源, {"作品名","タイトルID","デジタルタイトル名"},
                    {{"広告売上", each List.Sum([広告売上]), type number}})
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for row in zentai_rows:
        key = (row.get("作品名"), row.get("タイトルID"), row.get("デジタルタイトル名"))
        group = groups.setdefault(
            key,
            {"作品名": key[0], "タイトルID": key[1], "デジタルタイトル名": key[2], "広告売上": Decimal(0)},
        )
        group["広告売上"] += row.get("広告売上") or Decimal(0)
    result = list(groups.values())
    _sort_desc_by_value(result, "広告売上", ("作品名", "タイトルID", "デジタルタイトル名"))
    return result


def build_app2_work_summary_50(zentai_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """作品別_2: group by (作品ID, 作品名), SUM(広告売上_原資50). Matches the
    real Power Query M code exactly:
        Table.Group(源, {"作品ID","作品名"},
                    {{"広告売上_原資50", each List.Sum([広告売上_原資50]), type nullable number}})
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for row in zentai_rows:
        key = (row.get("作品ID"), row.get("作品名"))
        group = groups.setdefault(key, {"作品ID": key[0], "作品名": key[1], "広告売上_原資50": Decimal(0)})
        group["広告売上_原資50"] += row.get("広告売上_原資50") or Decimal(0)
    result = list(groups.values())
    _sort_desc_by_value(result, "広告売上_原資50", ("作品名",))
    return result


def build_web_work_summary(zentai_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """作品別 for WEB: same grouping shape as APP_2's 作品別 (group by
    作品名/タイトルID/デジタルタイトル名, SUM 広告売上)."""
    return build_app2_work_summary(zentai_rows)


def build_video_reward_work_summary(
    zentai_rows: list[dict[str, Any]], *, revenue_yen: int, author_rate: Decimal = Decimal("0.15")
) -> list[dict[str, Any]]:
    """作品別: group by 作品名, SUM(コイン消費数); then per work
    コイン消費割合 = 作品コイン消費数 / 全体コイン消費数, 広告還元額 =
    著者還元額(ROUND(revenue_yen*author_rate, 0)) * コイン消費割合. Matches
    the real Power Query M code exactly:
        Table.Group(源, {"作品名"},
                    {{"コイン消費数", each List.Sum([コイン消費数]), type number}})
    (コイン消費割合/広告還元額 are not part of the M query itself -- the
    real template's 作品別 sheet has no live formula for them either, so
    this module computes both directly instead of relying on Excel to.)
    """
    coin_by_work: dict[Any, int] = {}
    for row in zentai_rows:
        name = row.get("作品名")
        coin_by_work[name] = coin_by_work.get(name, 0) + int(row.get("コイン消費数") or 0)

    total_coin = sum(coin_by_work.values())
    author_amount = round_yen(Decimal(revenue_yen) * author_rate)

    result = []
    for name, coin in coin_by_work.items():
        share = Decimal(coin) / Decimal(total_coin) if total_coin else Decimal(0)
        result.append(
            {
                "作品名": name,
                "コイン消費数": coin,
                "コイン消費割合": share,
                "広告還元額": author_amount * share,
            }
        )
    result.sort(key=lambda r: (-r["コイン消費数"], "" if r["作品名"] is None else str(r["作品名"])))
    return result
