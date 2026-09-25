import os
import sys
import unittest
from unittest import mock

# See test_plus_point_sales_admin_ui.py for why this purge is needed: some
# sibling test modules replace sys.modules["flask"]/["app"] with incomplete
# stubs at import time and never restore them, which would otherwise make
# app.app.test_client() unavailable here depending on collection order.
_POLLUTABLE_MODULE_PREFIXES = ("flask", "app")
for _name in list(sys.modules):
    if any(_name == prefix or _name.startswith(prefix + ".") for prefix in _POLLUTABLE_MODULE_PREFIXES):
        del sys.modules[_name]

import app  # noqa: E402
import drive_io  # noqa: E402

_UNSET = object()


class _FakeResponse:
    def __init__(self, status_code, *, headers=None, json_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_body = json_body
        self.closed = False

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, *, post_result=None, put_result=None):
        self._post_result = post_result
        self._put_result = put_result
        self.post_calls = []
        self.put_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if isinstance(self._post_result, Exception):
            raise self._post_result
        return self._post_result

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        if isinstance(self._put_result, Exception):
            raise self._put_result
        return self._put_result


class ResumableSessionInitTests(unittest.TestCase):
    def test_success_with_location_header(self):
        fake = _FakeSession(post_result=_FakeResponse(200, headers={"Location": "https://secret-session-url.example/abc"}))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result, session_url = drive_io.diagnose_resumable_session_init(
                file_name="x.xlsx", file_size_bytes=100, folder_id="folder-1"
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["http_status"], 200)
        self.assertTrue(result["location_present"])
        self.assertEqual(session_url, "https://secret-session-url.example/abc")

    def test_502_is_recorded_as_failure(self):
        fake = _FakeSession(post_result=_FakeResponse(502))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result, session_url = drive_io.diagnose_resumable_session_init(
                file_name="x.xlsx", file_size_bytes=100, folder_id="folder-1"
            )
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["http_status"], 502)
        self.assertFalse(result["location_present"])
        self.assertIsNone(session_url)

    def test_transport_exception_is_sanitized(self):
        fake = _FakeSession(post_result=TimeoutError())
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result, session_url = drive_io.diagnose_resumable_session_init(
                file_name="x.xlsx", file_size_bytes=100, folder_id="folder-1"
            )
        self.assertEqual(result["status"], "failure")
        self.assertIsNone(result["http_status"])
        self.assertEqual(result["exception_module"], "builtins")
        self.assertEqual(result["exception_type"], "TimeoutError")
        self.assertIsNone(session_url)

    def test_missing_location_header_is_failure_even_on_200(self):
        fake = _FakeSession(post_result=_FakeResponse(200, headers={}))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result, session_url = drive_io.diagnose_resumable_session_init(
                file_name="x.xlsx", file_size_bytes=100, folder_id="folder-1"
            )
        self.assertEqual(result["status"], "failure")
        self.assertFalse(result["location_present"])
        self.assertIsNone(session_url)

    def test_session_url_and_secrets_never_logged(self):
        fake = _FakeSession(post_result=_FakeResponse(200, headers={"Location": "https://secret-session-url.example/abc?token=SECRET_VALUE"}))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            with self.assertLogs(level="INFO") as logs:
                drive_io.diagnose_resumable_session_init(
                    file_name="x.xlsx", file_size_bytes=100, folder_id="folder-1"
                )
        joined = "\n".join(logs.output)
        self.assertNotIn("secret-session-url.example", joined)
        self.assertNotIn("SECRET_VALUE", joined)
        self.assertIn("location_present=True", joined)

    def test_does_not_follow_redirects(self):
        fake = _FakeSession(post_result=_FakeResponse(200, headers={"Location": "https://secret-session-url.example/abc"}))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            drive_io.diagnose_resumable_session_init(
                file_name="x.xlsx", file_size_bytes=100, folder_id="folder-1"
            )
        self.assertEqual(len(fake.post_calls), 1)
        _, kwargs = fake.post_calls[0]
        self.assertIs(kwargs["allow_redirects"], False)


class ResumableFirstChunkTests(unittest.TestCase):
    def test_308_is_success(self):
        fake = _FakeSession(put_result=_FakeResponse(308))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result = drive_io.diagnose_resumable_first_chunk(
                session_url="https://secret-session-url.example/abc",
                chunk_bytes=b"x" * 1024,
                total_size_bytes=10_000_000,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["http_status"], 308)
        self.assertNotIn("_file_id", result)

    def test_502_is_failure(self):
        fake = _FakeSession(put_result=_FakeResponse(502))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result = drive_io.diagnose_resumable_first_chunk(
                session_url="https://secret-session-url.example/abc",
                chunk_bytes=b"x" * 1024,
                total_size_bytes=10_000_000,
            )
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["http_status"], 502)

    def test_transport_exception_is_sanitized(self):
        fake = _FakeSession(put_result=ConnectionResetError())
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result = drive_io.diagnose_resumable_first_chunk(
                session_url="https://secret-session-url.example/abc",
                chunk_bytes=b"x" * 1024,
                total_size_bytes=10_000_000,
            )
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["exception_type"], "ConnectionResetError")

    def test_complete_upload_in_one_put_returns_file_id_for_cleanup(self):
        fake = _FakeSession(put_result=_FakeResponse(200, json_body={"id": "diagfile1", "name": "x.xlsx"}))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            result = drive_io.diagnose_resumable_first_chunk(
                session_url="https://secret-session-url.example/abc",
                chunk_bytes=b"x" * 1024,
                total_size_bytes=1024,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["_file_id"], "diagfile1")

    def test_session_url_never_logged(self):
        fake = _FakeSession(put_result=_FakeResponse(308))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            with self.assertLogs(level="INFO") as logs:
                drive_io.diagnose_resumable_first_chunk(
                    session_url="https://secret-session-url.example/abc?upload_id=SECRET_TOKEN",
                    chunk_bytes=b"x" * 1024,
                    total_size_bytes=10_000_000,
                )
        joined = "\n".join(logs.output)
        self.assertNotIn("secret-session-url.example", joined)
        self.assertNotIn("SECRET_TOKEN", joined)

    def test_does_not_follow_redirects(self):
        fake = _FakeSession(put_result=_FakeResponse(308))
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            drive_io.diagnose_resumable_first_chunk(
                session_url="https://secret-session-url.example/abc",
                chunk_bytes=b"x" * 1024,
                total_size_bytes=10_000_000,
            )
        self.assertEqual(len(fake.put_calls), 1)
        _, kwargs = fake.put_calls[0]
        self.assertIs(kwargs["allow_redirects"], False)


class RunResumableUploadDiagnosticTests(unittest.TestCase):
    def test_skips_first_chunk_when_not_requested(self):
        with mock.patch.object(
            drive_io,
            "diagnose_resumable_session_init",
            return_value=({"status": "success", "http_status": 200, "elapsed_ms": 5, "location_present": True}, "https://x/y"),
        ) as init_mock, mock.patch.object(drive_io, "diagnose_resumable_first_chunk") as put_mock:
            result = drive_io.run_resumable_upload_diagnostic(
                file_size_bytes=1000, run_first_chunk=False, chunk_size_bytes=100, folder_id="folder-1"
            )
        init_mock.assert_called_once()
        put_mock.assert_not_called()
        self.assertIsNone(result["first_chunk"])

    def test_skips_first_chunk_when_init_failed(self):
        with mock.patch.object(
            drive_io,
            "diagnose_resumable_session_init",
            return_value=({"status": "failure", "http_status": 502, "elapsed_ms": 5, "location_present": False}, None),
        ), mock.patch.object(drive_io, "diagnose_resumable_first_chunk") as put_mock:
            result = drive_io.run_resumable_upload_diagnostic(
                file_size_bytes=1000, run_first_chunk=True, chunk_size_bytes=100, folder_id="folder-1"
            )
        put_mock.assert_not_called()
        self.assertIsNone(result["first_chunk"])

    def test_runs_first_chunk_and_strips_internal_file_id(self):
        with mock.patch.object(
            drive_io,
            "diagnose_resumable_session_init",
            return_value=({"status": "success", "http_status": 200, "elapsed_ms": 5, "location_present": True}, "https://x/y"),
        ), mock.patch.object(
            drive_io,
            "diagnose_resumable_first_chunk",
            return_value={"status": "success", "http_status": 308, "elapsed_ms": 5, "chunk_size_bytes": 100},
        ):
            result = drive_io.run_resumable_upload_diagnostic(
                file_size_bytes=1000, run_first_chunk=True, chunk_size_bytes=100, folder_id="folder-1"
            )
        self.assertEqual(result["first_chunk"]["status"], "success")
        self.assertNotIn("_file_id", result["first_chunk"])

    def test_completed_upload_triggers_trash_cleanup(self):
        with mock.patch.object(
            drive_io,
            "diagnose_resumable_session_init",
            return_value=({"status": "success", "http_status": 200, "elapsed_ms": 5, "location_present": True}, "https://x/y"),
        ), mock.patch.object(
            drive_io,
            "diagnose_resumable_first_chunk",
            return_value={"status": "success", "http_status": 200, "elapsed_ms": 5, "chunk_size_bytes": 100, "_file_id": "diagfile1"},
        ), mock.patch.object(drive_io, "get_drive_service") as get_service:
            fake_service = mock.Mock()
            get_service.return_value = fake_service
            drive_io.run_resumable_upload_diagnostic(
                file_size_bytes=100, run_first_chunk=True, chunk_size_bytes=1000, folder_id="folder-1"
            )
        fake_service.files.return_value.update.assert_called_once()
        _, kwargs = fake_service.files.return_value.update.call_args
        self.assertEqual(kwargs["fileId"], "diagfile1")
        self.assertEqual(kwargs["body"], {"trashed": True})


class DriveResumableDiagnosticEndpointTests(unittest.TestCase):
    ENV_KEYS = ("ADMIN_API_KEY", "ADMIN_AUTH_FAIL_CLOSED", "ADMIN_IAP_AUTH_ENABLED", "K_SERVICE")

    def setUp(self):
        self.saved_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self.client = app.app.test_client()

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_requires_admin_key(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret", "ADMIN_AUTH_FAIL_CLOSED": "1"}):
            resp = self.client.post("/admin/drive/resumable-diagnostic", json={})
        self.assertEqual(resp.status_code, 401)

    def test_success_returns_shaped_result_without_secrets(self):
        fake_result = {
            "init": {"status": "success", "http_status": 200, "elapsed_ms": 12, "location_present": True},
            "first_chunk": {"status": "success", "http_status": 308, "elapsed_ms": 34, "chunk_size_bytes": 1048576},
        }
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.run_resumable_upload_diagnostic", return_value=fake_result
        ) as run_diag:
            resp = self.client.post(
                "/admin/drive/resumable-diagnostic",
                json={"file_size_bytes": 10_143_332, "run_first_chunk": True},
                headers={"X-Admin-Key": "secret"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["init"]["status"], "success")
        self.assertEqual(body["first_chunk"]["status"], "success")
        self.assertNotIn("session_url", str(body))
        run_diag.assert_called_once()
        self.assertEqual(run_diag.call_args.kwargs["file_size_bytes"], 10_143_332)
        self.assertTrue(run_diag.call_args.kwargs["run_first_chunk"])

    def _post_with_run_first_chunk(self, run_diag, value=_UNSET):
        payload = {"file_size_bytes": 1000}
        if value is not _UNSET:
            payload["run_first_chunk"] = value
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.run_resumable_upload_diagnostic", return_value=run_diag
        ) as run_diag_mock:
            resp = self.client.post(
                "/admin/drive/resumable-diagnostic",
                json=payload,
                headers={"X-Admin-Key": "secret"},
            )
        return resp, run_diag_mock

    _FAKE_INIT_ONLY_RESULT = {"init": {"status": "success", "http_status": 200, "location_present": True}, "first_chunk": None}
    _FAKE_FULL_RESULT = {
        "init": {"status": "success", "http_status": 200, "location_present": True},
        "first_chunk": {"status": "success", "http_status": 308},
    }

    def test_run_first_chunk_unspecified_defaults_to_false(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(self._FAKE_INIT_ONLY_RESULT)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(run_diag_mock.call_args.kwargs["run_first_chunk"])

    def test_run_first_chunk_false_is_accepted(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(self._FAKE_INIT_ONLY_RESULT, value=False)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(run_diag_mock.call_args.kwargs["run_first_chunk"])

    def test_run_first_chunk_true_is_accepted(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(self._FAKE_FULL_RESULT, value=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(run_diag_mock.call_args.kwargs["run_first_chunk"])

    def test_run_first_chunk_string_false_is_rejected(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(None, value="false")
        self.assertEqual(resp.status_code, 400)
        run_diag_mock.assert_not_called()

    def test_run_first_chunk_string_true_is_rejected(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(None, value="true")
        self.assertEqual(resp.status_code, 400)
        run_diag_mock.assert_not_called()

    def test_run_first_chunk_int_1_is_rejected(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(None, value=1)
        self.assertEqual(resp.status_code, 400)
        run_diag_mock.assert_not_called()

    def test_run_first_chunk_int_0_is_rejected(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(None, value=0)
        self.assertEqual(resp.status_code, 400)
        run_diag_mock.assert_not_called()

    def test_run_first_chunk_null_is_rejected_distinctly_from_unspecified(self):
        resp, run_diag_mock = self._post_with_run_first_chunk(None, value=None)
        self.assertEqual(resp.status_code, 400)
        run_diag_mock.assert_not_called()

    def test_rejects_invalid_file_size(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}):
            resp = self.client.post(
                "/admin/drive/resumable-diagnostic",
                json={"file_size_bytes": -1},
                headers={"X-Admin-Key": "secret"},
            )
        self.assertEqual(resp.status_code, 400)

    def test_rejects_oversized_file_size(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}):
            resp = self.client.post(
                "/admin/drive/resumable-diagnostic",
                json={"file_size_bytes": 500 * 1024 * 1024},
                headers={"X-Admin-Key": "secret"},
            )
        self.assertEqual(resp.status_code, 400)

    def test_folder_id_is_not_accepted_from_request_body(self):
        """The endpoint must not let a caller redirect the diagnostic upload
        to an arbitrary folder -- folder_id always comes from server-side
        config (default_output_folder_id()), never from the request."""
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.run_resumable_upload_diagnostic",
            return_value={"init": {"status": "success"}, "first_chunk": None},
        ) as run_diag:
            self.client.post(
                "/admin/drive/resumable-diagnostic",
                json={"file_size_bytes": 1000, "folder_id": "attacker-controlled-folder"},
                headers={"X-Admin-Key": "secret"},
            )
        self.assertNotEqual(run_diag.call_args.kwargs["folder_id"], "attacker-controlled-folder")


if __name__ == "__main__":
    unittest.main()
