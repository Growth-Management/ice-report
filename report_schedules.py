"""Common registry + read-only status view of every scheduled process of the
ICE Report Generator (Admin UI "Schedules" tab, GET /admin/report-schedules).

External communication performed by this module:
- Cloud Scheduler API v1 (cloudscheduler.googleapis.com), READ ONLY
  (`projects.locations.jobs.list`) for project/location below, via the
  runtime's ADC. Nothing is ever created, updated, paused or resumed.
- Firestore read of report_definitions (through distribution.list_report_definitions).

Two different things are shown side by side and deliberately never merged:
1. Dedicated Cloud Scheduler jobs (REPORT_SCHEDULE_SPECS): one job per
   bespoke report / data sync / supporting process, with an expected cron,
   timezone and endpoint compared against the live job ("drift").
2. report_definitions schedule metadata: per-definition monthly settings
   stored in Firestore. A definition has NO Cloud Scheduler job of its own
   -- all of them are executed by the single generic executor job
   (`report-definitions-monthly-schedule-runs`, itself a spec entry of kind
   report_definition_executor) -- so definition rows never get a job name.

Only non-secret job fields are ever read into a response: name, schedule,
timeZone, state, attemptDeadline, retryConfig.* (retryCount/maxRetryAttempts,
maxRetryDuration, minBackoffDuration, maxBackoffDuration, maxDoublings),
httpTarget.uri / httpMethod, httpTarget.oidcToken.audience, and the
lastAttemptTime / scheduleTime / userUpdateTime / status.code timestamps. httpTarget.headers / body (which can carry an admin key),
oidcToken.serviceAccountEmail and oauthToken are never copied.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

SCHEDULER_PROJECT_ID = "ice-sh"
SCHEDULER_LOCATION = "asia-northeast1"
# Canonical Cloud Run origin that every ICE Report Generator Scheduler job
# targets (the same URL recorded in docs/thermae-romae-report.md etc.).
DEFAULT_TARGET_BASE_URL = "https://report-generator-635067190197.asia-northeast1.run.app"

KIND_REPORT_GENERATION = "report_generation"
KIND_DATA_SYNC = "data_sync"
KIND_REPORT_DEFINITION_EXECUTOR = "report_definition_executor"
KIND_MAINTENANCE = "maintenance"
KIND_REPORT_DEFINITION = "report_definition"
KIND_UNREGISTERED = "unregistered_job"
KIND_ORDER = (
    KIND_REPORT_GENERATION,
    KIND_DATA_SYNC,
    KIND_REPORT_DEFINITION_EXECUTOR,
    KIND_MAINTENANCE,
    KIND_REPORT_DEFINITION,
    KIND_UNREGISTERED,
)

DRIFT_OK = "OK"
DRIFT_CONFIG = "CONFIG_DRIFT"
DRIFT_NOT_CREATED = "NOT_CREATED"
DRIFT_UNKNOWN = "UNKNOWN"
DRIFT_NO_EXPECTED = "NO_EXPECTED"  # registered, but no documented expected config to compare with
DRIFT_NOT_APPLICABLE = "N/A"  # report_definitions metadata rows (no job of their own)
DRIFT_UNREGISTERED = "UNREGISTERED"

AUDIENCE_ENDPOINT_URL = "endpoint_url"  # audience = full endpoint URL
AUDIENCE_SERVICE_ROOT = "service_root"  # audience = Cloud Run origin only (no path)


@dataclass(frozen=True)
class RetryExpectation:
    """Expected Cloud Scheduler retryConfig (durations as the API prints them,
    e.g. "600s"). max_retry_duration "0s" means "no duration limit"; the
    attempt count is then the only bound."""

    max_retry_attempts: int
    max_retry_duration: str
    min_backoff: str
    max_backoff: str
    max_doublings: int


@dataclass(frozen=True)
class ScheduledReportSpec:
    id: str
    display_name: str
    report_type: str
    kind: str
    scheduler_job_name: str
    expected_schedule: str | None
    timezone: str
    endpoint: str
    target_month_rule: str
    audience_mode: str | None = None
    notes: str = ""
    # None = no documented expectation; drift is not evaluated for that part.
    expected_attempt_deadline: str | None = None
    expected_retry: RetryExpectation | None = None


# docs/jumpplus-coin-ledger-report.md. Generation takes ~326s (first production
# run), so the Scheduler deadline must not be shorter than the Cloud Run request
# timeout; retries (10m, 20m, 40m, 60m, 60m) cover ~3h of source_not_ready.
COIN_LEDGER_ATTEMPT_DEADLINE = "1800s"
COIN_LEDGER_RETRY = RetryExpectation(
    max_retry_attempts=5,
    max_retry_duration="0s",
    min_backoff="600s",
    max_backoff="3600s",
    max_doublings=3,
)

# Source of each expected value is noted per entry; none is copied from the
# live job itself.
REPORT_SCHEDULE_SPECS: tuple[ScheduledReportSpec, ...] = (
    ScheduledReportSpec(
        id="thermae-romae",
        display_name="テルマエ・ロマエ月次販売報告書",
        report_type="thermae-romae",
        kind=KIND_REPORT_GENERATION,
        scheduler_job_name="thermae-romae-monthly-report",
        expected_schedule="0 9 2 * *",  # docs/thermae-romae-report.md
        timezone="Asia/Tokyo",
        endpoint="/admin/reports/thermae-romae/scheduled-generate",
        target_month_rule="前月（Asia/Tokyo）",
        audience_mode=AUDIENCE_ENDPOINT_URL,
        notes="Drive保存のみ。配布なし",
    ),
    ScheduledReportSpec(
        id="plus-browser-point-sales",
        display_name="PLUS ブラウザ版ポイント売上",
        report_type="plus-browser-point-sales",
        kind=KIND_REPORT_GENERATION,
        scheduler_job_name="plus-browser-point-sales-monthly-report",
        expected_schedule="0 7 1 * *",  # docs/plus-browser-point-sales-report.md
        timezone="Asia/Tokyo",
        endpoint="/admin/reports/plus-browser-point-sales/scheduled-generate",
        target_month_rule="前月（Asia/Tokyo）",
        audience_mode=AUDIENCE_ENDPOINT_URL,
        notes="Drive保存のみ。配布なし",
    ),
    ScheduledReportSpec(
        id="jumpplus-ad-revenue-video-reward",
        display_name="J+ 動画リワード広告売上",
        report_type="jumpplus-ad-revenue/video-reward",
        kind=KIND_REPORT_GENERATION,
        scheduler_job_name="ad-revenue-video-reward-monthly-report",
        expected_schedule="5 8 * * *",  # docs/jumpplus-ad-revenue-report.md
        timezone="Asia/Tokyo",
        endpoint="/admin/reports/jumpplus-ad-revenue/video-reward/scheduled-generate",
        target_month_rule="前月 + readiness（未達はwaiting応答、翌日再poll）",
        audience_mode=AUDIENCE_SERVICE_ROOT,
        notes="日次poll。生成済み月はskip",
    ),
    ScheduledReportSpec(
        id="jumpplus-ad-revenue-app2",
        display_name="J+ 広告売上_APP_2（奥付広告）",
        report_type="jumpplus-ad-revenue/app2",
        kind=KIND_REPORT_GENERATION,
        scheduler_job_name="ad-revenue-app2-monthly-report",
        expected_schedule="15 8 * * *",  # docs/jumpplus-ad-revenue-report.md
        timezone="Asia/Tokyo",
        endpoint="/admin/reports/jumpplus-ad-revenue/app2/scheduled-generate",
        target_month_rule="前月 + readiness（未達はwaiting応答、翌日再poll）",
        audience_mode=AUDIENCE_SERVICE_ROOT,
        notes="日次poll。生成済み月はskip",
    ),
    ScheduledReportSpec(
        id="jumpplus-ad-revenue-web",
        display_name="J+ 広告売上_WEB",
        report_type="jumpplus-ad-revenue/web",
        kind=KIND_REPORT_GENERATION,
        scheduler_job_name="ad-revenue-web-monthly-report",
        expected_schedule="25 8 * * *",  # docs/jumpplus-ad-revenue-report.md
        timezone="Asia/Tokyo",
        endpoint="/admin/reports/jumpplus-ad-revenue/web/scheduled-generate",
        target_month_rule="前月 + readiness（未達はwaiting応答、翌日再poll）",
        audience_mode=AUDIENCE_SERVICE_ROOT,
        notes="日次poll。生成済み月はskip",
    ),
    ScheduledReportSpec(
        id="jumpplus-coin-ledger",
        display_name="ジャンプ＋コイン出納レポート",
        report_type="jumpplus-coin-ledger",
        kind=KIND_REPORT_GENERATION,
        scheduler_job_name="jumpplus-coin-ledger-monthly-report",
        expected_schedule="0 7 1 * *",  # 要求仕様: 毎月1日 07:00 Asia/Tokyo
        timezone="Asia/Tokyo",
        endpoint="/admin/reports/jumpplus-coin-ledger/scheduled-generate",
        target_month_rule="前月 + readiness gate（未達は503、Cloud Scheduler retryで再実行）",
        audience_mode=AUDIENCE_SERVICE_ROOT,
        notes="App/WEB/話売商品の3ファイルを1つのOTP付きdeliveryで配布",
        expected_attempt_deadline=COIN_LEDGER_ATTEMPT_DEADLINE,
        expected_retry=COIN_LEDGER_RETRY,
    ),
    ScheduledReportSpec(
        id="ad-revenue-sync",
        display_name="J+ 広告売上 確定値同期（Sheets→BigQuery）",
        report_type="ad-revenue-sync",
        kind=KIND_DATA_SYNC,
        scheduler_job_name="ad-revenue-sync",
        expected_schedule="*/30 * * * *",  # docs/jumpplus-ad-revenue-report.md
        timezone="Asia/Tokyo",
        endpoint="/admin/ad-revenue/scheduled-sync",
        target_month_rule="対象月なし（確定値シート全行をMERGE）",
        audience_mode=AUDIENCE_SERVICE_ROOT,
    ),
    ScheduledReportSpec(
        id="report-definitions-schedule-runs",
        display_name="レポート定義 月次スケジュール実行（汎用executor）",
        report_type="report_definitions",
        kind=KIND_REPORT_DEFINITION_EXECUTOR,
        scheduler_job_name="report-definitions-monthly-schedule-runs",
        expected_schedule=None,  # not documented in the repository
        timezone="Asia/Tokyo",
        endpoint="/admin/report-definitions/schedule-runs",
        target_month_rule="各レポート定義のschedule設定に従う（対象は前月）",
        audience_mode=None,
        notes="各レポート定義のschedule metadataを評価する唯一のjob。expected未文書化",
    ),
    ScheduledReportSpec(
        id="ice-report-cleanup",
        display_name="期限切れ配布のcleanup",
        report_type="cleanup",
        kind=KIND_MAINTENANCE,
        scheduler_job_name="ice-report-cleanup",
        expected_schedule=None,  # not documented in the repository
        timezone="Asia/Tokyo",
        endpoint="/internal/cleanup",
        target_month_rule="対象月なし（期限切れdeliveryをactive=false）",
        audience_mode=None,
        notes="expected未文書化",
    ),
)


# ---------------------------------------------------------------------------
# Human readable cron
# ---------------------------------------------------------------------------


def _is_int(text: str) -> bool:
    return text.isdigit()


def human_readable_schedule(cron: str | None) -> str:
    """Covers the cron shapes this service actually uses; anything else is
    shown verbatim (never guessed)."""
    if not cron:
        return "-"
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    if month == "*" and dow == "*":
        if _is_int(minute) and _is_int(hour) and int(minute) < 60 and int(hour) < 24:
            clock = f"{int(hour):02d}:{int(minute):02d}"
            if _is_int(dom) and 1 <= int(dom) <= 31:
                return f"毎月{int(dom)}日 {clock}"
            if dom == "*":
                return f"毎日 {clock}"
        if minute.startswith("*/") and _is_int(minute[2:]) and hour == "*" and dom == "*":
            return f"{int(minute[2:])}分おき"
        if _is_int(minute) and hour == "*" and dom == "*" and int(minute) < 60:
            return f"毎時 {int(minute):02d}分"
    return cron


# ---------------------------------------------------------------------------
# Live lookup (READ ONLY)
# ---------------------------------------------------------------------------

LIVE_OK = "ok"
LIVE_UNAVAILABLE = "unavailable"

_SAFE_JOB_FIELDS = ("schedule", "timeZone", "state", "lastAttemptTime", "scheduleTime", "userUpdateTime")


def safe_job_view(job: dict[str, Any]) -> dict[str, Any]:
    http_target = job.get("httpTarget") or {}
    oidc = http_target.get("oidcToken") or {}
    view = {"name": str(job.get("name") or "").rsplit("/", 1)[-1]}
    for key in _SAFE_JOB_FIELDS:
        view[key] = job.get(key)
    view["uri"] = http_target.get("uri")
    view["httpMethod"] = http_target.get("httpMethod")
    view["audience"] = oidc.get("audience")
    view["attemptDeadline"] = job.get("attemptDeadline")
    retry = job.get("retryConfig")
    retry = retry if isinstance(retry, dict) else {}
    view["retryConfig"] = {
        # Cloud Scheduler API v1 returns the attempt count as `retryCount`
        # (REST/gcloud); `maxRetryAttempts` is the client-library name.
        "maxRetryAttempts": retry.get("retryCount") if retry.get("retryCount") is not None else retry.get("maxRetryAttempts"),
        "maxRetryDuration": retry.get("maxRetryDuration"),
        "minBackoffDuration": retry.get("minBackoffDuration"),
        "maxBackoffDuration": retry.get("maxBackoffDuration"),
        "maxDoublings": retry.get("maxDoublings"),
    }
    status = job.get("status") or {}
    view["lastAttemptStatusCode"] = status.get("code") if isinstance(status, dict) else None
    return view


def _default_list_jobs(project: str, location: str) -> list[dict[str, Any]]:
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    service = build("cloudscheduler", "v1", credentials=credentials, cache_discovery=False)
    jobs: list[dict[str, Any]] = []
    request = service.projects().locations().jobs().list(
        parent=f"projects/{project}/locations/{location}", pageSize=100
    )
    while request is not None:
        response = request.execute()
        jobs.extend(response.get("jobs", []))
        request = service.projects().locations().jobs().list_next(request, response)
    return jobs


def _error_reason(exc: Exception) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "not_found"
    if status is not None:
        return f"api_error_{status}"
    name = type(exc).__name__
    if "DefaultCredentials" in name or "RefreshError" in name:
        return "credentials_unavailable"
    return "lookup_failed"


@dataclass
class LiveLookup:
    status: str
    reason: str
    jobs: dict[str, dict[str, Any]]
    fetched_at: str


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "value": None}


def live_lookup(
    *,
    list_jobs: Callable[[str, str], list[dict[str, Any]]] | None = None,
    refresh: bool = False,
    ttl_seconds: float = 60.0,
) -> LiveLookup:
    """Never raises: any failure (403, API error, missing credentials)
    becomes status=unavailable with a coarse reason code only -- no
    exception message is ever surfaced."""
    if os.environ.get("REPORT_SCHEDULES_LIVE_LOOKUP", "1").strip().lower() in ("0", "false", "off", "no"):
        return LiveLookup(LIVE_UNAVAILABLE, "disabled", {}, "")
    use_cache = list_jobs is None
    now = time.monotonic()
    if use_cache and not refresh:
        with _cache_lock:
            cached = _cache["value"]
            if cached is not None and now - _cache["at"] < ttl_seconds:
                return cached
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        raw_jobs = (list_jobs or _default_list_jobs)(SCHEDULER_PROJECT_ID, SCHEDULER_LOCATION)
        result = LiveLookup(LIVE_OK, "", {v["name"]: v for v in (safe_job_view(j) for j in raw_jobs)}, fetched_at)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        result = LiveLookup(LIVE_UNAVAILABLE, _error_reason(exc), {}, fetched_at)
    if use_cache:
        with _cache_lock:
            _cache["value"], _cache["at"] = result, now
    return result


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def target_base_url() -> str:
    return (os.environ.get("REPORT_SCHEDULES_TARGET_BASE_URL") or DEFAULT_TARGET_BASE_URL).strip().rstrip("/")


def _origin(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def expected_uri(spec: ScheduledReportSpec, base_url: str) -> str:
    return f"{base_url}{spec.endpoint}"


def expected_audience(spec: ScheduledReportSpec, base_url: str) -> str | None:
    if spec.audience_mode == AUDIENCE_ENDPOINT_URL:
        return expected_uri(spec, base_url)
    if spec.audience_mode == AUDIENCE_SERVICE_ROOT:
        return base_url
    return None


def _duration_seconds(value: Any) -> float | None:
    """Cloud Scheduler prints durations as "<seconds>s" (e.g. "1800s").
    Anything unparsable / missing is None, never an exception."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text.endswith("s"):
        return None
    try:
        return float(text[:-1])
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _retry_view(job: dict[str, Any]) -> dict[str, Any]:
    retry = job.get("retryConfig") or {}
    return {
        "max_retry_attempts": _int_or_none(retry.get("maxRetryAttempts")),
        "max_retry_duration": retry.get("maxRetryDuration"),
        "min_backoff": retry.get("minBackoffDuration"),
        "max_backoff": retry.get("maxBackoffDuration"),
        "max_doublings": _int_or_none(retry.get("maxDoublings")),
    }


def _expected_retry_view(expected: RetryExpectation | None) -> dict[str, Any] | None:
    if expected is None:
        return None
    return {
        "max_retry_attempts": expected.max_retry_attempts,
        "max_retry_duration": expected.max_retry_duration,
        "min_backoff": expected.min_backoff,
        "max_backoff": expected.max_backoff,
        "max_doublings": expected.max_doublings,
    }


def _retry_diffs(expected: RetryExpectation, job: dict[str, Any]) -> list[str]:
    actual = _retry_view(job)
    diffs = []
    if actual["max_retry_attempts"] != expected.max_retry_attempts:
        diffs.append("retry.max_retry_attempts")
    # An omitted maxRetryDuration is the proto default "0s" (no limit).
    actual_duration = 0.0 if actual["max_retry_duration"] is None else _duration_seconds(actual["max_retry_duration"])
    if actual_duration != _duration_seconds(expected.max_retry_duration):
        diffs.append("retry.max_retry_duration")
    if _duration_seconds(actual["min_backoff"]) != _duration_seconds(expected.min_backoff):
        diffs.append("retry.min_backoff")
    if _duration_seconds(actual["max_backoff"]) != _duration_seconds(expected.max_backoff):
        diffs.append("retry.max_backoff")
    if actual["max_doublings"] != expected.max_doublings:
        diffs.append("retry.max_doublings")
    return diffs


def evaluate_drift(
    spec: ScheduledReportSpec, job: dict[str, Any] | None, *, live_status: str, base_url: str
) -> tuple[str, list[str]]:
    if live_status != LIVE_OK:
        return DRIFT_UNKNOWN, []
    if job is None:
        return DRIFT_NOT_CREATED, []
    if spec.expected_schedule is None:
        return DRIFT_NO_EXPECTED, []
    diffs = []
    if (job.get("schedule") or "").strip() != spec.expected_schedule:
        diffs.append("cron")
    if (job.get("timeZone") or "") != spec.timezone:
        diffs.append("timezone")
    actual_uri = job.get("uri") or ""
    if urlsplit(actual_uri).path != spec.endpoint or _origin(actual_uri) != _origin(base_url):
        diffs.append("endpoint")
    audience = expected_audience(spec, base_url)
    if audience is not None and (job.get("audience") or "") != audience:
        diffs.append("audience")
    if spec.expected_attempt_deadline is not None and (
        _duration_seconds(job.get("attemptDeadline")) != _duration_seconds(spec.expected_attempt_deadline)
    ):
        diffs.append("attempt_deadline")
    if spec.expected_retry is not None:
        diffs.extend(_retry_diffs(spec.expected_retry, job))
    return (DRIFT_OK if not diffs else DRIFT_CONFIG), diffs


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _spec_row(spec: ScheduledReportSpec, live: LiveLookup, base_url: str) -> dict[str, Any]:
    job = live.jobs.get(spec.scheduler_job_name) if live.status == LIVE_OK else None
    drift, diffs = evaluate_drift(spec, job, live_status=live.status, base_url=base_url)
    if live.status != LIVE_OK:
        state = "UNKNOWN"
    elif job is None:
        state = "NOT_CREATED"
    else:
        state = job.get("state") or "UNKNOWN"
    return {
        "id": spec.id,
        "display_name": spec.display_name,
        "report_type": spec.report_type,
        "kind": spec.kind,
        "scheduler_job_name": spec.scheduler_job_name,
        "expected": {
            "cron": spec.expected_schedule,
            "schedule_text": human_readable_schedule(spec.expected_schedule),
            "timezone": spec.timezone,
            "endpoint": spec.endpoint,
            "attempt_deadline": spec.expected_attempt_deadline,
            "retry": _expected_retry_view(spec.expected_retry),
        },
        "actual": (
            {
                "cron": job.get("schedule"),
                "schedule_text": human_readable_schedule(job.get("schedule")),
                "timezone": job.get("timeZone"),
                "endpoint": urlsplit(job.get("uri") or "").path or None,
                "last_attempt_time": job.get("lastAttemptTime"),
                "next_schedule_time": job.get("scheduleTime"),
                "last_attempt_status_code": job.get("lastAttemptStatusCode"),
                "attempt_deadline": job.get("attemptDeadline"),
                "retry": _retry_view(job),
            }
            if job
            else None
        ),
        "state": state,
        "drift": drift,
        "drift_fields": diffs,
        "target_month_rule": spec.target_month_rule,
        "notes": spec.notes,
    }


def _definition_rows(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in definitions:
        schedule = item.get("schedule") or {}
        if item.get("status") == "archived" or not schedule.get("enabled"):
            continue
        frequency = schedule.get("frequency") or "monthly"
        text = (
            f"毎月{int(schedule.get('day_of_month') or 1)}日 {schedule.get('time_of_day') or ''}".strip()
            if frequency == "monthly"
            else str(frequency)
        )
        rows.append(
            {
                "id": str(item.get("report_id") or ""),
                "display_name": str(item.get("name") or item.get("report_id") or ""),
                "report_type": "report_definition",
                "kind": KIND_REPORT_DEFINITION,
                "scheduler_job_name": None,
                "expected": {
                    "cron": None,
                    "schedule_text": text,
                    "timezone": schedule.get("timezone") or "Asia/Tokyo",
                    "endpoint": None,
                },
                "actual": None,
                "state": "METADATA_ENABLED",
                "drift": DRIFT_NOT_APPLICABLE,
                "drift_fields": [],
                "target_month_rule": "前月（汎用executorが評価）",
                "notes": "schedule metadataのみ。専用Cloud Scheduler jobは持たない（汎用executorが実行）",
            }
        )
    return rows


def _unregistered_rows(live: LiveLookup, base_url: str) -> list[dict[str, Any]]:
    """Live jobs that target this service but are missing from the
    registry -- surfaced so a new job cannot silently go untracked."""
    if live.status != LIVE_OK:
        return []
    registered = {spec.scheduler_job_name for spec in REPORT_SCHEDULE_SPECS}
    base_origin = _origin(base_url)
    rows = []
    for name, job in live.jobs.items():
        if name in registered or _origin(job.get("uri") or "") != base_origin:
            continue
        rows.append(
            {
                "id": name,
                "display_name": name,
                "report_type": None,
                "kind": KIND_UNREGISTERED,
                "scheduler_job_name": name,
                "expected": None,
                "actual": {
                    "cron": job.get("schedule"),
                    "schedule_text": human_readable_schedule(job.get("schedule")),
                    "timezone": job.get("timeZone"),
                    "endpoint": urlsplit(job.get("uri") or "").path or None,
                    "last_attempt_time": job.get("lastAttemptTime"),
                    "next_schedule_time": job.get("scheduleTime"),
                    "last_attempt_status_code": job.get("lastAttemptStatusCode"),
                    "attempt_deadline": job.get("attemptDeadline"),
                    "retry": _retry_view(job),
                },
                "state": job.get("state") or "UNKNOWN",
                "drift": DRIFT_UNREGISTERED,
                "drift_fields": [],
                "target_month_rule": "",
                "notes": "registry未登録のjob",
            }
        )
    return rows


def _sort_key(row: dict[str, Any]) -> tuple:
    kind = row.get("kind")
    return (KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER), str(row.get("display_name") or ""), str(row.get("id") or ""))


def build_schedules_view(
    *,
    live: LiveLookup,
    definitions: list[dict[str, Any]] | None,
    definitions_error: str = "",
) -> dict[str, Any]:
    base_url = target_base_url()
    rows = [_spec_row(spec, live, base_url) for spec in REPORT_SCHEDULE_SPECS]
    rows += _definition_rows(definitions or [])
    rows += _unregistered_rows(live, base_url)
    rows.sort(key=_sort_key)

    by_drift: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for row in rows:
        by_drift[row["drift"]] = by_drift.get(row["drift"], 0) + 1
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    return {
        "items": rows,
        "summary": {"total": len(rows), "by_drift": by_drift, "by_kind": by_kind},
        "live_lookup": {
            "status": live.status,
            "reason": live.reason,
            "project": SCHEDULER_PROJECT_ID,
            "location": SCHEDULER_LOCATION,
            "fetched_at": live.fetched_at,
        },
        "report_definitions": {"status": "unavailable" if definitions_error else "ok", "reason": definitions_error},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
