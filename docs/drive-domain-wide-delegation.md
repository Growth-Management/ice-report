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
server-side execution under Cloud Run. Treat this as a formal migration candidate after the initial
OAuth-based operating period has produced enough operational evidence.

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
