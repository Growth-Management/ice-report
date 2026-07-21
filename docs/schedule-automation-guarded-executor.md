# Schedule Automation Guarded Executor Design

Date: 2026-07-08

This document defines the next Phase 9 step after the read-only schedule preview foundation.
It is a design checkpoint only. It does not create Cloud Scheduler jobs, automatically generate
reports, create deliveries, send email, edit SQL, edit template mappings, or change storage
destinations.

## Current State

- Admin users can save monthly schedule metadata per report definition.
- `GET /report-definitions/schedule-preview` evaluates enabled monthly schedules as a read-only
  dry run.
- Preview output contains safe metadata only.
- Report generation can use a published template only when `report_id` is explicitly supplied.
- Query config / template mapping publish foundation records approved metadata on report
  definition versions.
- Storage destination allowlist validation exists for report definitions.

## Goal

Add a guarded executor path that can evaluate due schedules and, when explicitly confirmed,
run eligible report definitions through the existing generation path.

The executor must be safe to operate manually before any recurring scheduler is attached.
The first implementation should prove input validation, idempotency, logging, smoke, and rollback
without enabling unattended production automation.

## Non-Goals

- No Cloud Scheduler job creation.
- No unattended periodic execution.
- No automatic email delivery until a later task explicitly enables it.
- No arbitrary Drive or GCS destination updates.
- No SQL editor, template mapping editor, or runtime mapping mutation.
- No public service-wide IAP change.

## Recommended First Implementation

Implement an admin-only executor endpoint:

`POST /report-definitions/schedule-runs`

The endpoint should use the same admin authorization boundary as other report definition admin
APIs:

- Primary human path: `report-generator-admin` with Cloud Run direct IAP.
- Script / break-glass path: `X-Admin-Key`.
- Do not require IAP on the public `report-generator` service.

The endpoint should default to dry-run behavior. Actual execution must require all of:

- `mode` equals `execute`.
- `confirm` equals `RUN_DUE_REPORTS`.
- A caller-supplied `idempotency_key`.
- A due schedule result from the same evaluation logic used by schedule preview.

Suggested request shape:

```json
{
  "mode": "dry_run",
  "evaluation_time": "2026-07-08T00:00:00Z",
  "limit": 100,
  "report_ids": ["example_report"],
  "idempotency_key": "manual-2026-07-08-0900",
  "confirm": "RUN_DUE_REPORTS"
}
```

Rules:

- `mode` defaults to `dry_run`.
- `evaluation_time` is optional and should default to server time.
- `report_ids` is optional. If omitted, evaluate all non-archived definitions up to `limit`.
- `execute` mode must reject missing or duplicate `idempotency_key`.
- `execute` mode must reject `report_ids` that are not due at the evaluation time unless a later
  explicit override task is approved.
- The response must identify each item by safe metadata only: `report_id`, `name`, `status`,
  `current_version`, `due`, `reason`, `action`, and sanitized result status.

## Eligibility Checks

For each candidate, run checks in this order:

1. Report definition exists.
2. Definition is not archived and status is active.
3. Schedule is enabled.
4. Schedule frequency is monthly.
5. Evaluation date and time are due according to the schedule timezone.
6. Current version exists.
7. Current version has a published template reference.
8. Current version has approved query config and template mapping metadata.
9. Storage metadata is within the configured allowlist.
10. No successful scheduled run already exists for the same report and schedule window.

Fail closed on the first failed eligibility check. Do not start BigQuery, workbook generation,
delivery creation, or notification work for failed candidates.

## Idempotency Model

Use a dedicated Firestore collection for execution records, for example:

`scheduled_report_runs`

Suggested document key:

`{report_id}:{schedule_local_date}:{schedule_time}:{idempotency_key_hash}`

The stored document should contain only safe metadata:

- `report_id`
- `schedule_local_date`
- `schedule_time`
- `schedule_timezone`
- `idempotency_key_hash`
- `status`
- `created_at`
- `updated_at`
- `started_at`
- `finished_at`
- `result_code`
- `delivery_id` when a delivery is created in a later approved step
- `error_code`

Do not store raw idempotency keys if they can contain operator context. Store a deterministic hash.
Do not store raw emails, tokens, signed URLs, template GCS URIs, SQL text, template mapping details,
Excel cell values, IP addresses, user agents, or provider event JSON.

State transitions:

```text
planned -> running -> succeeded
planned -> running -> failed
planned -> skipped
```

Duplicate execute requests for an existing successful run should return a safe duplicate result and
must not run generation again.

## Execution Boundary

The first executor implementation may stop after workbook generation or delivery creation, but it
must not send email automatically unless the PR explicitly scopes that behavior and has smoke and
rollback coverage.

Recommended staged path:

1. Dry-run executor response using existing schedule preview logic.
2. Execute mode creates run records and stops before generation.
3. Execute mode calls report generation with `report_id`.
4. Execute mode creates delivery records without sending email.
5. Later task: notification delivery with separate approval and smoke.
6. Later task: Cloud Scheduler attachment.

## Logging

Log one structured action per request and one per candidate. Logs may include:

- action name
- result code
- report_id
- candidate count
- due count
- skipped count
- execution mode
- sanitized error code

Logs must not include:

- secret
- PIN
- raw email
- token fragments
- Admin key fingerprint
- IP
- user agent
- Signed URL
- SQL text
- template mapping details
- template GCS URI
- Excel cell values
- provider event JSON
- raw idempotency key

## API Responses

Responses should be bounded and safe:

```json
{
  "mode": "dry_run",
  "dry_run": true,
  "evaluation_time": "2026-07-08T00:00:00Z",
  "counts": {
    "checked": 3,
    "due": 1,
    "eligible": 1,
    "executed": 0,
    "skipped": 2,
    "failed": 0
  },
  "items": [
    {
      "report_id": "example_report",
      "name": "Example Report",
      "current_version": 3,
      "due": true,
      "eligible": true,
      "action": "would_execute",
      "reason": "due"
    }
  ]
}
```

## Smoke Plan

For the design-only PR:

- `git diff --check`
- `python -m py_compile app.py distribution.py`
- `python -m unittest tests.test_admin_report_definitions tests.test_admin_iap_auth`
- `scripts\check-doc-legacy-references.ps1`
- Cloud Run deploy is not required.

For the first executor implementation PR:

- Run the existing unit tests above.
- Add tests for dry-run default, execute confirmation requirement, duplicate idempotency rejection,
  non-due rejection, and sanitized response fields.
- Deploy both `report-generator` and `report-generator-admin` if `app.py` or `distribution.py`
  changes.
- Smoke `POST /report-definitions/schedule-runs` in dry-run mode.
- Smoke execute mode against an archived or test-only report definition that cannot send mail.
- Confirm no Cloud Scheduler jobs were created.
- Confirm no email was sent unless that PR explicitly includes notification execution.
- Confirm public `/api-health` and admin IAP read-only checks still pass.
- Confirm public/admin runtime ERROR logs remain clear after deploy.

## Rollback

Design-only PR rollback is a normal docs revert.

Executor implementation rollback:

- Route traffic back to the previous Cloud Run revisions for both services.
- Revert the PR if the code path is unsafe.
- Keep existing report definition schedule metadata intact.
- Do not delete run records unless a separate retention/cleanup task is approved.
- Do not change SQL, template mapping, published template metadata, or storage allowlists during
  rollback.

## Decision

Proceed next with a small implementation PR for `POST /report-definitions/schedule-runs` dry-run
and execute-guard validation only. Keep actual generation, delivery creation, email sending, and
Cloud Scheduler attachment as later tasks unless explicitly approved in that PR scope.

## Guard Validation Foundation

The first implementation adds `POST /report-definitions/schedule-runs` as an admin-only guarded
executor validation path.

Implemented behavior:

- Defaults to dry-run mode.
- Reuses schedule preview evaluation for due checks.
- Requires `mode=execute`, `confirm=RUN_DUE_REPORTS`, and a valid `idempotency_key` before execute
  guard validation.
- Stores only hashed idempotency metadata in `scheduled_report_runs` for validated execute requests.
- Rejects duplicate execute requests for the same report, schedule window, timezone, and idempotency
  key hash.
- Returns safe metadata only.

Still intentionally not implemented:

- Delivery creation.
- Email notification.
- Cloud Scheduler job creation or recurring automation.
- SQL editing, template mapping editing, or storage destination changes.

## Manual Generation Foundation

The second implementation keeps the same admin-only endpoint and adds an explicitly guarded manual
generation step.

Implemented behavior:

- `POST /report-definitions/schedule-runs` still defaults to dry-run mode.
- `mode=execute` without `execute_step=generate` remains validation-only.
- Actual generation requires all of:
  - `mode=execute`
  - `execute_step=generate`
  - `confirm=RUN_DUE_REPORTS`
  - `confirm_generation=GENERATE_REPORTS`
  - a valid `idempotency_key`
  - an eligible due schedule
- The endpoint reuses the published template selected by the report definition current version.
- The output destination uses the report definition `gcs_prefix` when set, otherwise the runtime
  `BUCKET_NAME` / `OBJECT_PREFIX` defaults.
- The scheduled run record is marked `running`, then `succeeded` with `generation_succeeded`, or
  `failed` with `generation_failed`.
- Responses and run records include only safe generation metadata such as report month, output file
  name, row counts, and whether a GCS object was produced.

Still intentionally not implemented:

- Email notification.
- Cloud Scheduler job creation or recurring automation.
- SQL editing, template mapping editing, or storage destination changes.
- Signed URL generation from schedule execution.

## Manual Delivery Record Foundation

The third implementation adds a guarded manual delivery-record step after scheduled generation.

Implemented behavior:

- `POST /report-definitions/schedule-runs` supports `execute_step=deliver`.
- Actual delivery-record creation requires all of:
  - `mode=execute`
  - `execute_step=deliver`
  - `confirm=RUN_DUE_REPORTS`
  - `confirm_generation=GENERATE_REPORTS`
  - `confirm_delivery=CREATE_DELIVERY_RECORDS`
  - a valid `idempotency_key`
  - at least one `allowed_domains` or `allowed_emails` entry in the request payload
  - an eligible due schedule
- The endpoint generates the report with the published template, creates one active delivery
  record for the generated GCS object, and stores only safe delivery metadata in the scheduled
  run record.
- Scheduled delivery creation does not send email and does not post the download URL to Slack.
- Responses and run records include only safe metadata such as report month, output file name,
  row counts, whether a delivery record exists, delivery id, expiry timestamp, and allowlist
  counts.

Still intentionally not implemented:

- Email notification.
- Cloud Scheduler job creation or recurring automation.
- SQL editing, template mapping editing, or storage destination changes.
- Signed URL generation from schedule execution.
- Persisting per-report delivery allowlists on report definitions.

## Phase 9 Close-Out

Phase 9 stops at guarded manual execution for the main `report_definitions` scheduler.

The following work is intentionally split to Phase 10:

- Automatic Cloud Scheduler attachment for main `report_definitions` schedules.
- Notification or email delivery after scheduled generation.
- Persisting per-report delivery allowlists on report definitions.
- First production scheduled-delivery smoke with an explicitly eligible report definition.

Rationale:

- Main schedule automation has no current production eligible definition to run unattended.
- Delivery notification policy and allowlist ownership must be confirmed before any recurring job can create user-visible access.
- Rollback and duplicate-run handling must be validated with the first production target definition.
- Thermae Romae uses a separate dedicated scheduled endpoint and Cloud Scheduler job, so it does not change the main scheduler Phase 9 boundary.

## Phase 10 First Smoke Design

The Phase 10 first scheduled-delivery smoke design is tracked in
`docs/phase10-scheduled-delivery-smoke.md`.

Production inventory check on 2026-07-13 found no active eligible main
`report_definitions` target: all listed definitions were archived and both
schedule preview and schedule-runs dry-run returned zero checked items. Phase 10
therefore starts by explicitly approving or creating one eligible target before
any Cloud Scheduler attachment or notification automation.

## Phase 10 Delivery Allowlist Persistence

The next implementation adds a per-report delivery allowlist to report
definitions.

Implemented behavior:

- Admin API can save delivery allowlist metadata at
  `POST /report-definitions/<report_id>/delivery-allowlist`.
- Report definitions store normalized allowed domains and hashed allowed email
  identifiers only. Raw email addresses are not stored on report definitions.
- Public report definition responses include allowed domains and allowlist
  counts only. They do not include raw email addresses or email hashes.
- Admin UI can display and update the persisted delivery allowlist metadata.
  Raw email input is accepted only when saving and is cleared after the response.
- `execute_step=deliver` can use a persisted domain allowlist when request-time
  `allowed_domains` / `allowed_emails` are omitted.
- If neither request-time nor persisted allowlist data is available, the
  scheduled delivery candidate is skipped with `delivery_allowlist_required`
  before generation or delivery creation starts.

Still intentionally not implemented:

- Email notification.
- Cloud Scheduler job creation.
- Materializing stored email hashes into raw delivery email allowlists.

## Phase 10 Cloud Scheduler OIDC Endpoint

The runtime exposes a Cloud Scheduler-specific endpoint at:

`POST /admin/report-definitions/schedule-runs`

This endpoint is separate from the human/script admin API at
`POST /report-definitions/schedule-runs`.

Implemented behavior:

- Authentication is fail-closed unless
  `REPORT_DEFINITION_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS` is configured.
- Requests must include a bearer OIDC token whose email claim matches the
  configured service account allowlist.
- Token audience defaults to the endpoint URL and can be pinned with
  `REPORT_DEFINITION_SCHEDULER_AUDIENCE`.
- The endpoint builds the guarded execute payload internally, including the
  required confirmation strings and idempotency key.
- The default `execute_step` is `deliver`; it can be set to `generate` with
  `REPORT_DEFINITION_SCHEDULER_EXECUTE_STEP` or an explicit request payload.
- Request-time `allowed_domains` and `allowed_emails` are intentionally ignored
  by this endpoint. Recurring delivery must use the persisted per-report domain
  allowlist.

Still intentionally not implemented:

- Creating or modifying Cloud Scheduler jobs.
- Email notification.
- Posting download URLs to Slack.
