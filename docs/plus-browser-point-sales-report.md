# PLUS ブラウザ版 ポイント売上 月次レポート

This report is a dedicated Drive-output report for `PLUS_ブラウザ版_ポイント売上（月次Excel反映）`.
It is intentionally separate from the main `report_definitions` / delivery / OTP flow, following the
same pattern as the Thermae Romae report (`docs/thermae-romae-report.md`).

## Scope

- API: `POST /admin/reports/plus-browser-point-sales/generate`
- Input: optional `target_month` in `YYYY-MM-DD`; it must be the first day of the target month.
- Default target month: previous month, computed from the current time in `Asia/Tokyo` (not the
  container's local/UTC clock -- see "Target month calculation" below).
- Source table: `jumpplus-4a5f4.dataset_process_tables.daily_sbps_order_combined`
- Master tables: `jumpplus-4a5f4.dataset_exdata_tables.sbps_product`,
  `jumpplus-4a5f4.dataset_exdata_tables.sbps_payment_class`
- Template source: Drive `.xlsx` file, downloaded directly without Google Sheets conversion.
- Output: completed `.xlsx` uploaded to the same Drive folder as the template.

This implementation does not create a delivery record, send email, or create OTP/PIN download URLs.

## Environment

```text
PLUS_POINT_SALES_TEMPLATE_FILE_ID=1y2axBdZVfkeeJ4GiA76k8jqra4uAPiFf
PLUS_POINT_SALES_OUTPUT_FOLDER_ID=1FjFRdkQZz6yI5sRe00RSBiG3pGEwTNq_
PLUS_POINT_SALES_SOURCE_TABLE=jumpplus-4a5f4.dataset_process_tables.daily_sbps_order_combined
PLUS_POINT_SALES_PRODUCT_TABLE=jumpplus-4a5f4.dataset_exdata_tables.sbps_product
PLUS_POINT_SALES_PAYMENT_CLASS_TABLE=jumpplus-4a5f4.dataset_exdata_tables.sbps_payment_class
PLUS_POINT_SALES_SCHEDULED_RUNS_COLLECTION=plus_point_sales_scheduled_runs
PLUS_POINT_SALES_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS=thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com
PLUS_POINT_SALES_SCHEDULER_AUDIENCE=https://report-generator-635067190197.asia-northeast1.run.app/admin/reports/plus-browser-point-sales/scheduled-generate
```

No `DRIVE_AUTH_MODE` override or new Drive/scheduler service account is required -- see the two
"reuses..." sections below.

`BIGQUERY_PROJECT_ID`, `PROJECT_ID`, or `GOOGLE_CLOUD_PROJECT` is used for the BigQuery client
project.

### Drive authentication: reuses the existing runtime service account, no new SA

Unlike the Thermae report, this report does **not** need `DRIVE_AUTH_MODE=oauth`. The output folder
(`1FjFRdkQZz6yI5sRe00RSBiG3pGEwTNq_`, a Shared Drive folder, same Shared Drive as the template) is
already accessible to the existing Cloud Run runtime service account,
`ice-report-runner@ice-sh.iam.gserviceaccount.com`, via its membership in the
`drive-c82cc20a941861c4c25129822a2a736b@impress.co.jp` Shared Drive group. This was verified directly
by impersonating `ice-report-runner@ice-sh.iam.gserviceaccount.com` from a workstation with
`iam.serviceAccounts.getAccessToken` on it and, under `DRIVE_AUTH_MODE=adc`-equivalent credentials,
successfully downloading the template and uploading a test file to the output folder (the test file
was trashed afterwards). No new service account, Shared Drive membership change, or IAM grant is
required -- leave `DRIVE_AUTH_MODE` unset (defaults to `adc`) in Cloud Run, exactly as the main
`report_definitions` flow already does.

One minor, non-blocking observation from that test: the runtime SA's Shared Drive role can trash
files but not permanently delete them (a `files.delete` call 404s while `files.update(trashed=true)`
succeeds). This report never deletes Drive files, so it has no effect here.

## BigQuery aggregation

`plus_browser_point_sales_report.PLUS_SQL` reproduces the intake form's "受付時SQL" shape:

1. `actual`: `sum(sum_price)` from `daily_sbps_order_combined` grouped by `price, payment_class` for
   the target month.
2. `product_master` x `payment_master` (distinct, non-null `payment_class` only) cross-joined as
   `zero_fill` so every product x payment-class combination is present with `sum_price = 0`.
3. `union all` of `actual` and `zero_fill`, then `group by all` -- so real sales values win over the
   zero fill for every combination that actually sold.

The same query result feeds both `合計_決済別` (grouped by `payment_class` only) and
`合計_決済-商品別` (the full price x payment_class pivot). Manual and scheduled execution both call
`generate_plus_point_sales_report`, so aggregation logic is not duplicated between the two entry
points.

### payment_class format and sort order

`payment_class` values are `"<number>_<label>"`, e.g. `1_credit(JCB/AMEX)`, `10_suica`. Verified
against the live `sbps_payment_class` table (13 raw `res_pay_method`/`cardbrand_code` rows mapping to
10 distinct `payment_class` labels; one legacy `res_pay_method='credit'` combination has no mapping
and returns `payment_class IS NULL`, but only for historical rows dated before 2025-02-06 -- it does
not appear for any recent "previous month" target). Sort order is the numeric prefix, ascending
(`1, 2, 3, ..., 10`, not lexicographic `1, 10, 2, ...`), implemented in
`_payment_class_sort_key`/`payment_class_display_label`. Any `payment_class` that does not match
`^(\d+)_(.+)$` raises `PlusPointSalesReportError("unparseable_payment_class", ...)` instead of
silently sorting it wrong. Any row with `payment_class IS NULL` and a non-zero `sum_price` raises
`PlusPointSalesReportError("unexpected_payment_class", ...)` instead of silently dropping yen amounts.

### Product price -> template column label (confirmed business spec)

`price` (yen) is the product identifier and aggregation key throughout this module.
`sbps_product`/`daily_sbps_order_combined` also carry a `product_name` (e.g. `"300pt"`), but that is a
separate, purely informational BigQuery-side label -- it is never used for the Excel mapping and must
not be confused with the Excel column label.

The Excel template's `合計_決済-商品別` columns use the official bonus-inclusive "granted points" label
for each price tier. This is the confirmed mapping (business spec, not inferred):

| `price` (yen) | Excel point label |
| --- | --- |
| 100 | 100pt |
| 300 | 310pt |
| 400 | 410pt |
| 500 | 520pt |
| 600 | 630pt |
| 1,000 | 1050pt |
| 2,000 | 2150pt |
| 3,000 | 3250pt |
| 5,000 | 5450pt |
| 9,800 | 10800pt |

This is implemented as the static `PRICE_TO_POINT_LABEL` dict in `plus_browser_point_sales_report.py`.
It is intentionally static and is never auto-extended: when a new price tier is introduced in the
future, an engineer must add the new `price -> label` pair here and update the Excel template's
columns as a deliberate, explicit change -- see "Future product tiers" below.

If `sbps_product`/`daily_sbps_order_combined` ever reports a `price` outside this 10-entry map, report
generation fails closed with `PlusPointSalesReportError("unexpected_product_price", ...)`
unconditionally (even for a zero-fill row with `sum_price == 0`), so a newly introduced price tier is
caught the first month it appears rather than silently mapped to the wrong column or dropped.

## Excel workbook rules

- Both sheets (`合計_決済別`, `合計_決済-商品別`) are native Excel Tables (`ListObject`s), each with a
  totals row (`totalsRowCount=1`) whose `合計`/`TOTAL` row already uses whole-column structured
  references, e.g. `=SUBTOTAL(109,合計_決済別[売上])` and
  `=SUBTOTAL(109,合計_決済_商品別[100pt])..[TOTAL]`. These formulas are never rewritten; because they
  reference the whole table column rather than a fixed cell range, they keep summing correctly once
  the table's row count grows.
- The per-row `TOTAL` column (`L`) in `合計_決済-商品別` was blank in the template (no formula). This
  implementation writes a live formula per row,
  `=SUM(合計_決済_商品別[@[100pt]:[10800pt]])`, rather than a static number, for consistency with the
  column-level `SUBTOTAL` formulas.
- New `payment_class` values are handled by `_extend_table_rows`: it inserts rows directly above the
  existing totals row, copies cell style/number-format from the row that used to be just above the
  totals row, and updates the table's `ref` (and `autoFilter.ref`) to cover the new range. This only
  ever grows the table -- the template itself in Drive is never modified (each run downloads a fresh
  copy), so growth is recomputed from scratch every month based on that month's distinct
  `payment_class` values. Verified by simulating an 11th payment class: the table grew from
  `A1:L12` to `A1:L13`, the totals row moved to row 13 with its `SUBTOTAL` formula text unchanged, and
  the new row's number format (`#,##0`) matched the existing rows.
- Column extension (adding an 11th product/point column) is intentionally out of scope, unlike
  `payment_class` row growth. `sbps_product` currently has exactly 10 rows, matching the template's 10
  product columns and `PRICE_TO_POINT_LABEL`'s 10 entries; this is asserted at generation time
  (`unexpected_product_price`) rather than silently handled.
- The original template file in Drive is only ever downloaded, never overwritten.

### Future product tiers

Product/point-label growth is deliberately **not** automatic (unlike `payment_class` row growth,
which is). If `sbps_product` gains a new price tier, generation fails closed with
`unexpected_product_price` rather than guessing a column. To add a new tier: an engineer adds the
`price -> label` pair to `PRICE_TO_POINT_LABEL` in `plus_browser_point_sales_report.py` and adds the
matching column (with its own `SUBTOTAL` formula, following the existing columns' pattern) to the
Excel template in Drive, as one deliberate, reviewed change -- never inferred at runtime.

## Target month calculation

Cloud Scheduler runs at 07:00 JST on the 1st of the month, which is 22:00 UTC on the *previous* day.
Using the container's naive `date.today()` (UTC, as on Cloud Run) at that moment would resolve to the
wrong calendar day and therefore the wrong target month. `tokyo_today()` explicitly computes today's
date in `Asia/Tokyo` (`datetime.now(ZoneInfo("Asia/Tokyo")).date()`) before deriving the previous
month, for both the manual and scheduled endpoints. (This deliberately differs from the Thermae
report, whose scheduler runs on the 2nd at 09:00 JST -- far enough from the UTC day boundary that the
naive `date.today()` there is not at risk the same way.)

## Manual execution

```powershell
$body = @{ target_month = "2026-08-01" } | ConvertTo-Json
Invoke-RestMethod `
  -Uri "$env:SERVICE_URL/admin/reports/plus-browser-point-sales/generate" `
  -Method Post `
  -Headers @{ "X-Admin-Key" = $env:ADMIN_API_KEY; "Content-Type" = "application/json" } `
  -Body $body
```

When `target_month` is omitted, the API uses the first day of the previous month (Asia/Tokyo).
Successful responses include `file_id`, `file_name`, `webViewLink`, `target_month`,
`payment_class_count`, `grand_total`.

## Scheduled execution

```text
POST /admin/reports/plus-browser-point-sales/scheduled-generate
```

- Cloud Scheduler must call the endpoint with an OIDC token; the service account email must be in
  `PLUS_POINT_SALES_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS` and the token audience must match
  `PLUS_POINT_SALES_SCHEDULER_AUDIENCE`. Fail-closed when the allowlist is not configured (shared
  `_check_scheduler_oidc_auth` helper, same as Thermae and `report_definitions`).
- A Firestore run record is created in `PLUS_POINT_SALES_SCHEDULED_RUNS_COLLECTION`, document id
  `YYYY-MM`, via the shared `_claim_scheduled_run` helper. A duplicate scheduled call for the same
  target month returns `409`.
- The scheduled response/run record only ever include safe metadata (target month, generated date,
  file name, whether a Drive file exists, payment class count, grand total) -- never Drive file id,
  URL, token, raw email, IP, user agent, SQL text, or Excel cell values.

### Scheduler service account: reuses an existing scheduler-invoking SA, no new SA

The default new-bespoke-report playbook (`docs/new-bespoke-report-playbook.md`) recommends creating a
dedicated `<report-name>-scheduler@ice-sh.iam.gserviceaccount.com` per report. For this report that
step is skipped by explicit decision: `_check_scheduler_oidc_auth` only checks the caller's OIDC
`email` claim against an allowlist and the token audience against a per-endpoint URL -- it has no
dependency on the SA being single-purpose. Reusing the existing
`thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com` identity (already used to invoke the
structurally identical `POST /admin/reports/thermae-romae/scheduled-generate`) for this report's job
as well works with the same mechanism and requires no new IAM: Cloud Run is deployed with
`--allow-unauthenticated`, so no `roles/run.invoker` grant is needed either, and minting an OIDC token
for a second audience URL with an SA that can already mint tokens does not require any additional
grant on that SA.

```powershell
gcloud.cmd scheduler jobs create http plus-browser-point-sales-monthly-report `
  --project=ice-sh `
  --location=asia-northeast1 `
  --schedule="0 7 1 * *" `
  --time-zone="Asia/Tokyo" `
  --uri="https://report-generator-635067190197.asia-northeast1.run.app/admin/reports/plus-browser-point-sales/scheduled-generate" `
  --http-method=POST `
  --oidc-service-account-email="thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com" `
  --oidc-token-audience="https://report-generator-635067190197.asia-northeast1.run.app/admin/reports/plus-browser-point-sales/scheduled-generate"
```

```text
PLUS_POINT_SALES_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS=thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com
```

If whoever owns Cloud Scheduler prefers a cleaner separation between reports, any other
*already-existing* scheduler-invoking SA (e.g. the one behind
`REPORT_DEFINITION_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS`) works equally well -- the only requirement is
that it is an SA the deploying operator already has `iam.serviceAccounts.actAs` on (true for any SA
already used in an existing Cloud Scheduler job). The one thing to avoid is provisioning a brand new
SA solely for this report.

## Same-month re-run behavior

- **Manual endpoint**: no dedup. Each call creates a new Drive file (Drive allows duplicate names);
  this matches the existing Thermae report's behavior. Re-running the same month manually will leave
  multiple files with the same name in the output folder -- delete the unwanted one manually if that
  happens.
- **Scheduled endpoint**: blocked. A second scheduled call for a `target_month` that already has a
  Firestore run record (`PLUS_POINT_SALES_SCHEDULED_RUNS_COLLECTION/YYYY-MM`) returns `409
  duplicate_scheduled_run`.

## Verification performed during implementation

- BigQuery aggregation (both `合計_決済別` and `合計_決済-商品別` shapes) was cross-checked against the
  live reference spreadsheet
  (`https://docs.google.com/spreadsheets/d/1fRH7FGmWxtQF2bfjz2djGQS3H8VyGN1bN2e0fVqlGVI`) for target
  month 2026-08: every payment-class total and the grand total (9,589,100) matched exactly. For that
  same month, `payment_class IS NULL` rows summed to 0, so the `unexpected_payment_class` guard was
  not triggered (it remains active for any future month where it would be).
- The generated workbook was reloaded with `openpyxl` and its OOXML parts (all `.xml`/`.rels` entries)
  were parsed to confirm the zip package is well-formed.
- The row-extension path (`_extend_table_rows`) was exercised with a simulated 11th `payment_class`:
  both tables grew by one row, `SUBTOTAL` formula text was unchanged (structured references still
  resolve correctly), number formats were copied to the new row, and `autoFilter.ref` was updated.
- **Drive read+write, as the actual production runtime identity**: verified end-to-end by
  impersonating `ice-report-runner@ice-sh.iam.gserviceaccount.com` and, under the same ADC code path
  `drive_io.py` uses in production, successfully downloading the template and uploading a file to the
  production output folder (then trashing that test file). This confirms Cloud Run needs no Drive
  configuration change at all for this report.
- Live recalculation inside Excel itself (COM automation) could not be exercised in this sandboxed
  environment (Excel COM automation failed to open any workbook here, independent of this change).
  Per the updated verification policy for this report, this is **not a pre-push blocker** -- see
  "Post-deploy smoke test" below.

## Post-deploy smoke test (not required before opening the PR)

Once a deployed (or locally-run-against-real-credentials) instance has produced a real `.xlsx` via
`POST /admin/reports/plus-browser-point-sales/generate`, download that file and confirm:

1. It opens in Excel without a repair-warning prompt.
2. `合計_決済別` and `合計_決済-商品別` both show the expected data and the totals row recalculates
   correctly (Excel, unlike this repo's automated checks, actually evaluates `SUBTOTAL`/`SUM`).
3. If Cloud Scheduler has been configured, confirm a duplicate scheduled call for the same month
   returns `409` and that the OIDC auth check accepts the configured scheduler SA.

## Open items

The product-column-label question that previously blocked this report is resolved (see "Product price
-> template column label" above). Remaining work, none of which blocks opening a PR:

1. Open a real generated `.xlsx` in Excel and confirm no repair warning (see "Post-deploy smoke test").
2. ~~Confirm the existing runtime SA can write to the output Drive folder~~ -- done, see "Drive
   authentication" above (verified via impersonation, no new SA/IAM needed).
3. Create the Cloud Scheduler job against a reused existing scheduler SA (e.g.
   `thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com`) and confirm the OIDC auth check accepts
   it end-to-end, plus the `409` duplicate-run behavior.
4. Local `.venv` setup fails on this machine's Python 3.14 because `pandas==2.2.2` has no prebuilt
   wheel for 3.14 and its source build fails (`vswhere.exe` / meson error). This is a pre-existing
   environment issue unrelated to this report; noted for reference only, not addressed here.
