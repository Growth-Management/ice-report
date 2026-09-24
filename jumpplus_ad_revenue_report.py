from __future__ import annotations

import copy
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import bigquery
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.worksheet import Worksheet

import ad_revenue_work_summaries as work_summaries

# ---------------------------------------------------------------------------
# Report identity / defaults
# ---------------------------------------------------------------------------

AD_REVENUE_SPREADSHEET_ID_DEFAULT = "188iTZsN46tYQKc2ILi96ZH--coLFhUN-F9YDFIFCG70"
AD_REVENUE_SHEET_RANGE_DEFAULT = "data!A1:D1000"
AD_REVENUE_CONFIRMED_TABLE_DEFAULT = "jumpplus-4a5f4.dataset_exdata_tables.ad_revenue_confirmed_monthly"

DEFAULT_COIN_CONTENT_TABLE = "jumpplus-4a5f4.dataset_datamart_tables.report_plus_monthly_coin_content_report"
DEFAULT_AD_VIEW_TABLE = "jumpplus-4a5f4.dataset_datamart_tables.report_plus_monthly_ad_view"
DEFAULT_SERVICE_NAME = "J_PLUS"

DEFAULT_OUTPUT_FOLDER_ID = "1jxC2AZ6eeDKx1wVr88kf4Ilw86FWTART"

SUMMARY_SHEET = "サマリ"

VIDEO_REWARD_DETAIL_HEADERS = (
    "コンテンツID_Raise",
    "コンテンツID",
    "コンテンツ名",
    "JDCN",
    "コイン消費数",
    "作品名",
    "コミックスJDCN",
    "コミックス巻数",
)
# Headers confirmed by direct inspection of the real iOS/Android detail sheets
# (production Drive files, 2026-08 period). "全体" sheets add extra columns of
# their own (e.g. 作品ID, 広告売上 formula columns) that this module never
# writes to -- see write_detail_sheet()'s header_map-based, missing-header-
# tolerant design below.
AD_VIEW_DETAIL_HEADERS = (
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
# Headers that must exist on a detail sheet for a write to proceed; the rest of
# each *_DETAIL_HEADERS tuple is written only if present, so unconfirmed
# trailing columns (see docs/jumpplus-ad-revenue-report.md "Status / open
# items") degrade gracefully instead of crashing generation.
_REQUIRED_DETAIL_HEADERS = (
    "コンテンツID_Raise",
    "コンテンツID",
    "コンテンツ名",
    "JDCN",
    "作品名",
)

# Extra columns written only to the "全体" sheet (never iOS/Android), feeding
# the Python-side 作品別/作品別_2 aggregations that replace what used to be
# Power Query. See ad_revenue_work_summaries.py and
# docs/jumpplus-ad-revenue-report.md ("Power Query廃止").
APP2_ZENTAI_EXTRA_HEADERS = ("広告売上", "広告売上_原資50", "作品ID")
WEB_ZENTAI_EXTRA_HEADERS = ("広告売上",)

# 作品別/作品別_2 sheet headers -- confirmed by direct inspection of the real
# official templates' Excel Tables (not guessed): APP_2's 話データ_広告売上_作品別
# (A3:D4), 話データ_広告売上_作品別_2 (A3:C4), video-reward's
# 話データ_コイン消費数_作品別 (A3:D4).
APP2_WORK_SUMMARY_HEADERS = ("作品名", "タイトルID", "デジタルタイトル名", "広告売上")
APP2_WORK_SUMMARY_50_HEADERS = ("作品ID", "作品名", "広告売上_原資50")
WEB_WORK_SUMMARY_HEADERS = ("作品名", "タイトルID", "デジタルタイトル名", "広告売上")
VIDEO_REWARD_WORK_SUMMARY_HEADERS = ("作品名", "コイン消費数", "コイン消費割合", "広告還元額")


@dataclass(frozen=True)
class WorkSummarySheetSpec:
    sheet_name: str
    headers: tuple[str, ...]


@dataclass(frozen=True)
class AdRevenueReportSpec:
    report_id: str
    report_name: str
    revenue_column: str
    file_prefix: str
    template_env: str
    default_template_file_id: str
    detail_sheets: tuple[str, ...]
    detail_headers: tuple[str, ...]
    value_header: str  # the per-row count column: "コイン消費数" or "広告表示数"
    zentai_sheet: str = "全体"
    zentai_extra_headers: tuple[str, ...] = ()
    work_summary_sheets: tuple[WorkSummarySheetSpec, ...] = ()


# Official templates placed by システム管理室 in the "広告売上" Drive folder
# (1jxC2AZ6eeDKx1wVr88kf4Ilw86FWTART), confirmed 2026-09-18. Hardcoded as
# defaults -- same convention as thermae_romae_report.DEFAULT_THERMAE_TEMPLATE_FILE_ID
# and plus_browser_point_sales_report.DEFAULT_PLUS_TEMPLATE_FILE_ID -- with the
# corresponding env var still able to override per report type.
REPORT_SPECS: dict[str, AdRevenueReportSpec] = {
    "video-reward": AdRevenueReportSpec(
        report_id="video-reward",
        report_name="J+ 動画リワード広告売上",
        revenue_column="video_reward_yen",
        file_prefix="J+_動画リワード広告売上_",
        template_env="AD_REVENUE_VIDEO_REWARD_TEMPLATE_FILE_ID",
        default_template_file_id="1MtCimmJ9MjEjd0XW977kKME1sr82OP38",
        # Confirmed by opening the official template (2026-09-18): サマリ's
        # コイン消費数 block sums live from 話データ_iOS[コイン消費数] and
        # 話データ_Android[コイン消費数] via formula, so -- exactly like
        # app2 -- the iOS/Android sheets must be populated too, not just 全体.
        detail_sheets=("iOS", "Android", "全体"),
        detail_headers=VIDEO_REWARD_DETAIL_HEADERS,
        value_header="コイン消費数",
        work_summary_sheets=(WorkSummarySheetSpec("作品別", VIDEO_REWARD_WORK_SUMMARY_HEADERS),),
    ),
    "app2": AdRevenueReportSpec(
        report_id="app2",
        report_name="J+ 広告売上_APP_2（奥付広告）",
        revenue_column="app_footer_ad_yen",
        file_prefix="J+_広告売上_APP_2_",
        template_env="AD_REVENUE_APP2_TEMPLATE_FILE_ID",
        default_template_file_id="1Q1d4Mp-5iQFBwo3wnzmUa1mQPxPdvL1L",
        detail_sheets=("iOS", "Android", "全体"),
        detail_headers=AD_VIEW_DETAIL_HEADERS,
        value_header="広告表示数",
        zentai_extra_headers=APP2_ZENTAI_EXTRA_HEADERS,
        work_summary_sheets=(
            WorkSummarySheetSpec("作品別", APP2_WORK_SUMMARY_HEADERS),
            WorkSummarySheetSpec("作品別_2", APP2_WORK_SUMMARY_50_HEADERS),
        ),
    ),
    "web": AdRevenueReportSpec(
        report_id="web",
        report_name="J+ 広告売上_WEB",
        revenue_column="web_ad_yen",
        file_prefix="J+_広告売上_WEB_",
        template_env="AD_REVENUE_WEB_TEMPLATE_FILE_ID",
        default_template_file_id="1LJ72durOS1ekPO79GtDFdrJX23Vr8je-",
        detail_sheets=("全体",),
        detail_headers=AD_VIEW_DETAIL_HEADERS,
        value_header="広告表示数",
        zentai_extra_headers=WEB_ZENTAI_EXTRA_HEADERS,
        work_summary_sheets=(WorkSummarySheetSpec("作品別", WEB_WORK_SUMMARY_HEADERS),),
    ),
}


class AdRevenueReportError(Exception):
    def __init__(self, code: str, message: str | None = None, *, status_code: int = 400, **details: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code
        self.details = details


# ---------------------------------------------------------------------------
# Date helpers (mirrors plus_browser_point_sales_report: Asia/Tokyo, not the
# container's UTC clock, since Cloud Scheduler can fire close to the JST day
# boundary)
# ---------------------------------------------------------------------------


def tokyo_today() -> date:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


def previous_month_first(today: date | None = None) -> date:
    today = today or tokyo_today()
    first_this_month = today.replace(day=1)
    previous_month_last = first_this_month - timedelta(days=1)
    return previous_month_last.replace(day=1)


def parse_target_month(value: str | None = None, *, today: date | None = None) -> date:
    if not value:
        return previous_month_first(today)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise AdRevenueReportError("invalid_target_month", "target_month must be YYYY-MM-DD") from exc
    if parsed.day != 1:
        raise AdRevenueReportError("invalid_target_month", "target_month must be the first day of month")
    return parsed


def output_file_name(report_type: str, target_month: date, generated_date: date) -> str:
    spec = REPORT_SPECS[report_type]
    return f"{spec.file_prefix}{target_month:%Y}年{target_month:%m}月期_{generated_date:%y%m%d}.xlsx"


# ---------------------------------------------------------------------------
# Config accessors (env-overridable, matching thermae/plus report conventions)
# ---------------------------------------------------------------------------


def confirmed_revenue_table() -> str:
    return os.environ.get("AD_REVENUE_CONFIRMED_TABLE", AD_REVENUE_CONFIRMED_TABLE_DEFAULT)


def coin_content_table() -> str:
    return os.environ.get("AD_REVENUE_COIN_CONTENT_TABLE", DEFAULT_COIN_CONTENT_TABLE)


def ad_view_table() -> str:
    return os.environ.get("AD_REVENUE_AD_VIEW_TABLE", DEFAULT_AD_VIEW_TABLE)


def service_name_filter() -> str:
    return os.environ.get("AD_REVENUE_SERVICE_NAME", DEFAULT_SERVICE_NAME)


def default_output_folder_id() -> str:
    return os.environ.get("AD_REVENUE_OUTPUT_FOLDER_ID", DEFAULT_OUTPUT_FOLDER_ID)


def template_file_id(report_type: str) -> str:
    spec = REPORT_SPECS[report_type]
    return os.environ.get(spec.template_env, spec.default_template_file_id).strip()


def spreadsheet_id() -> str:
    return os.environ.get("AD_REVENUE_SPREADSHEET_ID", AD_REVENUE_SPREADSHEET_ID_DEFAULT)


def sheet_range() -> str:
    return os.environ.get("AD_REVENUE_SHEET_RANGE", AD_REVENUE_SHEET_RANGE_DEFAULT)


# ---------------------------------------------------------------------------
# Readiness check
# ---------------------------------------------------------------------------


@dataclass
class ReadinessResult:
    status: str  # "ready" | "waiting_detail" | "waiting_revenue"
    revenue_yen: int | None = None


def _detail_ready_query(report_type: str) -> str:
    if report_type == "video-reward":
        return (
            f"select 1 from `{coin_content_table()}` "
            "where purchase_date_month_jst = @target_month "
            "and service_name = @service_name "
            "and ws_ex_is_return_reward_video_ad_coin = true "
            "and purchase_type = 'episode' "
            "and app_pf in ('iOS', 'And') "
            "limit 1"
        )
    if report_type == "app2":
        return (
            f"select 1 from `{ad_view_table()}` "
            "where date_jst_month = @target_month and app_pf in ('iOS', 'And') "
            "and service_name = @service_name "
            "limit 1"
        )
    if report_type == "web":
        return (
            f"select 1 from `{ad_view_table()}` "
            "where date_jst_month = @target_month and app_pf = 'Web' "
            "and service_name = @service_name "
            "limit 1"
        )
    raise AdRevenueReportError("unknown_report_type", report_type=report_type)


def check_detail_ready(*, project_id: str, report_type: str, target_month: date) -> bool:
    query = _detail_ready_query(report_type)
    client = bigquery.Client(project=project_id)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_month", "DATE", target_month),
            bigquery.ScalarQueryParameter("service_name", "STRING", service_name_filter()),
        ]
    )
    rows = list(client.query(query, job_config=job_config).result())
    return len(rows) > 0


def fetch_confirmed_revenue(*, project_id: str, report_type: str, target_month: date) -> int | None:
    spec = REPORT_SPECS[report_type]
    client = bigquery.Client(project=project_id)
    query = (
        f"select {spec.revenue_column} as yen from `{confirmed_revenue_table()}` "
        "where target_month = @target_month limit 1"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("target_month", "DATE", target_month)]
    )
    rows = list(client.query(query, job_config=job_config).result())
    if not rows:
        return None
    value = rows[0]["yen"]
    return int(value) if value is not None else None


def check_readiness(*, project_id: str, report_type: str, target_month: date) -> ReadinessResult:
    if not check_detail_ready(project_id=project_id, report_type=report_type, target_month=target_month):
        return ReadinessResult(status="waiting_detail")
    revenue_yen = fetch_confirmed_revenue(project_id=project_id, report_type=report_type, target_month=target_month)
    if revenue_yen is None:
        return ReadinessResult(status="waiting_revenue")
    return ReadinessResult(status="ready", revenue_yen=revenue_yen)


# ---------------------------------------------------------------------------
# BigQuery detail queries
# ---------------------------------------------------------------------------


def run_coin_content_query(
    *, project_id: str, target_month: date, app_pfs: list[str], table: str | None = None
) -> list[dict[str, Any]]:
    """One row per content_id with a positive summed reward_video_ad_coin_count
    across the given app_pf values. Called with app_pfs=["iOS"] / ["And"] for
    the per-platform sheets and app_pfs=["iOS", "And"] for the combined 全体
    sheet (mirrors run_ad_view_query's app2 pattern -- Web is never included,
    see below).

    Filter conditions and the positive-sum exclusion were derived from the
    2026-08 Golden Master (the real, manually-produced production file for
    that month) rather than guessed:

    - `service_name = 'J_PLUS'` alone overcounts by exactly the sum of
      `ws_ex_is_return_reward_video_ad_coin = false` rows (366,242,450 vs the
      Golden Master's 365,253,010 -- a 989,440 gap that disappears once the
      flag filter is added).
    - `purchase_type = 'episode'` and `app_pf in ('iOS', 'And')` are also
      required: without them, `content_count` per platform (69,947 for iOS)
      overcounts the Golden Master's detail row count (59,614 for iOS) by
      thousands of `purchase_type = 'book'` / `app_pf = 'Web'` rows that carry
      zero reward-video coin count for the month but still exist as rows in
      the source table.
    - Even after those filters, some remaining content_ids still sum to zero
      for the month (reward-eligible but not actually viewed) and must be
      excluded from the detail list (`having reward_video_ad_coin_count > 0`,
      referencing the SELECT alias directly -- `having sum(...)` over an
      already-summed alias is rejected by BigQuery as an aggregation of an
      aggregation) -- the Golden
      Master's audited row counts (iOS 59,614 / Android 58,899 / combined
      61,699) only match with this filter applied.
    All four totals (iOS 250,864,974 / Android 114,388,036 / combined
    365,253,010) and all three row counts were reproduced exactly against
    live BigQuery data before this query was written this way -- see
    docs/jumpplus-ad-revenue-report.md's "Golden Master" section.

    Also fetches `ex_work_name` alongside `work_title`: the 作品別 sheet's
    Golden Master grouping (798 works, confirmed against the real
    2026-08 production file) matches `ex_work_name`, not `work_title`
    (804 distinct values) -- multiple `work_title` variants roll up to one
    `ex_work_name` (e.g. "ONE PIECE　第1部"/"第2部"/"第3部" -> "ONE PIECE").
    `work_title` is kept only for debugging; the report's own "作品名"
    column must be built from `ex_work_name` (see
    _coin_content_row_to_detail and docs/jumpplus-ad-revenue-report.md).
    """
    client = bigquery.Client(project=project_id)
    query = f"""
        select
            prefixed_id
            , content_id
            , any_value(name) as name
            , any_value(jdcn) as jdcn
            , sum(reward_video_ad_coin_count) as reward_video_ad_coin_count
            , any_value(work_title) as work_title
            , any_value(ex_work_name) as ex_work_name
            , any_value(ex_comics_jdcn) as ex_comics_jdcn
            , any_value(ex_episode_package_no) as ex_episode_package_no
        from `{table or coin_content_table()}`
        where purchase_date_month_jst = @target_month
            and service_name = @service_name
            and ws_ex_is_return_reward_video_ad_coin = true
            and purchase_type = 'episode'
            and app_pf in unnest(@app_pfs)
        group by prefixed_id, content_id
        having reward_video_ad_coin_count > 0
        order by content_id
    """
    query_parameters = [
        bigquery.ScalarQueryParameter("target_month", "DATE", target_month),
        bigquery.ScalarQueryParameter("service_name", "STRING", service_name_filter()),
        bigquery.ArrayQueryParameter("app_pfs", "STRING", app_pfs),
    ]
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    frame = client.query(query, job_config=job_config).to_dataframe()
    return frame.to_dict("records")


def run_ad_view_query(
    *, project_id: str, target_month: date, app_pfs: list[str], table: str | None = None
) -> list[dict[str, Any]]:
    """One row per content id, ad_view_count summed across the given app_pf
    values. Called with a single-element app_pfs for the iOS/Android/Web
    per-platform sheets, and with ["iOS", "And"] for APP_2's combined 全体
    sheet. Verified against production data for content id=1118985, 2026-08:
    app_pf='Web' -> ad_view_count 21668 and app_pf='iOS' -> 36680, both
    matching the live Drive template's 広告表示数 values exactly."""
    client = bigquery.Client(project=project_id)
    query = f"""
        select
            prefixed_id
            , id
            , any_value(name) as name
            , any_value(jdcn) as jdcn
            , sum(ad_view_count) as ad_view_count
            , any_value(ws_title) as ws_title
            , any_value(ex_comics_jdcn) as ex_comics_jdcn
            , any_value(ex_episode_package_no) as ex_episode_package_no
            , any_value(ex_episode_title_id) as ex_episode_title_id
            , any_value(ex_episode_title_name) as ex_episode_title_name
            , any_value(work_id) as work_id
        from `{table or ad_view_table()}`
        where date_jst_month = @target_month
            and app_pf in unnest(@app_pfs)
            and service_name = @service_name
        group by prefixed_id, id
        order by id
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_month", "DATE", target_month),
            bigquery.ArrayQueryParameter("app_pfs", "STRING", app_pfs),
            bigquery.ScalarQueryParameter("service_name", "STRING", service_name_filter()),
        ]
    )
    frame = client.query(query, job_config=job_config).to_dataframe()
    return frame.to_dict("records")


def _safe_int(value: Any) -> int | None:
    """int(value), but treats None and pandas' float NaN (which is not
    None, and which int() raises ValueError on) both as missing -- unlike
    id-type columns, 作品ID/work_id can legitimately be unlinked/null."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    return int(value)


def _coin_content_row_to_detail(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "コンテンツID_Raise": record.get("prefixed_id"),
        "コンテンツID": int(record["content_id"]) if record.get("content_id") is not None else None,
        "コンテンツ名": record.get("name"),
        "JDCN": record.get("jdcn"),
        "コイン消費数": int(record.get("reward_video_ad_coin_count") or 0),
        # 作品名 must come from ex_work_name, not work_title: the 作品別
        # sheet's Golden Master grouping (798 works) only matches
        # ex_work_name -- work_title has 804 distinct values because
        # several work_title variants (e.g. "ONE PIECE　第1部"/"第2部"/
        # "第3部") map to a single ex_work_name ("ONE PIECE"). Confirmed
        # against the real 2026-08 production file's per-work totals.
        "作品名": record.get("ex_work_name"),
        "コミックスJDCN": record.get("ex_comics_jdcn"),
        "コミックス巻数": record.get("ex_episode_package_no"),
    }


def _ad_view_row_to_detail(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "コンテンツID_Raise": record.get("prefixed_id"),
        "コンテンツID": int(record["id"]) if record.get("id") is not None else None,
        "コンテンツ名": record.get("name"),
        "JDCN": record.get("jdcn"),
        "広告表示数": int(record.get("ad_view_count") or 0),
        "作品名": record.get("ws_title"),
        "コミックスJDCN": record.get("ex_comics_jdcn"),
        "コミックス巻数": record.get("ex_episode_package_no"),
        "タイトルID": record.get("ex_episode_title_id"),
        "デジタルタイトル名": record.get("ex_episode_title_name"),
        "作品ID": _safe_int(record.get("work_id")),
    }


def fetch_detail_rows(*, project_id: str, report_type: str, target_month: date) -> dict[str, list[dict[str, Any]]]:
    if report_type == "video-reward":
        ios = run_coin_content_query(project_id=project_id, target_month=target_month, app_pfs=["iOS"])
        android = run_coin_content_query(project_id=project_id, target_month=target_month, app_pfs=["And"])
        combined = run_coin_content_query(project_id=project_id, target_month=target_month, app_pfs=["iOS", "And"])
        return {
            "iOS": [_coin_content_row_to_detail(r) for r in ios],
            "Android": [_coin_content_row_to_detail(r) for r in android],
            "全体": [_coin_content_row_to_detail(r) for r in combined],
        }
    if report_type == "app2":
        ios = run_ad_view_query(project_id=project_id, target_month=target_month, app_pfs=["iOS"])
        android = run_ad_view_query(project_id=project_id, target_month=target_month, app_pfs=["And"])
        combined = run_ad_view_query(project_id=project_id, target_month=target_month, app_pfs=["iOS", "And"])
        return {
            "iOS": [_ad_view_row_to_detail(r) for r in ios],
            "Android": [_ad_view_row_to_detail(r) for r in android],
            "全体": [_ad_view_row_to_detail(r) for r in combined],
        }
    if report_type == "web":
        records = run_ad_view_query(project_id=project_id, target_month=target_month, app_pfs=["Web"])
        return {"全体": [_ad_view_row_to_detail(r) for r in records]}
    raise AdRevenueReportError("unknown_report_type", report_type=report_type)


def add_work_summary_rows(
    *, report_type: str, revenue_yen: int, detail_rows: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Fills in the 作品別/作品別_2 replacements for what used to be Power
    Query (ad_revenue_work_summaries.py), and -- for app2/web -- the 全体
    sheet's own 広告売上/広告売上_原資50 per-row values those aggregations
    are computed from. See docs/jumpplus-ad-revenue-report.md
    ("Power Query廃止") for how the unit-price formula and each grouping was
    verified against the real official templates' own native Excel formula
    and decoded Power Query M source, not just guessed from the task
    description.

    Pure with respect to BigQuery/Drive: only touches the `detail_rows` dict
    already fetched by fetch_detail_rows(), returning a new dict (the input
    is not mutated) with the 全体 sheet's rows augmented in place and the
    work-summary sheets' rows added under their own sheet-name keys.
    """
    spec = REPORT_SPECS[report_type]
    if not spec.work_summary_sheets:
        return detail_rows

    augmented = dict(detail_rows)
    zentai_rows = [dict(row) for row in augmented.get(spec.zentai_sheet, [])]

    if report_type in ("app2", "web"):
        total_views = sum(int(row.get(spec.value_header) or 0) for row in zentai_rows)
        price = work_summaries.unit_price(revenue_yen, total_views)
        for row in zentai_rows:
            row["広告売上"] = work_summaries.ad_revenue_for_row(row.get(spec.value_header) or 0, price)

        if report_type == "app2":
            price_50 = work_summaries.unit_price(revenue_yen, total_views, ratio=Decimal("0.5"))
            for row in zentai_rows:
                row["広告売上_原資50"] = work_summaries.ad_revenue_for_row(
                    row.get(spec.value_header) or 0, price_50
                )
            augmented["作品別"] = work_summaries.build_app2_work_summary(zentai_rows)
            augmented["作品別_2"] = work_summaries.build_app2_work_summary_50(zentai_rows)
        else:
            augmented["作品別"] = work_summaries.build_web_work_summary(zentai_rows)

        augmented[spec.zentai_sheet] = zentai_rows
    elif report_type == "video-reward":
        augmented["作品別"] = work_summaries.build_video_reward_work_summary(zentai_rows, revenue_yen=revenue_yen)

    return augmented


# ---------------------------------------------------------------------------
# Excel workbook writing
# ---------------------------------------------------------------------------


def _find_summary_total_cell(ws: Worksheet):
    """Finds the yen amount cell for the report's grand total.

    Every one of the three templates places a "総計" label in column B (or C
    for 動画リワード's narrower first block) with the amount one cell to its
    right, and the revenue block is always the first such label top-to-bottom
    in the サマリ sheet (confirmed directly for APP_2/WEB; 動画リワード's
    コイン消費数/著者還元額 blocks below it also contain their own 総計 rows,
    so scanning top-to-bottom and taking the first match is what makes this
    correct for all three -- see docs/jumpplus-ad-revenue-report.md).
    """
    for row in ws.iter_rows(min_row=1, max_row=25, max_col=6):
        for cell in row:
            if str(cell.value or "").strip() == "総計":
                return ws.cell(row=cell.row, column=cell.column + 1)
    raise AdRevenueReportError("summary_total_cell_not_found", sheet=ws.title)


def write_summary_total(ws: Worksheet, revenue_yen: int) -> None:
    cell = _find_summary_total_cell(ws)
    cell.value = int(revenue_yen)


def _sheet_table(ws: Worksheet):
    tables = list(ws.tables.values())
    if len(tables) != 1:
        raise AdRevenueReportError("sheet_table_not_found", sheet=ws.title, table_count=len(tables))
    return tables[0]


def _resize_table_rows(ws: Worksheet, table, target_data_row_count: int) -> int:
    """Grows or shrinks an Excel Table's data rows to exactly match the fresh
    query result for this run (unlike plus_browser_point_sales_report's
    _extend_table_rows, this never fails closed on shrink: these are full
    monthly catalog dumps whose row count naturally rises and falls with the
    active content catalog, not a fixed enum of categories).

    New rows copy both style AND value/formula from the last existing data
    row, not just style. This matters because APP_2's 全体 sheet has table
    structured-reference formula columns ("広告売上" etc.) this module never
    writes to; those formulas use `[#This Row]`, which is row-relative, so
    copying the formula text verbatim into a new row keeps it correctly
    computing for that row once write_detail_sheet fills in the input columns.
    """
    min_col, min_row, max_col, max_row = range_boundaries(table.ref)
    header_row = min_row
    totals_row_count = getattr(table, "totalsRowCount", 0) or 0
    last_data_row = max_row - totals_row_count
    current_data_rows = last_data_row - header_row

    if target_data_row_count > current_data_rows:
        extra = target_data_row_count - current_data_rows
        style_source_row = last_data_row
        insert_at = last_data_row + 1
        ws.insert_rows(insert_at, amount=extra)
        for offset in range(extra):
            new_row = insert_at + offset
            for col in range(min_col, max_col + 1):
                src = ws.cell(row=style_source_row, column=col)
                dst = ws.cell(row=new_row, column=col)
                if src.has_style:
                    dst._style = copy.copy(src._style)
                dst.number_format = src.number_format
                dst.value = src.value
        new_last_data_row = last_data_row + extra
    elif target_data_row_count < current_data_rows:
        remove_count = current_data_rows - target_data_row_count
        ws.delete_rows(header_row + 1 + target_data_row_count, amount=remove_count)
        new_last_data_row = last_data_row - remove_count
    else:
        new_last_data_row = last_data_row

    new_max_row = new_last_data_row + totals_row_count
    table.ref = f"{get_column_letter(min_col)}{header_row}:{get_column_letter(max_col)}{new_max_row}"
    if table.autoFilter is not None:
        table.autoFilter.ref = f"{get_column_letter(min_col)}{header_row}:{get_column_letter(max_col)}{new_last_data_row}"
    return new_last_data_row


def write_detail_sheet(ws: Worksheet, headers: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    table = _sheet_table(ws)
    min_col, min_row, max_col, _max_row = range_boundaries(table.ref)
    header_row = min_row

    header_map: dict[str, int] = {}
    for col in range(min_col, max_col + 1):
        text = str(ws.cell(row=header_row, column=col).value or "").strip()
        if text:
            header_map[text] = col

    missing_required = [h for h in _REQUIRED_DETAIL_HEADERS if h in headers and h not in header_map]
    if missing_required:
        raise AdRevenueReportError("missing_detail_headers", sheet=ws.title, missing=missing_required)

    write_columns = [h for h in headers if h in header_map]

    data_start = header_row + 1
    _resize_table_rows(ws, table, len(rows))
    for offset, row_values in enumerate(rows):
        row_idx = data_start + offset
        for header in write_columns:
            ws.cell(row=row_idx, column=header_map[header]).value = row_values.get(header)


def create_ad_revenue_workbook(
    *,
    report_type: str,
    template_path: str | Path,
    output_path: str | Path,
    revenue_yen: int,
    detail_rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Builds the production XLSX via the package-preserving OOXML writer
    (xlsx_package_writer), NOT openpyxl's load_workbook()->save(). These
    templates carry Power Query (queryTable-backed) Excel Tables, and an
    openpyxl round trip -- even with zero edits -- silently drops the OOXML
    parts (xl/connections.xml, xl/queryTables/*, customXml/*, table
    relationship files) that say which connection a table is bound to,
    which is exactly what makes Excel Desktop flag the file for repair. See
    docs/jumpplus-ad-revenue-report.md ("openpyxl root cause"). openpyxl
    itself is still used elsewhere in this module (and in tests) for
    template structure analysis and validation -- just never as the writer
    for this function's output.
    """
    import xlsx_package_writer as pkg_writer

    spec = REPORT_SPECS[report_type]
    package = pkg_writer.XlsxPackage.load(template_path)

    try:
        pkg_writer.update_summary_value(package, SUMMARY_SHEET, revenue_yen)
    except pkg_writer.XlsxPackageError as exc:
        if exc.code == "sheet_not_found":
            raise AdRevenueReportError("summary_sheet_not_found", report_type=report_type) from exc
        if exc.code == "total_label_not_found":
            raise AdRevenueReportError("summary_total_cell_not_found", report_type=report_type) from exc
        raise

    def _write_sheet(sheet_name: str, headers: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
        try:
            pkg_writer.replace_detail_rows(
                package,
                sheet_name,
                headers,
                rows,
                required_headers=_REQUIRED_DETAIL_HEADERS,
            )
        except pkg_writer.XlsxPackageError as exc:
            if exc.code == "sheet_not_found":
                raise AdRevenueReportError(
                    "detail_sheet_not_found", report_type=report_type, sheet=sheet_name
                ) from exc
            if exc.code == "table_not_found":
                raise AdRevenueReportError(
                    "sheet_table_not_found", report_type=report_type, sheet=sheet_name
                ) from exc
            if exc.code == "missing_required_headers":
                raise AdRevenueReportError(
                    "missing_detail_headers", sheet=sheet_name, missing=exc.details.get("missing")
                ) from exc
            raise

    total_rows = 0
    for sheet_name in spec.detail_sheets:
        headers = spec.detail_headers
        if sheet_name == spec.zentai_sheet and spec.zentai_extra_headers:
            headers = spec.detail_headers + spec.zentai_extra_headers
        rows = detail_rows.get(sheet_name, [])
        _write_sheet(sheet_name, headers, rows)
        total_rows += len(rows)

    # 作品別/作品別_2: Python-computed replacements for what used to be Power
    # Query (see ad_revenue_work_summaries.py). Not counted in
    # detail_row_count, which has always meant "iOS/Android/全体 rows" --
    # existing callers (API response, audit log) rely on that meaning.
    for work_spec in spec.work_summary_sheets:
        _write_sheet(work_spec.sheet_name, work_spec.headers, detail_rows.get(work_spec.sheet_name, []))

    package.save(output_path)

    return {"detail_row_count": total_rows}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def generate_ad_revenue_report(
    *,
    project_id: str,
    report_type: str,
    target_month_text: str | None = None,
    today: date | None = None,
    template_file_id_override: str | None = None,
    output_folder_id: str | None = None,
) -> dict[str, Any]:
    if report_type not in REPORT_SPECS:
        raise AdRevenueReportError("unknown_report_type", report_type=report_type)
    if not project_id:
        raise AdRevenueReportError("project_required", "BIGQUERY_PROJECT_ID or PROJECT_ID is required")

    spec = REPORT_SPECS[report_type]
    today = today or tokyo_today()
    target_month = parse_target_month(target_month_text, today=today)

    readiness = check_readiness(project_id=project_id, report_type=report_type, target_month=target_month)
    if readiness.status != "ready":
        raise AdRevenueReportError(
            "not_ready",
            status_code=409,
            reason=readiness.status,
            report_type=report_type,
            target_month=target_month.isoformat(),
        )

    detail_rows = fetch_detail_rows(project_id=project_id, report_type=report_type, target_month=target_month)
    detail_rows = add_work_summary_rows(
        report_type=report_type, revenue_yen=readiness.revenue_yen, detail_rows=detail_rows
    )

    template_id = template_file_id_override or template_file_id(report_type)
    if not template_id:
        raise AdRevenueReportError(
            "template_not_configured", report_type=report_type, status_code=500
        )
    folder_id = output_folder_id or default_output_folder_id()
    file_name = output_file_name(report_type, target_month, today)

    from drive_io import download_drive_file, upload_xlsx_to_drive

    with tempfile.TemporaryDirectory(prefix="ad-revenue-report-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        template_path = tmp_root / "template.xlsx"
        output_path = tmp_root / file_name
        download_drive_file(template_id, template_path)
        result = create_ad_revenue_workbook(
            report_type=report_type,
            template_path=template_path,
            output_path=output_path,
            revenue_yen=readiness.revenue_yen,
            detail_rows=detail_rows,
        )
        uploaded = upload_xlsx_to_drive(
            output_path, folder_id=folder_id, file_name=file_name, report_type=report_type
        )

    return {
        "status": "ok",
        "report": spec.report_id,
        "report_name": spec.report_name,
        "target_month": target_month.isoformat(),
        "generated_date": today.isoformat(),
        "file_id": uploaded.get("id", ""),
        "file_name": uploaded.get("name") or file_name,
        "webViewLink": uploaded.get("webViewLink", ""),
        "revenue_yen": readiness.revenue_yen,
        "detail_row_count": result["detail_row_count"],
    }


# ---------------------------------------------------------------------------
# Google Sheets -> BigQuery sync (confirmed ad revenue values)
# ---------------------------------------------------------------------------

SHEET_HEADER_COLUMNS = ("対象月", "動画リワード", "奥付広告", "WEB")

_SHEETS_EPOCH = date(1899, 12, 30)


def _confirmed_table_schema() -> list:
    # Built lazily (not as a module-level constant) so importing this module
    # never touches `bigquery.SchemaField` at import time -- some sibling test
    # modules replace sys.modules["google.cloud.bigquery"] with an incomplete
    # stub and never restore it (see test_plus_browser_point_sales_report.py's
    # comment on the same issue for openpyxl), which would otherwise break
    # this module's import for any test that runs afterward in the same
    # process.
    return [
        bigquery.SchemaField("target_month", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("video_reward_yen", "INT64"),
        bigquery.SchemaField("app_footer_ad_yen", "INT64"),
        bigquery.SchemaField("web_ad_yen", "INT64"),
        bigquery.SchemaField("synced_at", "TIMESTAMP"),
    ]


def _parse_sheet_month_cell(value: Any) -> date:
    if isinstance(value, str):
        cleaned = value.strip()
        try:
            return datetime.strptime(cleaned, "%Y-%m-%d").date()
        except ValueError as exc:
            raise AdRevenueReportError("unparseable_sheet_month", value=cleaned) from exc
    if isinstance(value, (int, float)):
        return _SHEETS_EPOCH + timedelta(days=int(value))
    raise AdRevenueReportError("unparseable_sheet_month", value=repr(value))


def _parse_sheet_amount_cell(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if not cleaned or cleaned == "-":
            return None
        value = float(cleaned)
    return int(value)


def _ensure_confirmed_table(client: bigquery.Client, table_id: str) -> None:
    table = bigquery.Table(table_id, schema=_confirmed_table_schema())
    client.create_table(table, exists_ok=True)


def sync_ad_revenue_confirmed_values(
    *,
    project_id: str,
    spreadsheet_id_override: str | None = None,
    sheet_range_override: str | None = None,
) -> dict[str, Any]:
    """Refreshes the normal BigQuery table this module reads confirmed ad
    revenue from, from the "PLUS_広告費" Google Sheet's `data` sheet.

    Deliberately not a BigQuery EXTERNAL TABLE (format=GOOGLE_SHEETS) -- that
    is the existing org-wide convention for `dataset_exdata_tables.spreadsheets_*`
    tables, but was explicitly ruled out for this report (no BigQuery external
    table, no TROCCO). This function reads the sheet via the Sheets API and
    MERGEs into a plain BigQuery table instead, so it is safe to call
    repeatedly/on a schedule -- each run simply refreshes every month's row to
    the sheet's current value.
    """
    spreadsheet_id_value = spreadsheet_id_override or spreadsheet_id()
    sheet_range_value = sheet_range_override or sheet_range()

    from sheets_io import read_sheet_values

    rows = read_sheet_values(spreadsheet_id_value, sheet_range_value)
    if not rows:
        raise AdRevenueReportError("sheet_empty", status_code=500)

    header = [str(c).strip() for c in rows[0]]
    if header[: len(SHEET_HEADER_COLUMNS)] != list(SHEET_HEADER_COLUMNS):
        raise AdRevenueReportError("unexpected_sheet_header", status_code=500, header=header)

    records = []
    for raw_row in rows[1:]:
        if not raw_row or raw_row[0] in (None, ""):
            continue
        target_month = _parse_sheet_month_cell(raw_row[0])
        video_reward = _parse_sheet_amount_cell(raw_row[1]) if len(raw_row) > 1 else None
        app_footer_ad = _parse_sheet_amount_cell(raw_row[2]) if len(raw_row) > 2 else None
        web_ad = _parse_sheet_amount_cell(raw_row[3]) if len(raw_row) > 3 else None
        records.append(
            {
                "target_month": target_month.isoformat(),
                "video_reward_yen": video_reward,
                "app_footer_ad_yen": app_footer_ad,
                "web_ad_yen": web_ad,
            }
        )

    client = bigquery.Client(project=project_id)
    table_id = confirmed_revenue_table()
    _ensure_confirmed_table(client, table_id)

    if records:
        struct_params = [
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("target_month", "DATE", r["target_month"]),
                bigquery.ScalarQueryParameter("video_reward_yen", "INT64", r["video_reward_yen"]),
                bigquery.ScalarQueryParameter("app_footer_ad_yen", "INT64", r["app_footer_ad_yen"]),
                bigquery.ScalarQueryParameter("web_ad_yen", "INT64", r["web_ad_yen"]),
            )
            for r in records
        ]
        merge_sql = f"""
            merge `{table_id}` T
            using unnest(@rows) S
            on T.target_month = S.target_month
            when matched then update set
                video_reward_yen = S.video_reward_yen,
                app_footer_ad_yen = S.app_footer_ad_yen,
                web_ad_yen = S.web_ad_yen,
                synced_at = current_timestamp()
            when not matched then insert (target_month, video_reward_yen, app_footer_ad_yen, web_ad_yen, synced_at)
            values (S.target_month, S.video_reward_yen, S.app_footer_ad_yen, S.web_ad_yen, current_timestamp())
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("rows", "STRUCT", struct_params)]
        )
        client.query(merge_sql, job_config=job_config).result()

    return {"status": "ok", "row_count": len(records)}
