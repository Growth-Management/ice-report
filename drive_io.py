from __future__ import annotations

import os
from pathlib import Path

import google.auth
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


DRIVE_XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


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


def download_drive_file(file_id: str, destination_path: str | Path, *, service=None) -> Path:
    service = service or get_drive_service()
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with destination.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return destination


def upload_xlsx_to_drive(
    local_path: str | Path,
    *,
    folder_id: str,
    file_name: str,
    service=None,
) -> dict:
    service = service or get_drive_service()
    path = Path(local_path)
    metadata = {
        "name": file_name,
        "parents": [folder_id],
    }
    media = MediaFileUpload(str(path), mimetype=DRIVE_XLSX_MIME_TYPE, resumable=False)
    return (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
