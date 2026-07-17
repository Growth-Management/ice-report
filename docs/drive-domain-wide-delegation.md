# Drive Domain-wide Delegation Design

This document defines the future target design for replacing the initial user OAuth Drive
operations model with Google Workspace domain-wide delegation.

## Current State

ICE Report Generator can use two Drive authentication modes:

- `DRIVE_AUTH_MODE=adc`: Cloud Run runtime service account through Application Default Credentials.
- `DRIVE_AUTH_MODE=oauth`: initial operations mode using Secret Manager injected client and refresh
  token values.

The OAuth subject is `sinohara@impress.co.jp`. This was introduced because Shared Drive policy
blocked direct sharing to the Cloud Run service account. Initial implementation and the first
several months of operations should continue with this OAuth mode while usage, rotation cadence, and
Drive ownership requirements are observed.

On 2026-07-14, a no-traffic Cloud Run smoke confirmed that `DRIVE_AUTH_MODE=adc` can read a
template and create a completed `.xlsx` in a Shared Drive when the Cloud Run runtime service account
is added as a member of the Shared Drive itself. Folder-only sharing had previously failed with a
Drive not found style error. Treat Shared Drive membership for
`ice-report-runner@ice-sh.iam.gserviceaccount.com` as the preferred near-term alternative before
implementing domain-wide delegation, where Workspace policy allows it.

## Service Account Shared Drive Membership Alternative

This option keeps Drive access under the Cloud Run runtime service account and does not require
user OAuth or domain-wide delegation.

Required shape:

- `DRIVE_AUTH_MODE=adc`
- Runtime service account: `ice-report-runner@ice-sh.iam.gserviceaccount.com`
- The runtime service account is a member of the target Shared Drive, not only a folder-level share.
- The template file and output folder are in that Shared Drive or otherwise readable/writable by the
  runtime service account.

Use this option when:

- The Shared Drive can accept the runtime service account as a member.
- A system-owned Drive identity is sufficient.
- User impersonation, per-user ownership, or user-level audit identity is not required.

Do not use this option as proof that Google Group based access works unless the smoke is repeated
with the group as the Shared Drive member. Connector uploads by an operator account are only setup
steps; the success condition is a Cloud Run runtime call creating the output file with ADC.

Smoke result to keep as baseline:

- Date: 2026-07-14 JST.
- Service: `report-generator`.
- Revision type: no-traffic tagged smoke revision.
- Auth mode: `DRIVE_AUTH_MODE=adc`.
- Result: `POST /admin/reports/thermae-romae/generate` succeeded and created the output `.xlsx` in
  the target Shared Drive folder.
- Cleanup: smoke tag removed, production traffic returned to the previous stable revision at 100%.

Important follow-up:

- On 2026-07-17, a no-traffic smoke using the production Thermae template and the already-validated
  test output folder failed with `drive_not_found`.
- The production Thermae template/output folder is in a different Shared Drive from the successful
  2026-07-14 test folder.
- Do not switch production Thermae Drive access from OAuth to ADC until the runtime service account
  has been added to the production Thermae Shared Drive itself and a no-traffic smoke succeeds.
- The 2026-07-17 smoke did not write to the production output folder.

## Target State

Use a Google-managed service account with domain-wide delegation to impersonate a dedicated
Workspace user for Drive operations.

Target shape:

- Cloud Run runtime service account: `ice-report-runner@ice-sh.iam.gserviceaccount.com`
- Drive delegated subject: a dedicated Workspace user, for example
  `ice-report-drive-automation@impress.co.jp`
- Scope: `https://www.googleapis.com/auth/drive`
- Drive template and output folders are shared with the delegated Workspace user, not with a
  personal operator account.

This should apply to Thermae Romae Drive output first, then be generalized for broader ICE Report
Generator Drive backup and report output operations.

## Why Not Keep User OAuth Permanently

User OAuth is the approved initial operations model, but it is not the preferred permanent model:

- It depends on an individual user's refresh token lifecycle.
- Token revocation, account changes, or consent changes can break automation.
- Audit ownership is tied to a person rather than a system actor.
- Client secret and refresh token rotation become operationally sensitive recurring work.

Domain-wide delegation can move the operational identity to a dedicated Workspace user while keeping
server-side execution under Cloud Run. Treat this as a formal migration candidate only when the
Shared Drive membership alternative is not sufficient, or when a user-delegated identity is required.

## Google Workspace Prerequisites

Workspace Super Admin action is required.

1. Create or choose a service account intended for Drive delegated access.
2. Enable domain-wide delegation for that service account.
3. In Google Admin console, authorize the service account client ID for:
   - `https://www.googleapis.com/auth/drive`
4. Create or choose the delegated Workspace user.
5. Grant that delegated user access to the required Shared Drives, template files, and output
   folders.

Official references:

- Google service account OAuth and domain-wide delegation:
  https://developers.google.com/identity/protocols/oauth2/service-account
- Google Workspace Admin domain-wide delegation controls:
  https://support.google.com/a/answer/162106
- Google Drive Shared Drives API behavior:
  https://developers.google.com/workspace/drive/api/guides/about-shareddrives

## Runtime Configuration

Add a third Drive auth mode only after approval:

```text
DRIVE_AUTH_MODE=domain_wide_delegation
DRIVE_DELEGATED_SUBJECT=ice-report-drive-automation@impress.co.jp
DRIVE_DELEGATED_SCOPES=https://www.googleapis.com/auth/drive
```

The implementation should avoid long-lived service account keys if possible. Prefer the Cloud Run
runtime identity and Google Auth libraries. If a key becomes unavoidable, it must be treated as a
temporary exception with explicit rotation and retirement tasks.

## Implementation Outline

1. Add `domain_wide_delegation` as a supported `DRIVE_AUTH_MODE`.
2. Build delegated Drive credentials with the configured subject.
3. Keep current `adc` and `oauth` behavior unchanged.
4. Add sanitized error codes:
   - `drive_delegated_subject_missing`
   - `drive_domain_delegation_not_authorized`
   - `drive_access_denied`
   - `drive_not_found`
5. Add unit tests for mode selection and missing subject handling.
6. Deploy without enabling the mode first.
7. Enable the mode only after Workspace Admin configuration and Drive sharing are confirmed.

## Smoke Plan

Use a historical target month and avoid exposing Drive URLs or token material.

1. Confirm Cloud Run revision has:
   - `DRIVE_AUTH_MODE=domain_wide_delegation`
   - `DRIVE_DELEGATED_SUBJECT` set to the dedicated Workspace user
2. Run `POST /admin/reports/thermae-romae/generate` with explicit `target_month`.
3. Confirm response fields:
   - `target_month`
   - `detail_row_count`
   - `payment_total`
   - `tax`
   - `total_with_tax`
   - `has_file_id`
   - `file_name`
   - `has_webViewLink`
4. Confirm the generated `.xlsx` opens in Excel and print preview is not broken.
5. Confirm logs do not contain:
   - secret values
   - refresh tokens
   - authorization codes
   - Admin key
   - Signed URLs
   - raw Drive URLs
   - token fragments
   - SQL text
   - Excel cell values

## Transition / Rollback

During the initial operations period, OAuth remains the baseline. If domain-wide delegation is later
enabled, preferred rollback order is:

1. Set `DRIVE_AUTH_MODE=oauth` to return to the approved initial operations mode.
2. If needed, set `DRIVE_AUTH_MODE=adc` and revert to service account access for locations that
   support it.
3. Redeploy the previous Cloud Run revision only if auth mode rollback is insufficient.

Do not remove the OAuth secrets until domain-wide delegation has completed several successful
monthly runs and rollback is no longer needed.

## Open Decisions

- Exact delegated Workspace user address.
- Whether to use the existing runtime service account or a dedicated Drive delegation service
  account.
- Whether Workspace policy permits the delegated user to access the target Shared Drives.
- Whether this mode should become the default for all Drive backup operations or only for report
  outputs that require Shared Drive access.
- Target timing for reconsidering OAuth after the first several months of operations.
- Whether Shared Drive membership for the runtime service account is acceptable as the permanent
  Drive access model for Thermae Romae and other Drive-output reports.
