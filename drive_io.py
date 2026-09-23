from __future__ import annotations

import logging
import os
from pathlib import Path

import google.auth
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


DRIVE_XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Passed to next_chunk()/execute() so the google-api-python-client's own
# exponential-backoff retry handles transient 5xx/connection errors during
# upload. Do not wrap this in an additional custom retry loop -- that would
# double-retry the same transient failure.
DRIVE_UPLOAD_NUM_RETRIES = 3


class DriveOperationError(Exception):
    def __init__(self, code: str, *, status_code: int = 500) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _drive_http_error_code(exc: HttpError) -> str:
    status = int(getattr(exc.resp, "status", 500) or 500)
    if status in {401, 403}:
        return "drive_access_denied"
    if status == 404:
        return "drive_not_found"
    return "drive_api_error"


def _sanitized_http_status(exc: Exception) -> str:
    """HTTP status only -- never the exception's message/reason, which can
    echo request details. Used for logging, never for the raised error."""
    if isinstance(exc, HttpError):
        return str(int(getattr(exc.resp, "status", 0) or 0))
    return "unknown"


def _raise_drive_error(exc: Exception) -> None:
    if isinstance(exc, DriveOperationError):
        raise exc
    if isinstance(exc, RefreshError):
        raise DriveOperationError("drive_oauth_refresh_failed", status_code=500) from exc
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 500) or 500)
        raise DriveOperationError(_drive_http_error_code(exc), status_code=status) from exc
    raise exc


def _project_id() -> str:
    return (
        os.environ.get("DRIVE_OAUTH_SECRET_PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or ""
    )


def _access_secret(secret_name: str, *, project_id: str | None = None) -> str:
    name = secret_name.strip()
    if not name:
        return ""

    if name.startswith("projects/"):
        secret_version = name
    else:
        project = project_id or _project_id()
        if not project:
            raise RuntimeError("drive_oauth_secret_project_required")
        secret_version = f"projects/{project}/secrets/{name}/versions/latest"

    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": secret_version})
    return response.payload.data.decode("utf-8").strip()


def _config_value(env_name: str, secret_env_name: str) -> str:
    direct = os.environ.get(env_name, "").strip()
    if direct:
        return direct

    secret_name = os.environ.get(secret_env_name, "").strip()
    if not secret_name:
        return ""
    return _access_secret(secret_name)


def _drive_oauth_credentials():
    client_id = _config_value("DRIVE_OAUTH_CLIENT_ID", "DRIVE_OAUTH_CLIENT_ID_SECRET_NAME")
    client_secret = _config_value("DRIVE_OAUTH_CLIENT_SECRET", "DRIVE_OAUTH_CLIENT_SECRET_SECRET_NAME")
    refresh_token = _config_value("DRIVE_OAUTH_REFRESH_TOKEN", "DRIVE_OAUTH_REFRESH_TOKEN_SECRET_NAME")

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
        raise RuntimeError("drive_oauth_config_missing")

    return OAuthCredentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=os.environ.get("DRIVE_OAUTH_TOKEN_URI", DEFAULT_TOKEN_URI),
        client_id=client_id,
        client_secret=client_secret,
        scopes=[DRIVE_SCOPE],
    )


def _drive_credentials():
    mode = os.environ.get("DRIVE_AUTH_MODE", "adc").strip().lower()
    if mode in {"oauth", "oauth_user", "user_oauth"}:
        return _drive_oauth_credentials()
    if mode not in {"", "adc", "service_account"}:
        raise RuntimeError("drive_auth_mode_unsupported")

    credentials, _ = google.auth.default(scopes=[DRIVE_SCOPE])
    return credentials


def get_drive_service():
    credentials = _drive_credentials()
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_drive_files(
    *,
    folder_id: str,
    name_contains: str | None = None,
    mime_type: str | None = None,
    limit: int = 20,
    service=None,
) -> list[dict]:
    """List non-trashed files directly inside a Drive folder, newest first.

    Read-only helper shared by any admin UI that needs to show what a report
    has produced (Shared Drive aware via supportsAllDrives/includeItemsFromAllDrives).
    """
    service = service or get_drive_service()
    query_parts = [f"'{_escape_drive_query_value(folder_id)}' in parents", "trashed = false"]
    if name_contains:
        query_parts.append(f"name contains '{_escape_drive_query_value(name_contains)}'")
    if mime_type:
        query_parts.append(f"mimeType = '{_escape_drive_query_value(mime_type)}'")

    try:
        response = (
            service.files()
            .list(
                q=" and ".join(query_parts),
                fields="files(id,name,webViewLink,createdTime,modifiedTime,size)",
                orderBy="createdTime desc",
                pageSize=max(1, min(int(limit), 100)),
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
    except Exception as exc:
        _raise_drive_error(exc)

    return response.get("files", [])


def download_drive_file(file_id: str, destination_path: str | Path, *, service=None) -> Path:
    service = service or get_drive_service()
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        with destination.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
    except Exception as exc:
        _raise_drive_error(exc)

    return destination


def upload_xlsx_to_drive(
    local_path: str | Path,
    *,
    folder_id: str,
    file_name: str,
    report_type: str | None = None,
    service=None,
) -> dict:
    """Uploads an XLSX file to Drive via a resumable upload.

    All XLSX uploads use resumable=True regardless of size: small files work
    fine over resumable too, and a single upload mode avoids a size-branch
    that would otherwise need its own tests and its own failure mode. Large
    non-resumable ("simple"/"multipart") uploads were observed in production
    to fail with a 502 from the Drive API on a single long-lived request;
    resumable uploads send bounded chunks instead, so no single request has to
    carry the whole payload end-to-end.
    """
    service = service or get_drive_service()
    path = Path(local_path)
    file_size_bytes = path.stat().st_size
    metadata = {
        "name": file_name,
        "parents": [folder_id],
    }
    media = MediaFileUpload(str(path), mimetype=DRIVE_XLSX_MIME_TYPE, resumable=True)
    request = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    )

    logging.info(
        "ICE_REPORT_DRIVE_UPLOAD operation=upload_xlsx report_type=%s "
        "file_size_bytes=%d resumable=true status=start",
        report_type or "",
        file_size_bytes,
    )

    try:
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=DRIVE_UPLOAD_NUM_RETRIES)
    except Exception as exc:
        logging.error(
            "ICE_REPORT_DRIVE_UPLOAD operation=upload_xlsx report_type=%s "
            "file_size_bytes=%d resumable=true status=failure http_status=%s",
            report_type or "",
            file_size_bytes,
            _sanitized_http_status(exc),
        )
        _raise_drive_error(exc)
        raise

    logging.info(
        "ICE_REPORT_DRIVE_UPLOAD operation=upload_xlsx report_type=%s "
        "file_size_bytes=%d resumable=true status=success",
        report_type or "",
        file_size_bytes,
    )
    return response
