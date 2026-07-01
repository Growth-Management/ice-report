# Published Template Runtime Switch

This document defines the first safe step for applying published Excel templates to report generation.

## Goal

Use the current published template from Firestore `report_definitions` when an admin explicitly generates a report for a `report_id`.

Implementation status: report_id-gated runtime switch is implemented for `POST /generate` and `POST /deliveries` generation. Existing generation without `report_id` remains the fallback path.

The switch must keep existing report generation working:

- No `report_id`: continue using `TEMPLATE_PATH` or bundled `templates/template.xlsx`.
- `report_id` with a current version that has `template_gcs_uri`: download that template to `/tmp` and pass it to `generate_report`.
- `report_id` with no published template: fail closed by default before generation.

## Non-goals

This step does not implement:

- SQL editing or query config publish.
- Template mapping editing or dynamic cell mapping.
- Schedule automation.
- Arbitrary Drive or GCS destination changes.
- Public service-wide IAP.

## Runtime Contract

Admin generation endpoints accept an optional `report_id`.

Target endpoints:

- `POST /generate`
- `POST /deliveries` when `gcs_uri` is omitted and the service generates the file

Resolution order:

1. If `report_id` is absent, use existing `TEMPLATE_PATH` behavior.
2. If `report_id` is present, read the report definition from Firestore.
3. Find the definition's `current_version`.
4. Require the current version to include `template_gcs_uri`.
5. Validate the URI is a `gs://` URI.
6. Download the template object to a unique file under `/tmp`.
7. Pass the local path to `generate_report`.
8. Delete the temporary file after the request completes when practical.

The response can include non-sensitive metadata:

- `report_id`
- `report_definition_version`
- `template_name`
- `template_sha256`

The response must not include `template_gcs_uri`.

## Failure Behavior

When `report_id` is present, failures stop before BigQuery execution and before delivery creation:

| Condition | Result |
| --- | --- |
| report definition not found | `400` / `404` with generic error |
| archived report definition | `400` |
| current version missing | `400` |
| current version has no `template_gcs_uri` | `400` |
| template object cannot be downloaded | `500` with generic error |
| template workbook invalid | existing workbook validation error |

The implementation should not silently fall back to bundled template when `report_id` is present. Silent fallback would make the selected definition misleading.

## Logging Rules

Allowed log fields:

- action
- result
- status code
- report_id
- report definition version
- template name
- safe reason code

Forbidden in logs, UI, and API responses:

- `template_gcs_uri`
- Signed URL
- secret
- PIN
- raw email
- token fragment
- Admin key fingerprint
- IP address
- user agent
- Excel cell values
- SQL text
- template mapping details

Use fixed failure messages for unexpected exceptions, for example:

- `ICE_REPORT_TEMPLATE_RESOLVE_FAILED`
- `ICE_REPORT_TEMPLATE_DOWNLOAD_FAILED`
- `ICE_REPORT_GENERATE_WITH_REPORT_DEFINITION_FAILED`

## Data Model

The existing published template version document is sufficient for this step:

```text
versions[].version
versions[].status
versions[].template_name
versions[].template_gcs_uri
versions[].template_sha256
versions[].template_size_bytes
versions[].created_at
versions[].updated_at
```

No Firestore schema migration is required.

## Implementation Shape

Implemented helpers in `distribution.py`:

- `get_report_definition_runtime_template(report_id)`: returns internal current version metadata, including `template_gcs_uri`.
- `download_report_definition_template(report_id, destination_dir)`: downloads the current template to a local temp path and returns safe public metadata plus the local path.

Keep `_public_report_definition` unchanged so read-only Admin APIs continue excluding storage internals.

Implemented app-level wrapper:

- Resolve the template path before calling `generate_report`.
- Include safe metadata in audit detail.
- Remove temporary files in `finally`.

## Admin UI

Initial UI change should be minimal:

- Add optional `report_id` selector or hidden binding from selected report definition to delivery generation.
- Show the selected report definition name and current version before generation.
- Do not show `template_gcs_uri`.

## Smoke

Use a test report definition with a published template.

1. Create or reuse a test definition.
2. Publish a `.xlsx` template to create a current version with `template_gcs_uri`.
3. Run `/generate` with `report_id`.
4. Confirm generated workbook exists in the expected output GCS prefix.
5. Run `/deliveries` with `report_id` and no `gcs_uri`.
6. Confirm delivery is created and points to the generated workbook.
7. Confirm OTP / download flow is unchanged.
8. Confirm logs do not include forbidden fields.
9. Confirm `report_id` absent path still uses existing template behavior.

## Rollback

Operational rollback options:

1. Stop passing `report_id` from Admin UI/API clients.
2. Roll Cloud Run back to the previous public/admin revisions.
3. If a bad template was published, use template rollback to move `current_version` to the prior template version.

Because this step only changes runtime template selection when `report_id` is present, existing generation without `report_id` remains the fallback path.
