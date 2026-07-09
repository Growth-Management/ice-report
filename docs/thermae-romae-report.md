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

The Cloud Run runtime service account must be able to:

- Read the target BigQuery table.
- Read the Drive template file.
- Create files in the Drive output folder.

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
- `支払通知書` is updated at `D30`, `E42`, `E43`, `E44`, and `B53`.
- `支払通知書` print settings are preserved where possible and the expected print area / A4 /
  portrait / scale / horizontal centering settings are reinforced.
- `売上明細` is updated using the existing template mapping from `タイトル名` to `書籍コード`.
- If a BigQuery result title has no matching book code in the template, the API fails with
  `book_code_not_found`.

## Smoke

1. Confirm the Drive template file is shared with the Cloud Run runtime service account.
2. Confirm the Drive output folder allows the runtime service account to create files.
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
