from __future__ import annotations

import os

import google.auth
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class SheetsOperationError(Exception):
    def __init__(self, code: str, *, status_code: int = 500) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _raise_sheets_error(exc: Exception) -> None:
    if isinstance(exc, SheetsOperationError):
        raise exc
    if isinstance(exc, RefreshError):
        raise SheetsOperationError("sheets_oauth_refresh_failed", status_code=500) from exc
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 500) or 500)
        code = "sheets_access_denied" if status in {401, 403} else (
            "sheets_not_found" if status == 404 else "sheets_api_error"
        )
        raise SheetsOperationError(code, status_code=status) from exc
    raise exc


def _project_id() -> str:
    return (
        os.environ.get("SHEETS_OAUTH_SECRET_PROJECT_ID")
        or os.environ.get("DRIVE_OAUTH_SECRET_PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or ""
    )


def _access_secret(secret_name: str, *, project_id: str | None = None) -> str:
    """Identical to drive_io._access_secret -- duplicated rather than imported
    to keep this module self-contained, matching this codebase's existing
    convention of not sharing helpers between report/io modules."""
    name = secret_name.strip()
    if not name:
        return ""

    if name.startswith("projects/"):
        secret_version = name
    else:
        project = project_id or _project_id()
        if not project:
            raise RuntimeError("sheets_oauth_secret_project_required")
        secret_version = f"projects/{project}/secrets/{name}/versions/latest"

    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": secret_version})
    return response.payload.data.decode("utf-8").strip()


def _config_value(env_name: str, secret_env_name: str, *, fallback_env_name: str = "", fallback_secret_env_name: str = "") -> str:
    direct = os.environ.get(env_name, "").strip()
    if direct:
        return direct

    secret_name = os.environ.get(secret_env_name, "").strip()
    if secret_name:
        return _access_secret(secret_name)

    # Falls back to drive_io's DRIVE_OAUTH_* config so ops can reuse a single
    # sinohara@impress.co.jp OAuth client/refresh token that already carries
    # both the Drive and Sheets scopes, instead of provisioning a second,
    # Sheets-only credential -- set SHEETS_OAUTH_* explicitly only if a
    # separate credential is actually wanted.
    if fallback_env_name:
        fallback_direct = os.environ.get(fallback_env_name, "").strip()
        if fallback_direct:
            return fallback_direct
    if fallback_secret_env_name:
        fallback_secret_name = os.environ.get(fallback_secret_env_name, "").strip()
        if fallback_secret_name:
            return _access_secret(fallback_secret_name)
    return ""


def _sheets_oauth_credentials():
    client_id = _config_value(
        "SHEETS_OAUTH_CLIENT_ID",
        "SHEETS_OAUTH_CLIENT_ID_SECRET_NAME",
        fallback_env_name="DRIVE_OAUTH_CLIENT_ID",
        fallback_secret_env_name="DRIVE_OAUTH_CLIENT_ID_SECRET_NAME",
    )
    client_secret = _config_value(
        "SHEETS_OAUTH_CLIENT_SECRET",
        "SHEETS_OAUTH_CLIENT_SECRET_SECRET_NAME",
        fallback_env_name="DRIVE_OAUTH_CLIENT_SECRET",
        fallback_secret_env_name="DRIVE_OAUTH_CLIENT_SECRET_SECRET_NAME",
    )
    refresh_token = _config_value(
        "SHEETS_OAUTH_REFRESH_TOKEN",
        "SHEETS_OAUTH_REFRESH_TOKEN_SECRET_NAME",
        fallback_env_name="DRIVE_OAUTH_REFRESH_TOKEN",
        fallback_secret_env_name="DRIVE_OAUTH_REFRESH_TOKEN_SECRET_NAME",
    )

    missing = [
        name
        for name, value in (
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("refresh_token", refresh_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("sheets_oauth_config_missing")

    return OAuthCredentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=os.environ.get("SHEETS_OAUTH_TOKEN_URI", DEFAULT_TOKEN_URI),
        client_id=client_id,
        client_secret=client_secret,
        scopes=[SHEETS_READONLY_SCOPE],
    )


def _sheets_credentials():
    """Mirrors drive_io._drive_credentials(). Production uses SHEETS_AUTH_MODE=oauth
    (same sinohara@impress.co.jp user OAuth identity drive_io.py uses under
    DRIVE_AUTH_MODE=oauth) -- Shared Drive policy blocks sharing "PLUS_広告費"
    directly with the ice-report-runner service account, exactly as it blocked
    direct Drive folder sharing (see docs/drive-domain-wide-delegation.md).
    `adc` remains available for local/dev use where the caller's own ADC
    identity already has access to the sheet.
    """
    mode = os.environ.get("SHEETS_AUTH_MODE", "adc").strip().lower()
    if mode in {"oauth", "oauth_user", "user_oauth"}:
        return _sheets_oauth_credentials()
    if mode not in {"", "adc", "service_account"}:
        raise RuntimeError("sheets_auth_mode_unsupported")

    credentials, _ = google.auth.default(scopes=[SHEETS_READONLY_SCOPE])
    return credentials


def get_sheets_service():
    credentials = _sheets_credentials()
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_sheet_values(spreadsheet_id: str, range_name: str, *, service=None) -> list[list]:
    """Read a range as raw (UNFORMATTED_VALUE) rows -- numbers stay numbers, dates
    come back as either an ISO-ish string or a Sheets serial-date number depending
    on the source cell's format. Callers must handle both (see
    jumpplus_ad_revenue_report._parse_sheet_month_cell)."""
    service = service or get_sheets_service()
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueRenderOption="UNFORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            )
            .execute()
        )
    except Exception as exc:
        _raise_sheets_error(exc)

    return response.get("values", [])
