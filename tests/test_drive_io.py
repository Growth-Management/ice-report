import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

import drive_io


class _FakeHttpResp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = ""


def _fake_http_error(status: int) -> HttpError:
    return HttpError(_FakeHttpResp(status), b'{"error": {"message": "boom"}}')


class _FakeResumableRequest:
    """Stands in for the HttpRequest object service.files().create(...)
    returns when media_body is a resumable MediaFileUpload -- next_chunk()
    is called repeatedly until it returns a non-None response, mirroring the
    real googleapiclient resumable upload protocol."""

    def __init__(
        self,
        chunk_count: int,
        final_response: dict,
        *,
        fail_on_chunk: int | None = None,
        fail_with: Exception | None = None,
    ):
        self.chunk_count = chunk_count
        self.final_response = final_response
        self.fail_on_chunk = fail_on_chunk
        self.fail_with = fail_with
        self.calls = 0
        self.num_retries_seen: list[int] = []

    def next_chunk(self, num_retries: int = 0):
        self.calls += 1
        self.num_retries_seen.append(num_retries)
        if self.fail_on_chunk is not None and self.calls == self.fail_on_chunk:
            raise self.fail_with if self.fail_with is not None else _fake_http_error(503)
        if self.calls < self.chunk_count:
            return (mock.Mock(), None)
        return (mock.Mock(), self.final_response)


class _FakeFilesResource:
    def __init__(self, request: _FakeResumableRequest):
        self._request = request
        self.create_kwargs = None

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return self._request


class _FakeDriveService:
    def __init__(self, request: _FakeResumableRequest):
        self._files = _FakeFilesResource(request)

    def files(self):
        return self._files


def _write_temp_xlsx(size_bytes: int) -> Path:
    fd, name = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    path = Path(name)
    path.write_bytes(b"x" * size_bytes)
    return path


class DriveIoTests(unittest.TestCase):
    def test_default_auth_mode_uses_adc_credentials(self):
        credentials = object()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            drive_io.google.auth,
            "default",
            return_value=(credentials, "ice-sh"),
        ) as default, mock.patch.object(drive_io, "build", return_value="service") as build:
            service = drive_io.get_drive_service()

        self.assertEqual(service, "service")
        default.assert_called_once_with(scopes=[drive_io.DRIVE_SCOPE])
        build.assert_called_once_with("drive", "v3", credentials=credentials, cache_discovery=False)

    def test_oauth_auth_mode_uses_user_refresh_token_credentials(self):
        env = {
            "DRIVE_AUTH_MODE": "oauth",
            "DRIVE_OAUTH_CLIENT_ID": "client-id",
            "DRIVE_OAUTH_CLIENT_SECRET": "client-secret",
            "DRIVE_OAUTH_REFRESH_TOKEN": "refresh-token",
        }
        captured = {}

        def _build(*args, **kwargs):
            captured["args"] = args
            captured["credentials"] = kwargs["credentials"]
            captured["cache_discovery"] = kwargs["cache_discovery"]
            return "service"

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(drive_io, "build", side_effect=_build):
            service = drive_io.get_drive_service()

        self.assertEqual(service, "service")
        self.assertEqual(captured["args"], ("drive", "v3"))
        self.assertFalse(captured["cache_discovery"])
        credentials = captured["credentials"]
        self.assertEqual(credentials.client_id, "client-id")
        self.assertEqual(credentials.client_secret, "client-secret")
        self.assertEqual(credentials.refresh_token, "refresh-token")
        self.assertEqual(credentials.scopes, [drive_io.DRIVE_SCOPE])

    def test_oauth_auth_mode_requires_complete_config(self):
        env = {
            "DRIVE_AUTH_MODE": "oauth",
            "DRIVE_OAUTH_CLIENT_ID": "client-id",
            "DRIVE_OAUTH_CLIENT_SECRET": "client-secret",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "drive_oauth_config_missing"):
                drive_io.get_drive_service()

    def test_secret_name_config_reads_secret_manager(self):
        env = {
            "DRIVE_AUTH_MODE": "oauth",
            "DRIVE_OAUTH_CLIENT_ID_SECRET_NAME": "drive-oauth-client-id",
            "DRIVE_OAUTH_CLIENT_SECRET_SECRET_NAME": "drive-oauth-client-secret",
            "DRIVE_OAUTH_REFRESH_TOKEN_SECRET_NAME": "drive-oauth-refresh-token",
        }
        values = {
            "drive-oauth-client-id": "client-id",
            "drive-oauth-client-secret": "client-secret",
            "drive-oauth-refresh-token": "refresh-token",
        }

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            drive_io,
            "_access_secret",
            side_effect=lambda secret_name: values[secret_name],
        ):
            credentials = drive_io._drive_credentials()

        self.assertEqual(credentials.client_id, "client-id")
        self.assertEqual(credentials.client_secret, "client-secret")
        self.assertEqual(credentials.refresh_token, "refresh-token")

    def test_refresh_error_is_sanitized(self):
        with self.assertRaises(drive_io.DriveOperationError) as ctx:
            drive_io._raise_drive_error(RefreshError("invalid token"))

        self.assertEqual(ctx.exception.code, "drive_oauth_refresh_failed")
        self.assertEqual(ctx.exception.status_code, 500)


class _FakeMediaFileUpload:
    """Stand-in for googleapiclient.http.MediaFileUpload. Some sibling test
    modules (test_admin_iap_auth.py) install a bare `object` stub for
    googleapiclient.http.MediaFileUpload via sys.modules.setdefault(...) at
    *import* time; depending on pytest's collection order that stub -- not the
    real class -- can already be bound as drive_io.MediaFileUpload by the time
    this file's tests run. These tests fake out the whole Drive service
    anyway, so they don't need the real MediaFileUpload's file-reading
    behavior -- just something constructible to pass through as media_body."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class UploadXlsxToDriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = _write_temp_xlsx(1024)
        self.addCleanup(self.tmp_path.unlink, missing_ok=True)
        patcher = mock.patch.object(drive_io, "MediaFileUpload", _FakeMediaFileUpload)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_media_file_upload_is_resumable(self):
        request = _FakeResumableRequest(chunk_count=1, final_response={"id": "f1", "name": "n.xlsx"})
        service = _FakeDriveService(request)

        captured = {}

        class _CapturingMediaFileUpload:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

        with mock.patch.object(drive_io, "MediaFileUpload", _CapturingMediaFileUpload):
            drive_io.upload_xlsx_to_drive(
                self.tmp_path, folder_id="folder-1", file_name="n.xlsx", service=service
            )

        self.assertTrue(captured["kwargs"]["resumable"])
        self.assertEqual(captured["kwargs"]["mimetype"], drive_io.DRIVE_XLSX_MIME_TYPE)
        self.assertEqual(captured["kwargs"]["chunksize"], drive_io.DRIVE_UPLOAD_CHUNK_SIZE)

    def test_resumable_upload_progresses_through_multiple_chunks_to_completion(self):
        request = _FakeResumableRequest(chunk_count=3, final_response={"id": "f1", "name": "n.xlsx"})
        service = _FakeDriveService(request)

        result = drive_io.upload_xlsx_to_drive(
            self.tmp_path, folder_id="folder-1", file_name="n.xlsx", service=service
        )

        self.assertEqual(request.calls, 3)
        self.assertEqual(result, {"id": "f1", "name": "n.xlsx"})

    def test_success_returns_file_id_and_name(self):
        request = _FakeResumableRequest(
            chunk_count=1, final_response={"id": "abc123", "name": "report.xlsx", "webViewLink": "https://x"}
        )
        service = _FakeDriveService(request)

        result = drive_io.upload_xlsx_to_drive(
            self.tmp_path, folder_id="folder-1", file_name="report.xlsx", service=service
        )

        self.assertEqual(result["id"], "abc123")
        self.assertEqual(result["name"], "report.xlsx")
        self.assertEqual(result["webViewLink"], "https://x")

    def test_drive_error_is_converted_to_drive_operation_error(self):
        request = _FakeResumableRequest(chunk_count=3, final_response={"id": "f1"}, fail_on_chunk=2)
        service = _FakeDriveService(request)

        with self.assertRaises(drive_io.DriveOperationError) as ctx:
            drive_io.upload_xlsx_to_drive(
                self.tmp_path, folder_id="folder-1", file_name="n.xlsx", service=service
            )

        self.assertEqual(ctx.exception.code, "drive_api_error")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_small_file_uploads_successfully(self):
        small_path = _write_temp_xlsx(512)
        self.addCleanup(small_path.unlink, missing_ok=True)
        request = _FakeResumableRequest(chunk_count=1, final_response={"id": "web-file", "name": "web.xlsx"})
        service = _FakeDriveService(request)

        result = drive_io.upload_xlsx_to_drive(
            small_path, folder_id="folder-1", file_name="web.xlsx", service=service
        )

        self.assertEqual(result["id"], "web-file")

    def test_large_file_uploads_successfully_across_many_chunks(self):
        large_path = _write_temp_xlsx(2048)
        self.addCleanup(large_path.unlink, missing_ok=True)
        request = _FakeResumableRequest(chunk_count=12, final_response={"id": "big-file", "name": "big.xlsx"})
        service = _FakeDriveService(request)

        result = drive_io.upload_xlsx_to_drive(
            large_path, folder_id="folder-1", file_name="big.xlsx", service=service
        )

        self.assertEqual(request.calls, 12)
        self.assertEqual(result["id"], "big-file")

    def test_shared_drive_options_are_preserved(self):
        request = _FakeResumableRequest(chunk_count=1, final_response={"id": "f1", "name": "n.xlsx"})
        service = _FakeDriveService(request)

        drive_io.upload_xlsx_to_drive(
            self.tmp_path, folder_id="shared-drive-folder", file_name="n.xlsx", service=service
        )

        create_kwargs = service._files.create_kwargs
        self.assertTrue(create_kwargs["supportsAllDrives"])
        self.assertEqual(create_kwargs["body"]["parents"], ["shared-drive-folder"])
        self.assertEqual(create_kwargs["fields"], "id,name,webViewLink")

    def test_num_retries_is_passed_to_next_chunk_without_a_second_retry_wrapper(self):
        request = _FakeResumableRequest(chunk_count=2, final_response={"id": "f1"})
        service = _FakeDriveService(request)

        drive_io.upload_xlsx_to_drive(
            self.tmp_path, folder_id="folder-1", file_name="n.xlsx", service=service
        )

        self.assertEqual(request.num_retries_seen, [drive_io.DRIVE_UPLOAD_NUM_RETRIES] * 2)

    def test_uses_configured_chunk_size(self):
        request = _FakeResumableRequest(chunk_count=1, final_response={"id": "f1"})
        service = _FakeDriveService(request)
        captured = {}

        class _CapturingMediaFileUpload:
            def __init__(self, *args, **kwargs):
                captured["kwargs"] = kwargs

        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": "1048576"}, clear=True):
            with mock.patch.object(drive_io, "MediaFileUpload", _CapturingMediaFileUpload):
                drive_io.upload_xlsx_to_drive(
                    self.tmp_path, folder_id="folder-1", file_name="n.xlsx", service=service
                )

        self.assertEqual(captured["kwargs"]["chunksize"], 1024 * 1024)

    def test_exception_diagnostics_are_logged_without_message_or_secrets(self):
        secret_message = "boom access_token=SECRET_VALUE refresh_token=OTHER_SECRET"
        request = _FakeResumableRequest(
            chunk_count=2,
            final_response={"id": "f1"},
            fail_on_chunk=1,
            fail_with=ConnectionResetError(secret_message),
        )
        service = _FakeDriveService(request)

        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(ConnectionResetError):
                drive_io.upload_xlsx_to_drive(
                    self.tmp_path, folder_id="folder-1", file_name="n.xlsx", service=service
                )

        joined = "\n".join(logs.output)
        self.assertIn("exception_module=builtins", joined)
        self.assertIn("exception_type=ConnectionResetError", joined)
        self.assertIn("chunk_index=1", joined)
        self.assertNotIn("SECRET_VALUE", joined)
        self.assertNotIn("OTHER_SECRET", joined)
        self.assertNotIn(secret_message, joined)

    def test_chunk_progress_is_logged_across_multiple_chunks(self):
        request = _FakeResumableRequest(chunk_count=3, final_response={"id": "f1"})
        service = _FakeDriveService(request)

        with self.assertLogs(level="INFO") as logs:
            drive_io.upload_xlsx_to_drive(
                self.tmp_path, folder_id="folder-1", file_name="n.xlsx", service=service
            )

        chunk_logs = [line for line in logs.output if "operation=upload_chunk" in line]
        self.assertEqual(len(chunk_logs), 3)
        self.assertTrue(any("chunk_index=1" in line for line in chunk_logs))
        self.assertTrue(any("chunk_index=2" in line for line in chunk_logs))
        self.assertTrue(any("chunk_index=3" in line for line in chunk_logs))


class DriveUploadChunkSizeTests(unittest.TestCase):
    def test_default_is_4_mib_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(drive_io.drive_upload_chunk_size(), 4 * 1024 * 1024)

    def test_env_override_is_used_when_valid(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": "1048576"}, clear=True):
            self.assertEqual(drive_io.drive_upload_chunk_size(), 1024 * 1024)

    def test_accepts_any_positive_multiple_of_256_kib(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": str(768 * 1024)}, clear=True):
            self.assertEqual(drive_io.drive_upload_chunk_size(), 768 * 1024)

    def test_non_integer_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": "not-a-number"}, clear=True):
            self.assertEqual(drive_io.drive_upload_chunk_size(), drive_io.DRIVE_UPLOAD_CHUNK_SIZE)

    def test_zero_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": "0"}, clear=True):
            self.assertEqual(drive_io.drive_upload_chunk_size(), drive_io.DRIVE_UPLOAD_CHUNK_SIZE)

    def test_negative_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": "-1048576"}, clear=True):
            self.assertEqual(drive_io.drive_upload_chunk_size(), drive_io.DRIVE_UPLOAD_CHUNK_SIZE)

    def test_non_multiple_of_256_kib_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": "1000000"}, clear=True):
            self.assertEqual(drive_io.drive_upload_chunk_size(), drive_io.DRIVE_UPLOAD_CHUNK_SIZE)

    def test_invalid_value_logs_warning_without_leaking_the_raw_value(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_CHUNK_SIZE_BYTES": "not-a-number"}, clear=True):
            with self.assertLogs(level="WARNING") as logs:
                drive_io.drive_upload_chunk_size()

        joined = "\n".join(logs.output)
        self.assertIn("ICE_REPORT_DRIVE_UPLOAD_CHUNK_SIZE_INVALID", joined)
        self.assertNotIn("not-a-number", joined)


if __name__ == "__main__":
    unittest.main()
