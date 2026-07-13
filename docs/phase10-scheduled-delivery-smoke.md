# Phase 10 Scheduled Delivery Smoke Design

Date: 2026-07-13

This document defines the first Phase 10 productionization step for the main
`report_definitions` scheduler.

Phase 9 intentionally stopped at guarded manual execution. Phase 10 starts by
choosing one explicitly eligible report definition and proving the scheduled
delivery path manually before any Cloud Scheduler attachment or notification
automation is enabled.

## Current Production Inventory

Safe metadata check on 2026-07-13:

- `GET /report-definitions?limit=100` returned 10 report definitions.
- All returned definitions were archived.
- `GET /report-definitions/schedule-preview?limit=100` returned zero checked,
  due, and scheduled items.
- `POST /report-definitions/schedule-runs` with dry-run mode returned zero
  checked, due, eligible, generated, delivered, and skipped items.

Conclusion: there is no current production-eligible target for the main
`report_definitions` scheduled-delivery smoke.

Do not unarchive old smoke definitions for production proof unless the target,
owner, schedule, template, query config, mapping version, storage destination,
and delivery allowlist are explicitly re-approved.

## First Target Selection Criteria

Select exactly one report definition for the first scheduled-delivery smoke.
The target must satisfy all of the following before execution:

1. The definition is active and not archived.
2. The report owner and primary operator are identified.
3. Schedule metadata is enabled with `frequency=monthly`.
4. The scheduled local date and time are known for the smoke window.
5. `current_version` points to the approved production version.
6. The current version has a published Excel template reference.
7. The current version has approved query config metadata.
8. The current version has approved template mapping metadata.
9. The configured GCS or Drive storage destination is already allowlisted.
10. The delivery allowlist is explicitly supplied for the smoke.
11. The same report and schedule window has no prior successful scheduled run.

The first target should be a low-risk production report whose output can be
validated by the operator before any recipient-facing notification is sent.

## Smoke Boundary

The first scheduled-delivery smoke may create:

- one generated workbook through the existing `report_id` generation path
- one active delivery record for the generated object
- one scheduled run record with safe metadata only

The smoke must not:

- send email
- post download URLs to Slack
- create or modify Cloud Scheduler jobs
- edit SQL
- edit template mappings
- upload or publish templates
- change storage destinations
- store or disclose secrets, PINs, raw email addresses, token fragments, Admin
  key fingerprints, IP addresses, user agents, Signed URLs, Drive URLs, GCS
  URIs, SQL text, template mapping details, template GCS URIs, Excel cell values,
  provider event JSON, raw idempotency keys, or local file paths

## Manual Smoke Sequence

Use the public service Admin API with `X-Admin-Key`, or the admin service through
IAP if the same endpoint is reachable there. Do not paste the Admin key into
Notion, Slack, GitHub, screenshots, or shell output.

1. Confirm the target definition is active and has the expected safe metadata.
2. Run dry-run for the selected report only:

```json
{
  "mode": "dry_run",
  "limit": 100,
  "report_ids": ["<target_report_id>"],
  "evaluation_time": "<scheduled_window_iso8601>"
}
```

Expected result:

- `counts.checked` is `1`
- `counts.due` is `1`
- `counts.eligible` is `1`
- item `action` is `would_execute`
- no delivery record is created
- no scheduled run record is created

3. Confirm the delivery guard rejects missing delivery confirmation:

```json
{
  "mode": "execute",
  "execute_step": "deliver",
  "confirm": "RUN_DUE_REPORTS",
  "confirm_generation": "GENERATE_REPORTS",
  "idempotency_key": "<operator-controlled-key>",
  "report_ids": ["<target_report_id>"],
  "evaluation_time": "<scheduled_window_iso8601>",
  "allowed_emails": ["<approved-operator-email>"]
}
```

Expected result: `400` with a generic delivery confirmation error.

4. Execute the delivery-record smoke once:

```json
{
  "mode": "execute",
  "execute_step": "deliver",
  "confirm": "RUN_DUE_REPORTS",
  "confirm_generation": "GENERATE_REPORTS",
  "confirm_delivery": "CREATE_DELIVERY_RECORDS",
  "idempotency_key": "<operator-controlled-key>",
  "report_ids": ["<target_report_id>"],
  "evaluation_time": "<scheduled_window_iso8601>",
  "allowed_emails": ["<approved-operator-email>"]
}
```

Expected result:

- `counts.delivered` is `1`
- item `action` is `delivery_created`
- the response contains a delivery id, expiry timestamp, report month, output
  file name, row counts when available, and allowlist counts only
- no download URL, Signed URL, raw email address, GCS URI, Drive URL, template
  URI, SQL text, or cell value is returned

5. Repeat the same request with the same idempotency key.

Expected result:

- no second generation or delivery is created
- the response marks the item as duplicate with safe metadata only

6. Verify runtime state:

- latest public and admin Cloud Run revisions have no new `severity>=ERROR`
  entries for the smoke window
- scheduled run record contains only safe fields
- delivery record is active and scoped to the intended allowlist
- no mail send event was created by the scheduled-delivery smoke
- no Cloud Scheduler job was created or modified

## Rollback

If the smoke fails before delivery creation:

- keep the failed scheduled run record for audit
- disable or archive the target report definition only if the target was created
  solely for smoke
- do not delete generated files unless a separate retention or cleanup approval
  exists

If a delivery record was created with an incorrect allowlist or output:

- disable that delivery record through the existing admin delivery controls
- record only the delivery id, reason, operator, and resolution summary
- rotate the Admin key only if break-glass handling exposed it or the key was
  used outside the expected path

If the target definition itself is wrong:

- set its schedule to disabled
- roll back `current_version` to the previous approved version if needed
- archive the definition only when it was created solely for smoke

No Cloud Run rollback is expected for this docs-only design step. A later code or
configuration change must include its own deploy and rollback plan.

## Next Decisions

After this design is accepted, proceed in this order:

1. Create or approve one active eligible report definition for the first smoke.
2. Run the dry-run and guarded delivery-record smoke manually.
3. Persist per-report delivery allowlist on report definitions if request-time
   allowlists are not acceptable for recurring operation.
4. Attach Cloud Scheduler only after the first manual scheduled-delivery smoke is
   successful and rollback conditions are recorded.
5. Decide notification or email policy after the delivery path is proven.

## Delivery Allowlist Persistence Update

Phase 10 adds report-definition-level delivery allowlist metadata before Cloud
Scheduler attachment.

The persisted allowlist is intentionally limited:

- allowed domains are stored normalized
- allowed email addresses are stored only as deterministic hashes and counts
- report definition responses return allowed domains and counts, not raw emails
  or hashes

Scheduled delivery execution may use the persisted domain allowlist when a
request-time allowlist is omitted. Stored email hashes are for audit and
configuration review only; they are not materialized back into raw email
allowlists for delivery records.

For the first manual smoke, an operator may still supply a request-time
`allowed_emails` value. That value must not be copied into Notion, Slack, GitHub,
logs, or screenshots.
