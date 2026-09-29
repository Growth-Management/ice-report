from __future__ import annotations

import logging
import os
import time
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

# Must be a multiple of 256 KiB (Drive API resumable upload requirement).
# 4 MiB keeps even a small report (e.g. WEB, ~0.5 MiB) uploading in a single
# chunk while still bounding how much of a large report (e.g. video-reward,
# ~9 MiB) any one HTTP request has to carry. Overridable per-environment via
# DRIVE_UPLOAD_CHUNK_SIZE_BYTES (see drive_upload_chunk_size()) without a new
# image -- useful for isolating whether a large-file upload failure is
# chunk-size-sensitive (e.g. a transport/timeout issue on one request) versus
# something else entirely.
DRIVE_UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
DRIVE_UPLOAD_CHUNK_SIZE_BYTES_ENV = "DRIVE_UPLOAD_CHUNK_SIZE_BYTES"
_DRIVE_UPLOAD_CHUNK_SIZE_UNIT = 256 * 1024  # Drive API resumable upload requirement


def drive_upload_chunk_size() -> int:
    """DRIVE_UPLOAD_CHUNK_SIZE_BYTES override, falling back to the
    DRIVE_UPLOAD_CHUNK_SIZE default on anything that isn't a positive
    multiple of 256 KiB (Drive's own resumable upload chunk-size
    requirement). Never raises: an operator typo in this env var should
    degrade to a known-good default, not take report generation down."""
    raw = os.environ.get(DRIVE_UPLOAD_CHUNK_SIZE_BYTES_ENV, "").strip()
    if not raw:
        return DRIVE_UPLOAD_CHUNK_SIZE

    try:
        value = int(raw)
    except ValueError:
        logging.warning(
            "ICE_REPORT_DRIVE_UPLOAD_CHUNK_SIZE_INVALID reason=not_an_integer falling_back_to=%d",
            DRIVE_UPLOAD_CHUNK_SIZE,
        )
        return DRIVE_UPLOAD_CHUNK_SIZE

    if value <= 0 or value % _DRIVE_UPLOAD_CHUNK_SIZE_UNIT != 0:
        logging.warning(
            "ICE_REPORT_DRIVE_UPLOAD_CHUNK_SIZE_INVALID reason=not_positive_multiple_of_256kib "
            "falling_back_to=%d",
            DRIVE_UPLOAD_CHUNK_SIZE,
        )
        return DRIVE_UPLOAD_CHUNK_SIZE

    return value


DRIVE_UPLOAD_TRANSPORT_ENV = "DRIVE_UPLOAD_TRANSPORT"
DRIVE_UPLOAD_TRANSPORT_GOOGLEAPICLIENT = "googleapiclient"
DRIVE_UPLOAD_TRANSPORT_AUTHORIZED_SESSION = "authorized_session"
_DRIVE_UPLOAD_TRANSPORTS = (DRIVE_UPLOAD_TRANSPORT_GOOGLEAPICLIENT, DRIVE_UPLOAD_TRANSPORT_AUTHORIZED_SESSION)


def drive_upload_transport() -> str:
    """DRIVE_UPLOAD_TRANSPORT override: "googleapiclient" (default, the
    long-established MediaFileUpload/next_chunk() path) or
    "authorized_session" (a from-scratch resumable-upload implementation on
    google-auth's AuthorizedSession, added after Production video-reward
    uploads failed 3 times on googleapiclient's resumable path -- see
    docs/jumpplus-ad-revenue-report.md, "Drive upload transport
    failures" -- while a low-level AuthorizedSession probe of the same
    Production environment succeeded immediately). Kept switchable by env
    rather than replacing the default outright: if the new path turns out
    to have its own problems, reverting to "googleapiclient" (or just
    unsetting the var) needs no image rollback. An invalid value falls back
    to the default with a warning, matching drive_upload_chunk_size()."""
    raw = os.environ.get(DRIVE_UPLOAD_TRANSPORT_ENV, "").strip()
    if not raw:
        return DRIVE_UPLOAD_TRANSPORT_GOOGLEAPICLIENT
    if raw not in _DRIVE_UPLOAD_TRANSPORTS:
        logging.warning(
            "ICE_REPORT_DRIVE_UPLOAD_TRANSPORT_INVALID falling_back_to=%s",
            DRIVE_UPLOAD_TRANSPORT_GOOGLEAPICLIENT,
        )
        return DRIVE_UPLOAD_TRANSPORT_GOOGLEAPICLIENT
    return raw


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


def _sanitized_exception_fields(exc: Exception) -> tuple[str, str, object]:
    """(module, type name, errno) for logging only -- never str(exc)/repr(exc):
    both can echo request/response details (URLs, headers, an OAuth error
    body). This is what lets a non-HttpError upload failure (transport
    errors like a timeout or reset connection have no HTTP status at all)
    be told apart from an auth/logic/config failure without ever risking a
    credential or request body reaching the logs."""
    return type(exc).__module__, type(exc).__name__, getattr(exc, "errno", None)


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


def trash_drive_file(file_id: str, *, service=None) -> None:
    """Moves a file this app uploaded to the Drive trash (recoverable, not a
    hard delete). Used only to clean up a multi-file report run's own
    earlier uploads when a later file in the same run fails, so a partial
    set is never left in the official output folder."""
    service = service or get_drive_service()
    try:
        service.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
    except Exception as exc:
        _raise_drive_error(exc)


def upload_xlsx_to_drive(
    local_path: str | Path,
    *,
    folder_id: str,
    file_name: str,
    report_type: str | None = None,
    service=None,
) -> dict:
    """Uploads an XLSX file to Drive via a resumable upload.

    Dispatches on drive_upload_transport(): "googleapiclient" (default) uses
    the long-established MediaFileUpload/next_chunk() path unchanged;
    "authorized_session" uses a from-scratch resumable-upload implementation
    on google-auth's AuthorizedSession (see _upload_xlsx_authorized_session).
    `service` is only meaningful for the googleapiclient path -- the
    authorized_session path builds its own session from _drive_credentials()
    and ignores it, since it never uses a googleapiclient service object at
    all.
    """
    if drive_upload_transport() == DRIVE_UPLOAD_TRANSPORT_AUTHORIZED_SESSION:
        return _upload_xlsx_authorized_session(
            local_path, folder_id=folder_id, file_name=file_name, report_type=report_type
        )
    return _upload_xlsx_googleapiclient(
        local_path, folder_id=folder_id, file_name=file_name, report_type=report_type, service=service
    )


def _upload_xlsx_googleapiclient(
    local_path: str | Path,
    *,
    folder_id: str,
    file_name: str,
    report_type: str | None = None,
    service=None,
) -> dict:
    """The original resumable upload path, unchanged since before
    DRIVE_UPLOAD_TRANSPORT existed (still the default transport).

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
    chunk_size = drive_upload_chunk_size()
    metadata = {
        "name": file_name,
        "parents": [folder_id],
    }
    media = MediaFileUpload(
        str(path),
        mimetype=DRIVE_XLSX_MIME_TYPE,
        resumable=True,
        chunksize=chunk_size,
    )
    request = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    )

    logging.info(
        "ICE_REPORT_DRIVE_UPLOAD operation=upload_xlsx report_type=%s "
        "file_size_bytes=%d resumable=true chunk_size_bytes=%d status=start",
        report_type or "",
        file_size_bytes,
        chunk_size,
    )

    response = None
    chunk_index = 0
    while response is None:
        chunk_index += 1
        chunk_started_at = time.monotonic()
        try:
            status, response = request.next_chunk(num_retries=DRIVE_UPLOAD_NUM_RETRIES)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - chunk_started_at) * 1000)
            module, type_name, errno = _sanitized_exception_fields(exc)
            logging.error(
                "ICE_REPORT_DRIVE_UPLOAD operation=upload_chunk report_type=%s "
                "chunk_index=%d elapsed_ms=%d status=failure "
                "exception_module=%s exception_type=%s errno=%s",
                report_type or "",
                chunk_index,
                elapsed_ms,
                module,
                type_name,
                errno,
            )
            logging.error(
                "ICE_REPORT_DRIVE_UPLOAD operation=upload_xlsx report_type=%s "
                "file_size_bytes=%d resumable=true chunk_size_bytes=%d status=failure "
                "http_status=%s exception_module=%s exception_type=%s errno=%s",
                report_type or "",
                file_size_bytes,
                chunk_size,
                _sanitized_http_status(exc),
                module,
                type_name,
                errno,
            )
            _raise_drive_error(exc)
            raise

        elapsed_ms = int((time.monotonic() - chunk_started_at) * 1000)
        progress_percent = None
        if status is not None:
            try:
                progress_percent = round(status.progress() * 100, 1)
            except Exception:
                progress_percent = None
        logging.info(
            "ICE_REPORT_DRIVE_UPLOAD operation=upload_chunk report_type=%s "
            "chunk_index=%d progress_percent=%s elapsed_ms=%d status=success",
            report_type or "",
            chunk_index,
            progress_percent,
            elapsed_ms,
        )

    logging.info(
        "ICE_REPORT_DRIVE_UPLOAD operation=upload_xlsx report_type=%s "
        "file_size_bytes=%d resumable=true chunk_size_bytes=%d status=success",
        report_type or "",
        file_size_bytes,
        chunk_size,
    )
    return response


# ---------------------------------------------------------------------------
# Resumable upload diagnostics -- NOT part of the normal report-generation
# upload path (upload_xlsx_to_drive above, unchanged). Production video-reward
# uploads have failed twice at 4 MiB and once at 1 MiB chunk size, always on
# the very first chunk, always preceded by a googleapiclient-logged 502 retry,
# with the final exception being TimeoutError or BrokenPipeError -- i.e. the
# chunk-size change ruled out a payload-size cause, but next_chunk() conflates
# two distinct HTTP legs (the resumable session's initiating POST, and the
# first data PUT) into one call, so it can't say which leg is actually
# failing. These functions issue each leg directly via google-auth's
# AuthorizedSession (bypassing googleapiclient/httplib2's own resumable-upload
# code entirely) so a Production admin can tell apart a session-creation-side
# failure, a data-PUT-side failure, and (if this succeeds where the real
# upload path doesn't) a problem specific to googleapiclient/httplib2's own
# transport. Never logs a session URL, token, or response body -- only
# HTTP status, elapsed time, and (on failure) the sanitized exception fields
# already used by upload_xlsx_to_drive.
# ---------------------------------------------------------------------------

DRIVE_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/drive/v3/files"
_DIAGNOSTIC_INIT_TIMEOUT_S = 30
_DIAGNOSTIC_PUT_TIMEOUT_S = 120


def _new_authorized_session(credentials):
    """Constructs a fresh AuthorizedSession for the given credentials.
    Factored out (rather than inlining `from google.auth.transport.requests
    import AuthorizedSession` at each call site) so tests can mock this one
    attribute on the drive_io module directly instead of patching the
    dotted `google.auth.transport.requests.AuthorizedSession` path, which
    depends on that module's live sys.modules entry -- fragile in this
    codebase's test suite, where some sibling test files replace
    sys.modules["google.auth.transport.requests"] with an incomplete stub
    at import time and never restore it."""
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(credentials)


def _authorized_session():
    return _new_authorized_session(_drive_credentials())


def diagnose_resumable_session_init(
    *,
    file_name: str,
    file_size_bytes: int,
    folder_id: str,
    mime_type: str = DRIVE_XLSX_MIME_TYPE,
) -> tuple[dict, str | None]:
    """Phase A: issues only the resumable-upload session-initiation POST (no
    file bytes). Returns (sanitized result dict, session URL) -- the caller
    may pass the URL straight to diagnose_resumable_first_chunk() in the same
    request; it must never be logged, persisted, or returned to an API
    caller."""
    session = _authorized_session()
    started = time.monotonic()
    try:
        response = session.post(
            DRIVE_UPLOAD_ENDPOINT,
            params={
                "uploadType": "resumable",
                "supportsAllDrives": "true",
                "fields": "id,name,webViewLink",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(file_size_bytes),
            },
            json={"name": file_name, "parents": [folder_id]},
            timeout=_DIAGNOSTIC_INIT_TIMEOUT_S,
            allow_redirects=False,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        module, type_name, errno = _sanitized_exception_fields(exc)
        logging.error(
            "ICE_REPORT_DRIVE_DIAGNOSTIC operation=drive_resumable_init "
            "file_size_bytes=%d elapsed_ms=%d status=failure "
            "exception_module=%s exception_type=%s errno=%s",
            file_size_bytes,
            elapsed_ms,
            module,
            type_name,
            errno,
        )
        return (
            {
                "status": "failure",
                "http_status": None,
                "elapsed_ms": elapsed_ms,
                "location_present": False,
                "exception_module": module,
                "exception_type": type_name,
                "errno": errno,
            },
            None,
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    location = response.headers.get("Location")
    location_present = bool(location)
    success = 200 <= response.status_code < 300 and location_present
    response.close()
    logging.info(
        "ICE_REPORT_DRIVE_DIAGNOSTIC operation=drive_resumable_init "
        "file_size_bytes=%d elapsed_ms=%d http_status=%d location_present=%s status=%s",
        file_size_bytes,
        elapsed_ms,
        response.status_code,
        location_present,
        "success" if success else "failure",
    )
    return (
        {
            "status": "success" if success else "failure",
            "http_status": response.status_code,
            "elapsed_ms": elapsed_ms,
            "location_present": location_present,
        },
        location if success else None,
    )


def diagnose_resumable_first_chunk(
    *,
    session_url: str,
    chunk_bytes: bytes,
    total_size_bytes: int,
) -> dict:
    """Phase B: PUTs exactly one chunk (the caller decides its size) to the
    session URL from Phase A. A 308 means Drive accepted this chunk and is
    waiting for more (the normal, expected outcome for a chunk smaller than
    the whole file); 200/201 means the whole upload completed in this one
    PUT. session_url is used only to make the request -- never logged."""
    session = _authorized_session()
    end = len(chunk_bytes) - 1
    started = time.monotonic()
    try:
        response = session.put(
            session_url,
            data=chunk_bytes,
            headers={
                "Content-Length": str(len(chunk_bytes)),
                "Content-Range": f"bytes 0-{end}/{total_size_bytes}",
            },
            timeout=_DIAGNOSTIC_PUT_TIMEOUT_S,
            allow_redirects=False,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        module, type_name, errno = _sanitized_exception_fields(exc)
        logging.error(
            "ICE_REPORT_DRIVE_DIAGNOSTIC operation=drive_resumable_first_put "
            "chunk_index=1 chunk_size_bytes=%d elapsed_ms=%d status=failure "
            "exception_module=%s exception_type=%s errno=%s",
            len(chunk_bytes),
            elapsed_ms,
            module,
            type_name,
            errno,
        )
        return {
            "status": "failure",
            "http_status": None,
            "elapsed_ms": elapsed_ms,
            "chunk_size_bytes": len(chunk_bytes),
            "exception_module": module,
            "exception_type": type_name,
            "errno": errno,
        }

    elapsed_ms = int((time.monotonic() - started) * 1000)
    success = response.status_code in (200, 201, 308)
    file_id = None
    if response.status_code in (200, 201):
        try:
            file_id = response.json().get("id")
        except Exception:
            file_id = None
    response.close()
    logging.info(
        "ICE_REPORT_DRIVE_DIAGNOSTIC operation=drive_resumable_first_put "
        "chunk_index=1 chunk_size_bytes=%d elapsed_ms=%d http_status=%d status=%s",
        len(chunk_bytes),
        elapsed_ms,
        response.status_code,
        "success" if success else "failure",
    )
    result = {
        "status": "success" if success else "failure",
        "http_status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "chunk_size_bytes": len(chunk_bytes),
    }
    if file_id:
        # internal only, for cleanup -- popped before returning to any API caller
        result["_file_id"] = file_id
    return result


def run_resumable_upload_diagnostic(
    *,
    file_size_bytes: int,
    run_first_chunk: bool,
    chunk_size_bytes: int,
    folder_id: str,
) -> dict:
    """Orchestrates Phase A (+ Phase B if requested) against deterministic
    filler bytes generated in-process -- never real report data, and never
    content supplied by the caller of the admin endpoint this backs. Cleans
    up any Drive file this diagnostic itself completed (only possible if
    file_size_bytes <= chunk_size_bytes, so the single PUT both starts and
    finishes the upload); a chunk smaller than the whole file leaves no
    visible Drive file behind at all (Drive does not create the file
    resource until the upload completes), so there is nothing to clean up
    in the normal case."""
    file_name = f"DIAGNOSTIC_resumable_upload_test_{int(time.time())}.xlsx"
    init_result, session_url = diagnose_resumable_session_init(
        file_name=file_name, file_size_bytes=file_size_bytes, folder_id=folder_id
    )
    result: dict = {"init": init_result, "first_chunk": None}

    if not run_first_chunk or session_url is None:
        return result

    chunk_size = min(chunk_size_bytes, file_size_bytes)
    chunk_bytes = b"D" * chunk_size  # deterministic filler, not real report content
    first_chunk_result = diagnose_resumable_first_chunk(
        session_url=session_url, chunk_bytes=chunk_bytes, total_size_bytes=file_size_bytes
    )
    file_id = first_chunk_result.pop("_file_id", None)
    result["first_chunk"] = first_chunk_result

    if file_id:
        try:
            service = get_drive_service()
            service.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
        except Exception:
            logging.warning("ICE_REPORT_DRIVE_DIAGNOSTIC operation=cleanup status=failed_to_trash")

    return result


# ---------------------------------------------------------------------------
# AuthorizedSession resumable uploader -- the production alternative to
# _upload_xlsx_googleapiclient(), selected via DRIVE_UPLOAD_TRANSPORT (see
# drive_upload_transport()). Uses the exact same low-level HTTP primitives
# as the diagnostics above (AuthorizedSession, allow_redirects=False), but
# is a full resumable-upload implementation rather than a one-shot probe:
# streams the file in bounded chunks (never loading it whole into memory),
# tracks the server-confirmed offset from each 308's Range header rather
# than assuming its own chunk size advanced the position, and on a
# transport error or unexpected status queries the session for what was
# actually received before ever resending anything.
# ---------------------------------------------------------------------------

_UPLOAD_MAX_RECOVER_ATTEMPTS = 3
_UPLOAD_MAX_SESSION_RESTARTS = 1
_UPLOAD_STATUS_QUERY_TIMEOUT_S = 30


def _parse_range_header(value: str | None) -> int | None:
    """Parses a resumable-upload "bytes=0-1048575" Range header (present on
    a 308 once Drive has received at least one byte) into the next byte
    offset to send. None if absent or unparseable -- callers must not guess
    in that case (a 308 with no Range header at all means nothing has been
    received yet, i.e. offset 0, which is not the same as "unparseable")."""
    if not value:
        return None
    try:
        _, range_part = value.split("=", 1)
        _, end = range_part.split("-", 1)
        return int(end) + 1
    except (ValueError, AttributeError):
        return None


def _open_resumable_session(
    session,
    *,
    file_name: str,
    file_size_bytes: int,
    folder_id: str,
    report_type: str | None,
    mime_type: str = DRIVE_XLSX_MIME_TYPE,
) -> str:
    """Session-initiation POST for the production upload path. Distinct
    from diagnose_resumable_session_init() (diagnostic-only, returns a dict
    shaped for the admin API instead of raising). Returns the session URL --
    held only in the caller's memory, never logged."""
    started = time.monotonic()
    try:
        response = session.post(
            DRIVE_UPLOAD_ENDPOINT,
            params={
                "uploadType": "resumable",
                "supportsAllDrives": "true",
                "fields": "id,name,webViewLink",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(file_size_bytes),
            },
            json={"name": file_name, "parents": [folder_id]},
            timeout=_DIAGNOSTIC_INIT_TIMEOUT_S,
            allow_redirects=False,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        module, type_name, errno = _sanitized_exception_fields(exc)
        logging.error(
            "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=session_init "
            "report_type=%s elapsed_ms=%d status=failure "
            "exception_module=%s exception_type=%s errno=%s",
            report_type or "",
            elapsed_ms,
            module,
            type_name,
            errno,
        )
        raise DriveOperationError("drive_api_error", status_code=502) from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    location = response.headers.get("Location")
    status_code = response.status_code
    response.close()

    if not (200 <= status_code < 300 and location):
        logging.error(
            "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=session_init "
            "report_type=%s elapsed_ms=%d http_status=%d location_present=%s status=failure",
            report_type or "",
            elapsed_ms,
            status_code,
            bool(location),
        )
        raise DriveOperationError("drive_api_error", status_code=status_code)

    logging.info(
        "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=session_init "
        "report_type=%s elapsed_ms=%d http_status=%d status=success",
        report_type or "",
        elapsed_ms,
        status_code,
    )
    return location


def _query_upload_status(session, session_url: str, total_size_bytes: int) -> tuple[str, object]:
    """Drive's documented way to ask a resumable session what it actually
    received, instead of blindly resending a chunk after a transport error
    or an unexpected status: an empty PUT with Content-Range: bytes */total.

    Returns (state, payload):
      "incomplete", next_offset (int)      -- resume sending from here
      "complete",   response JSON or None  -- the upload already finished;
                                               nothing left to send
      "expired",    None                   -- session no longer valid
      "unknown",    None                   -- query itself failed/ambiguous
    """
    try:
        response = session.put(
            session_url,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{total_size_bytes}"},
            timeout=_UPLOAD_STATUS_QUERY_TIMEOUT_S,
            allow_redirects=False,
        )
    except Exception:
        return "unknown", None

    status_code = response.status_code
    if status_code == 308:
        next_offset = _parse_range_header(response.headers.get("Range"))
        response.close()
        return "incomplete", (next_offset if next_offset is not None else 0)
    if status_code in (200, 201):
        try:
            result = response.json()
        except Exception:
            result = None
        response.close()
        return "complete", result
    if status_code == 404:
        response.close()
        return "expired", None
    response.close()
    return "unknown", None


def _recover_upload(
    session,
    session_url: str,
    file_size_bytes: int,
    session_restarts: int,
    *,
    file_name: str,
    folder_id: str,
    report_type: str | None,
) -> tuple[str, object, str, int]:
    """Called after a transport error or an unexpected chunk-PUT status.
    Never blindly resends the failed chunk -- always asks the session what
    it actually has first. Returns (outcome, payload, session_url,
    session_restarts):
      "resume",   next_offset (int)  -- continue sending from here
      "complete", response JSON      -- upload already finished
      "failed",   None               -- recovery exhausted or impossible;
                                         caller must raise
    A 404 (expired session) opens at most _UPLOAD_MAX_SESSION_RESTARTS new
    sessions from scratch (offset resets to 0) -- never unbounded."""
    state, payload = _query_upload_status(session, session_url, file_size_bytes)
    if state == "incomplete":
        return "resume", payload, session_url, session_restarts
    if state == "complete":
        return "complete", payload, session_url, session_restarts
    if state == "expired" and session_restarts < _UPLOAD_MAX_SESSION_RESTARTS:
        new_session_url = _open_resumable_session(
            session,
            file_name=file_name,
            file_size_bytes=file_size_bytes,
            folder_id=folder_id,
            report_type=report_type,
        )
        return "resume", 0, new_session_url, session_restarts + 1
    return "failed", None, session_url, session_restarts


def _upload_xlsx_authorized_session(
    local_path: str | Path,
    *,
    folder_id: str,
    file_name: str,
    report_type: str | None = None,
) -> dict:
    path = Path(local_path)
    file_size_bytes = path.stat().st_size
    chunk_size = drive_upload_chunk_size()
    session = _authorized_session()

    logging.info(
        "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_xlsx "
        "report_type=%s file_size_bytes=%d chunk_size_bytes=%d status=start",
        report_type or "",
        file_size_bytes,
        chunk_size,
    )

    session_url = _open_resumable_session(
        session,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        folder_id=folder_id,
        report_type=report_type,
    )

    offset = 0
    chunk_index = 0
    recover_attempts = 0
    session_restarts = 0

    with path.open("rb") as fh:
        while offset < file_size_bytes:
            chunk_index += 1
            fh.seek(offset)
            data = fh.read(chunk_size)
            end = offset + len(data) - 1
            chunk_started = time.monotonic()

            try:
                response = session.put(
                    session_url,
                    data=data,
                    headers={
                        "Content-Length": str(len(data)),
                        "Content-Range": f"bytes {offset}-{end}/{file_size_bytes}",
                    },
                    timeout=_DIAGNOSTIC_PUT_TIMEOUT_S,
                    allow_redirects=False,
                )
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - chunk_started) * 1000)
                module, type_name, errno = _sanitized_exception_fields(exc)
                logging.error(
                    "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_chunk "
                    "report_type=%s chunk_index=%d offset_start=%d offset_end=%d elapsed_ms=%d "
                    "status=failure exception_module=%s exception_type=%s errno=%s",
                    report_type or "",
                    chunk_index,
                    offset,
                    end,
                    elapsed_ms,
                    module,
                    type_name,
                    errno,
                )
                recover_attempts += 1
                if recover_attempts > _UPLOAD_MAX_RECOVER_ATTEMPTS:
                    raise DriveOperationError("drive_api_error", status_code=502) from exc
                outcome, payload, session_url, session_restarts = _recover_upload(
                    session,
                    session_url,
                    file_size_bytes,
                    session_restarts,
                    file_name=file_name,
                    folder_id=folder_id,
                    report_type=report_type,
                )
                if outcome == "resume":
                    offset = payload
                    continue
                if outcome == "complete":
                    logging.info(
                        "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_xlsx "
                        "report_type=%s file_size_bytes=%d chunk_count=%d status=success",
                        report_type or "",
                        file_size_bytes,
                        chunk_index,
                    )
                    return payload
                raise DriveOperationError("drive_api_error", status_code=502) from exc

            elapsed_ms = int((time.monotonic() - chunk_started) * 1000)
            status_code = response.status_code

            if status_code in (200, 201):
                result = response.json()
                response.close()
                logging.info(
                    "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_chunk "
                    "report_type=%s chunk_index=%d offset_start=%d offset_end=%d elapsed_ms=%d "
                    "http_status=%d status=success",
                    report_type or "",
                    chunk_index,
                    offset,
                    end,
                    elapsed_ms,
                    status_code,
                )
                logging.info(
                    "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_xlsx "
                    "report_type=%s file_size_bytes=%d chunk_count=%d status=success",
                    report_type or "",
                    file_size_bytes,
                    chunk_index,
                )
                return result

            if status_code == 308:
                range_header = response.headers.get("Range")
                response.close()
                next_offset = _parse_range_header(range_header)

                if next_offset is not None:
                    logging.info(
                        "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_chunk "
                        "report_type=%s chunk_index=%d offset_start=%d offset_end=%d elapsed_ms=%d "
                        "http_status=308 status=success",
                        report_type or "",
                        chunk_index,
                        offset,
                        end,
                        elapsed_ms,
                    )
                    offset = next_offset
                    recover_attempts = 0
                    continue

                # Drive's 308 doesn't by itself say how much of this chunk
                # actually landed when the Range header is missing or
                # unparseable -- assuming "all of it" (end + 1) risks
                # silently skipping bytes Drive never received and
                # completing a corrupt file. Never guess; ask the session
                # what it actually has (same bounded recovery as a
                # transport error/unexpected status below).
                logging.error(
                    "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_chunk "
                    "report_type=%s chunk_index=%d offset_start=%d offset_end=%d elapsed_ms=%d "
                    "http_status=308 status=failure reason=range_missing_or_unparseable",
                    report_type or "",
                    chunk_index,
                    offset,
                    end,
                    elapsed_ms,
                )
                recover_attempts += 1
                if recover_attempts > _UPLOAD_MAX_RECOVER_ATTEMPTS:
                    raise DriveOperationError("drive_api_error", status_code=308)
                outcome, payload, session_url, session_restarts = _recover_upload(
                    session,
                    session_url,
                    file_size_bytes,
                    session_restarts,
                    file_name=file_name,
                    folder_id=folder_id,
                    report_type=report_type,
                )
                if outcome == "resume":
                    offset = payload
                    continue
                if outcome == "complete":
                    logging.info(
                        "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_xlsx "
                        "report_type=%s file_size_bytes=%d chunk_count=%d status=success",
                        report_type or "",
                        file_size_bytes,
                        chunk_index,
                    )
                    return payload
                raise DriveOperationError("drive_api_error", status_code=308)

            response.close()
            logging.error(
                "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_chunk "
                "report_type=%s chunk_index=%d offset_start=%d offset_end=%d elapsed_ms=%d "
                "http_status=%d status=failure",
                report_type or "",
                chunk_index,
                offset,
                end,
                elapsed_ms,
                status_code,
            )
            recover_attempts += 1
            if recover_attempts > _UPLOAD_MAX_RECOVER_ATTEMPTS:
                raise DriveOperationError("drive_api_error", status_code=status_code)
            outcome, payload, session_url, session_restarts = _recover_upload(
                session,
                session_url,
                file_size_bytes,
                session_restarts,
                file_name=file_name,
                folder_id=folder_id,
                report_type=report_type,
            )
            if outcome == "resume":
                offset = payload
                continue
            if outcome == "complete":
                logging.info(
                    "ICE_REPORT_DRIVE_UPLOAD transport=authorized_session operation=upload_xlsx "
                    "report_type=%s file_size_bytes=%d chunk_count=%d status=success",
                    report_type or "",
                    file_size_bytes,
                    chunk_index,
                )
                return payload
            raise DriveOperationError("drive_api_error", status_code=status_code)

    # file_size_bytes == 0 edge case: no chunk loop ever ran. Treat the same
    # as any other never-started upload rather than silently returning
    # nothing.
    raise DriveOperationError("drive_api_error", status_code=400)


# ---------------------------------------------------------------------------
# Full resumable-upload diagnostic -- NOT part of either upload path above
# (_upload_xlsx_googleapiclient, _upload_xlsx_authorized_session both
# unchanged; never called from here). Production's AuthorizedSession
# uploader trial on the real video-reward file (~10.1 MB, 1 MiB chunks) got
# 4 consecutive real HTTP 502s, always at the exact same confirmed offset
# (2,097,152 bytes = 2 MiB), after its first two chunks evidently succeeded.
# That the post-502 status query kept reporting a confirmed offset (rather
# than the connection appearing entirely dead) means "the whole connection
# is just broken" isn't a given -- but it's still unclear whether HTTP
# connection/keep-alive reuse across many sequential requests to the same
# session URL is implicated at all, versus the Drive resumable session
# itself, versus the specific byte offset/chunk boundary, versus Drive API
# or Cloud Run egress. Rather than guess and change the production
# uploader, this runs a full resumable upload of deterministic filler
# bytes (never real report data) with an explicit, switchable variable:
# whether the same AuthorizedSession (and thus the same underlying
# requests/urllib3 connection pool) is reused for every request, or a
# fresh one is created (and closed) per request while the Drive resumable
# session URL itself stays identical throughout.
# ---------------------------------------------------------------------------

_FULL_DIAGNOSTIC_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
FULL_DIAGNOSTIC_CONNECTION_MODE_REUSE = "reuse"
FULL_DIAGNOSTIC_CONNECTION_MODE_FRESH_PER_REQUEST = "fresh_per_request"
_FULL_DIAGNOSTIC_CONNECTION_MODES = (
    FULL_DIAGNOSTIC_CONNECTION_MODE_REUSE,
    FULL_DIAGNOSTIC_CONNECTION_MODE_FRESH_PER_REQUEST,
)


def run_full_resumable_diagnostic(
    *,
    file_size_bytes: int,
    chunk_size_bytes: int,
    connection_mode: str,
    folder_id: str,
) -> dict:
    """Runs one full resumable upload of deterministic filler bytes to
    isolate what's behind the repeated real Production 502s (see module
    docstring above). Returns a dict shaped for the admin API -- never a
    session URL, token, header, or response body, only status codes,
    offsets, and elapsed times.

    connection_mode:
      "reuse"             -- one AuthorizedSession for session init, every
                              chunk PUT, and any status query (matches the
                              Production AuthorizedSession uploader's
                              current behavior exactly).
      "fresh_per_request"  -- a brand-new AuthorizedSession (closed
                              immediately after) for every single HTTP
                              request, while the Drive resumable session
                              URL itself is unchanged throughout.

    Both modes build their AuthorizedSession(s) from the exact same
    credentials object (fetched once, up front, via _drive_credentials())
    -- the only variable this diagnostic isolates is whether the HTTP
    session/connection pool is reused across requests, not how
    credentials happen to be obtained.

    Deliberately does not restart the resumable session on an expired/404
    response in this first diagnostic pass -- silently working around a
    stuck session would hide exactly the behavior this exists to observe.
    Bounded recovery (status-query-then-resume, never a blind resend)
    still applies, same bound as the production uploader. Once that bound
    is exhausted, a single terminal status query (never a retry, never a
    new resumable session, never additional recover budget) observes
    Drive's actual final state before reporting failure -- see
    _full_diagnostic_terminal_observation.
    """
    if connection_mode not in _FULL_DIAGNOSTIC_CONNECTION_MODES:
        raise ValueError(f"invalid connection_mode: {connection_mode!r}")

    credentials = _drive_credentials()
    reused_session = None
    requests_log: list[dict] = []
    request_index = 0
    recover_count = 0
    file_name = f"DIAGNOSTIC_full_resumable_test_{int(time.time())}.xlsx"

    def _session_for_request():
        nonlocal reused_session
        if connection_mode == FULL_DIAGNOSTIC_CONNECTION_MODE_REUSE:
            if reused_session is None:
                reused_session = _new_authorized_session(credentials)
            return reused_session, False
        return _new_authorized_session(credentials), True

    def _record(
        kind: str,
        *,
        offset_start=None,
        offset_end=None,
        http_status=None,
        elapsed_ms=None,
        exc=None,
        confirmed_offset=None,
    ) -> dict:
        nonlocal request_index
        request_index += 1
        entry: dict = {"kind": kind, "request_index": request_index}
        if offset_start is not None:
            entry["offset_start"] = offset_start
        if offset_end is not None:
            entry["offset_end"] = offset_end
        if http_status is not None:
            entry["http_status"] = http_status
        if elapsed_ms is not None:
            entry["elapsed_ms"] = elapsed_ms
        if confirmed_offset is not None:
            entry["confirmed_offset"] = confirmed_offset
        module = type_name = errno = None
        if exc is not None:
            module, type_name, errno = _sanitized_exception_fields(exc)
            entry["exception_module"] = module
            entry["exception_type"] = type_name
            entry["errno"] = errno
        requests_log.append(entry)
        logging.warning(
            "ICE_REPORT_DRIVE_FULL_DIAGNOSTIC connection_mode=%s kind=%s request_index=%d "
            "offset_start=%s offset_end=%s http_status=%s elapsed_ms=%s confirmed_offset=%s "
            "recover_count=%d exception_module=%s exception_type=%s errno=%s",
            connection_mode,
            kind,
            request_index,
            offset_start,
            offset_end,
            http_status,
            elapsed_ms,
            confirmed_offset,
            recover_count,
            module,
            type_name,
            errno,
        )
        return entry

    def _finalize(status: str, confirmed_offset: int, *, file_id: str | None = None, cleanup: str | None = None) -> dict:
        result = {
            "status": status,
            "connection_mode": connection_mode,
            "file_size_bytes": file_size_bytes,
            "chunk_size_bytes": chunk_size_bytes,
            "confirmed_offset": confirmed_offset,
            "requests": requests_log,
        }
        if cleanup is not None:
            result["cleanup"] = cleanup
        return result

    try:
        # -- session init --
        session, should_close = _session_for_request()
        started = time.monotonic()
        try:
            response = session.post(
                DRIVE_UPLOAD_ENDPOINT,
                params={
                    "uploadType": "resumable",
                    "supportsAllDrives": "true",
                    "fields": "id,name,webViewLink",
                },
                headers={
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": DRIVE_XLSX_MIME_TYPE,
                    "X-Upload-Content-Length": str(file_size_bytes),
                },
                json={"name": file_name, "parents": [folder_id]},
                timeout=_DIAGNOSTIC_INIT_TIMEOUT_S,
                allow_redirects=False,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _record("init", elapsed_ms=elapsed_ms, exc=exc)
            if should_close:
                session.close()
            return _finalize("failure", 0)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        session_url = response.headers.get("Location")
        status_code = response.status_code
        response.close()
        _record("init", http_status=status_code, elapsed_ms=elapsed_ms)
        if should_close:
            session.close()

        if not (200 <= status_code < 300 and session_url):
            return _finalize("failure", 0)

        offset = 0
        chunk_index = 0

        while offset < file_size_bytes:
            chunk_index += 1
            chunk_len = min(chunk_size_bytes, file_size_bytes - offset)
            end = offset + chunk_len - 1
            data = b"D" * chunk_len

            session, should_close = _session_for_request()
            started = time.monotonic()
            try:
                response = session.put(
                    session_url,
                    data=data,
                    headers={
                        "Content-Length": str(chunk_len),
                        "Content-Range": f"bytes {offset}-{end}/{file_size_bytes}",
                    },
                    timeout=_DIAGNOSTIC_PUT_TIMEOUT_S,
                    allow_redirects=False,
                )
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                _record("chunk", offset_start=offset, offset_end=end, elapsed_ms=elapsed_ms, exc=exc)
                if should_close:
                    session.close()
                recover_count += 1
                if recover_count > _UPLOAD_MAX_RECOVER_ATTEMPTS:
                    return _full_diagnostic_terminal_observation(
                        _session_for_request, _record, session_url, file_size_bytes, offset, _finalize
                    )
                outcome, payload = _full_diagnostic_query_status(
                    _session_for_request, _record, session_url, file_size_bytes
                )
                if outcome == "resume":
                    offset = payload
                    continue
                if outcome == "complete":
                    return _full_diagnostic_cleanup_and_finalize(_finalize, payload, file_size_bytes)
                return _finalize("failure", offset)

            elapsed_ms = int((time.monotonic() - started) * 1000)
            status_code = response.status_code

            if status_code in (200, 201):
                result = response.json()
                response.close()
                if should_close:
                    session.close()
                _record(
                    "chunk",
                    offset_start=offset,
                    offset_end=end,
                    http_status=status_code,
                    elapsed_ms=elapsed_ms,
                    confirmed_offset=file_size_bytes,
                )
                return _full_diagnostic_cleanup_and_finalize(_finalize, result, file_size_bytes)

            if status_code == 308:
                range_header = response.headers.get("Range")
                response.close()
                if should_close:
                    session.close()
                next_offset = _parse_range_header(range_header)
                _record(
                    "chunk",
                    offset_start=offset,
                    offset_end=end,
                    http_status=status_code,
                    elapsed_ms=elapsed_ms,
                    confirmed_offset=next_offset,
                )
                if next_offset is not None:
                    offset = next_offset
                    recover_count = 0
                    continue
                # Range missing/unparseable -- never guess end+1, query status.
                recover_count += 1
                if recover_count > _UPLOAD_MAX_RECOVER_ATTEMPTS:
                    return _full_diagnostic_terminal_observation(
                        _session_for_request, _record, session_url, file_size_bytes, offset, _finalize
                    )
                outcome, payload = _full_diagnostic_query_status(
                    _session_for_request, _record, session_url, file_size_bytes
                )
                if outcome == "resume":
                    offset = payload
                    continue
                if outcome == "complete":
                    return _full_diagnostic_cleanup_and_finalize(_finalize, payload, file_size_bytes)
                return _finalize("failure", offset)

            response.close()
            if should_close:
                session.close()
            _record("chunk", offset_start=offset, offset_end=end, http_status=status_code, elapsed_ms=elapsed_ms)
            recover_count += 1
            if recover_count > _UPLOAD_MAX_RECOVER_ATTEMPTS:
                return _full_diagnostic_terminal_observation(
                    _session_for_request, _record, session_url, file_size_bytes, offset, _finalize
                )
            outcome, payload = _full_diagnostic_query_status(_session_for_request, _record, session_url, file_size_bytes)
            if outcome == "resume":
                offset = payload
                continue
            if outcome == "complete":
                return _full_diagnostic_cleanup_and_finalize(_finalize, payload, file_size_bytes)
            return _finalize("failure", offset)

        return _finalize("failure", offset)
    finally:
        # Close the diagnostic's own reused HTTP session exactly once, at
        # the very end, regardless of how the diagnostic exits (success,
        # failure, or an uncaught error) -- never mid-run, so TEST 1's
        # connection-reuse condition (one session serving every request)
        # is not disturbed. fresh_per_request already closes its session
        # after each individual request.
        if connection_mode == FULL_DIAGNOSTIC_CONNECTION_MODE_REUSE and reused_session is not None:
            reused_session.close()


def _full_diagnostic_query_status(session_for_request, record, session_url: str, total_size_bytes: int):
    """Status query for run_full_resumable_diagnostic() -- an empty PUT
    with Content-Length: 0 and Content-Range: bytes */total, using
    whatever session the caller's connection_mode dictates (a fresh one is
    still "the same Drive resumable session", just a new HTTP connection).
    No session restart here (see run_full_resumable_diagnostic's
    docstring): "expired" is reported as an outcome, not silently
    recovered from."""
    session, should_close = session_for_request()
    started = time.monotonic()
    try:
        response = session.put(
            session_url,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{total_size_bytes}"},
            timeout=_UPLOAD_STATUS_QUERY_TIMEOUT_S,
            allow_redirects=False,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        record("status_query", elapsed_ms=elapsed_ms, exc=exc)
        if should_close:
            session.close()
        return "unknown", None

    elapsed_ms = int((time.monotonic() - started) * 1000)
    status_code = response.status_code

    if status_code == 308:
        next_offset = _parse_range_header(response.headers.get("Range"))
        response.close()
        if should_close:
            session.close()
        confirmed_offset = next_offset if next_offset is not None else 0
        record("status_query", http_status=status_code, elapsed_ms=elapsed_ms, confirmed_offset=confirmed_offset)
        return "resume", confirmed_offset
    if status_code in (200, 201):
        try:
            result = response.json()
        except Exception:
            result = None
        response.close()
        if should_close:
            session.close()
        record(
            "status_query", http_status=status_code, elapsed_ms=elapsed_ms, confirmed_offset=total_size_bytes
        )
        return "complete", result
    response.close()
    if should_close:
        session.close()
    record("status_query", http_status=status_code, elapsed_ms=elapsed_ms)
    return "expired" if status_code == 404 else "unknown", None


def _full_diagnostic_terminal_observation(
    session_for_request, record, session_url: str, total_size_bytes: int, current_offset: int, finalize
) -> dict:
    """Runs exactly one status query once run_full_resumable_diagnostic's
    bounded recover budget is exhausted, purely to observe Drive's actual
    final state -- never to resume the upload, never to restart the
    resumable session, never to spend additional recover budget. A
    client-side chunk failure (502, a dropped connection, an unparseable
    308) does not guarantee Drive never received those bytes, so the
    reported confirmed_offset must reflect Drive's own view whenever this
    query resolves it, rather than the last byte offset the client
    happened to attempt."""
    outcome, payload = _full_diagnostic_query_status(session_for_request, record, session_url, total_size_bytes)
    if outcome == "complete":
        return _full_diagnostic_cleanup_and_finalize(finalize, payload, total_size_bytes)
    if outcome == "resume":
        return finalize("failure", payload)
    return finalize("failure", current_offset)


def _full_diagnostic_cleanup_and_finalize(finalize, result: dict | None, confirmed_offset: int) -> dict:
    """The diagnostic completed an upload (deterministic filler content, not
    a real report) -- trash it immediately rather than leaving a stray
    file behind."""
    file_id = (result or {}).get("id")
    cleanup = "not_applicable"
    if file_id:
        try:
            service = get_drive_service()
            service.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
            cleanup = "trashed"
        except Exception:
            cleanup = "failed_to_trash"
    return finalize("success", confirmed_offset, file_id=file_id, cleanup=cleanup)
