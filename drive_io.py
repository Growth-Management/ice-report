from __future__ import annotations

from pathlib import Path

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


DRIVE_XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def get_drive_service():
    credentials, _ = google.auth.default(scopes=[DRIVE_SCOPE])
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
