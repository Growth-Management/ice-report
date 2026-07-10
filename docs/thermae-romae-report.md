# Thermae Romae Monthly Sales Report

This report is a dedicated Drive-output report for the monthly Thermae Romae sales statement.
It is intentionally separate from the main `report_definitions` / delivery / OTP flow.

## Scope

- API: `POST /admin/reports/thermae-romae/generate`
- Input: optional `target_month` in `YYYY-MM-DD`; it must be the first day of the target month.
- Default target month: previous month.
- Source table: `jumpplus-4a5f4.dataset_datamart_tables.report_plus_monthly_coin_content_report`
- Work IDs: `100040643`, `100040644`
- Template source: Drive `.xlsx` file, downloaded directly without Google Sheets conversion.
- Output: completed `.xlsx` uploaded to a Drive folder.

This implementation does not create a delivery record, send email, create OTP/PIN download URLs,
or attach Cloud Scheduler. Monthly automation is a later task.

## Environment

```text
THERMAE_TEMPLATE_FILE_ID=1KvfIA96o17oHfTp5dWMCByL8THxU_Txp
THERMAE_OUTPUT_FOLDER_ID=12kjj_xdQ-O6QAFl5QvDWXn4dUIYGlMCa
THERMAE_SOURCE_TABLE=jumpplus-4a5f4.dataset_datamart_tables.report_plus_monthly_coin_content_report
THERMAE_WORK_IDS=100040643,100040644
```

`BIGQUERY_PROJECT_ID`, `PROJECT_ID`, or `GOOGLE_CLOUD_PROJECT` is used for the BigQuery client
project.

Drive authentication defaults to the Cloud Run runtime service account through Application Default
Credentials. For initial implementation and the first several months of operations, use user OAuth
because the target Shared Drive policy blocks direct service account sharing:

```text
DRIVE_AUTH_MODE=oauth
DRIVE_OAUTH_CLIENT_ID_SECRET_NAME=drive-oauth-client-id
DRIVE_OAUTH_CLIENT_SECRET_SECRET_NAME=drive-oauth-client-secret
DRIVE_OAUTH_REFRESH_TOKEN_SECRET_NAME=drive-oauth-refresh-token
```

The initial OAuth subject is `sinohara@impress.co.jp`. Store the OAuth client secret and refresh
token only in Secret Manager or Cloud Run secret env vars. Do not commit them, paste them into
Notion, or include them in smoke output.

After several months of OAuth operation, evaluate Google Workspace domain-wide delegation so a
service account can act on behalf of a dedicated Workspace user. This is expected to apply to broader
ICE Report Generator Drive operations as well.

The Cloud Run runtime service account must be able to:

- Read the target BigQuery table.
- Read the Drive template file, unless user OAuth is enabled.
- Create files in the Drive output folder, unless user OAuth is enabled.
- Read OAuth Secret Manager secrets when `DRIVE_AUTH_MODE=oauth` and secret-name env vars are used.

Do not record service account credentials, Drive access tokens, Admin keys, or raw API tokens in
Notion, Slack, GitHub, logs, or screenshots.

## Manual Execution

```powershell
$body = @{ target_month = "2026-06-01" } | ConvertTo-Json
Invoke-RestMethod `
  -Uri "$env:SERVICE_URL/admin/reports/thermae-romae/generate" `
  -Method Post `
  -Headers @{ "X-Admin-Key" = $env:ADMIN_API_KEY; "Content-Type" = "application/json" } `
  -Body $body
```

When `target_month` is omitted, the API uses the first day of the previous month.

Successful responses include:

- `file_id`
- `file_name`
- `webViewLink`
- `target_month`
- `detail_row_count`
- `payment_total`
- `tax`
- `total_with_tax`

## Workbook Rules

- The template is opened as `.xlsx` with openpyxl.
- Google Sheets conversion is not used.
- `支払通知書` is updated at `G3`, `D30`, `E42`, `E43`, `E44`, and `B53`.
- `G3` is the generation date.
- `B53` is `※御支払いはyyyy年m月末を予定しております。`; the year/month is generation
  month + 3 months.
- `支払通知書` print settings are preserved where possible and the expected print area / A4 /
  portrait / scale / horizontal centering settings are reinforced.
- `売上明細` rows are fixed to the approved 54-row Thermae Romae code/title order in
  `thermae_romae_report.py`; missing BigQuery rows remain as zero-value rows.
- `売上明細` totals are fixed at `F56:G58`, with labels in `F56:F58`, amounts in `G56:G58`,
  and a thick outline around the totals range.
- If a BigQuery result title is not in the approved fixed code/title list, the API fails with
  `unexpected_title_name`.

## Smoke

1. Confirm the Drive template file is shared with the Cloud Run runtime service account.
2. Confirm the Drive output folder allows the runtime service account to create files.
   - If `DRIVE_AUTH_MODE=oauth`, confirm the OAuth user has access instead.
3. Run the API with an explicit historical `target_month`.
4. Confirm the response has Drive `file_id`, `file_name`, and `webViewLink`.
5. Open the uploaded `.xlsx` in Excel and confirm both target sheets are updated.
6. Confirm `支払通知書` print preview is not broken.
7. Confirm Cloud Logging does not contain secret, PIN, raw email, token fragments, Admin key
   fingerprint, IP, user agent, Signed URL, SQL text, Excel cell values, or provider event JSON.

## Rollback

- Route Cloud Run traffic back to the previous public/admin revisions.
- Revert the implementation PR if needed.
- Remove only test output files from Drive after explicit confirmation.
- Do not change existing `report_definitions`, delivery records, OTP/PIN flow, or scheduled
  executor settings during rollback.
