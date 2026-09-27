import os
import sys
import unittest
from unittest import mock

# See test_plus_point_sales_admin_ui.py for why this purge is needed: some
# sibling test modules (test_admin_report_definitions.py, test_admin_iap_auth.py)
# replace sys.modules["flask"]/sys.modules["app"] with incomplete stubs at
# *module import time* and never restore them. We only need flask/app here --
# drive_io's own fresh-AuthorizedSession construction is patched via
# mock.patch.object(drive_io, "_new_authorized_session", ...) below, which is
# an attribute patch on the already-loaded module object and is therefore
# immune to any sys.modules pollution of google/googleapiclient left behind
# by other test files (purging those prefixes here would only force a later
# re-import that creates new HttpError/RefreshError class objects with a
# different identity than the ones drive_io.py already bound internally).
_POLLUTABLE_MODULE_PREFIXES = ("flask", "app")
for _name in list(sys.modules):
    if any(_name == prefix or _name.startswith(prefix + ".") for prefix in _POLLUTABLE_MODULE_PREFIXES):
        del sys.modules[_name]

import drive_io  # noqa: E402
import app  # noqa: E402


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
    """One fake session instance per "connection" -- tracks its own
    post()/put() calls and whether close() was invoked, so tests can
    confirm connection_mode="fresh_per_request" actually creates and
    closes a new instance per HTTP request."""

    _all_instances: list["_FakeSession"] = []

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False
        _FakeSession._all_instances.append(self)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def put(self, url, **kwargs):
        self.calls.append(("put", url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


_LOCATION = "https://secret-session-url.example/full-diagnostic-abc"


def _run_with_scripted_responses(responses, *, connection_mode="reuse", file_size_bytes=300, chunk_size_bytes=100):
    """Feeds `responses` out one at a time to whichever fake session
    services each HTTP request, regardless of connection_mode -- for
    "reuse" that's a single shared instance, for "fresh_per_request" a new
    one is constructed each time (this generator hands the same shared
    queue to every new instance)."""
    _FakeSession._all_instances = []
    remaining = list(responses)

    class _Shared(_FakeSession):
        def __init__(self):
            super().__init__([])
            self._responses = remaining  # shared, mutated in place

    with mock.patch.object(drive_io, "_drive_credentials", return_value=object()), mock.patch.object(
        drive_io, "_authorized_session", side_effect=lambda: _Shared()
    ), mock.patch.object(
        drive_io, "_new_authorized_session", side_effect=lambda creds: _Shared()
    ):
        return drive_io.run_full_resumable_diagnostic(
            file_size_bytes=file_size_bytes,
            chunk_size_bytes=chunk_size_bytes,
            connection_mode=connection_mode,
            folder_id="folder-1",
        )


class ConnectionModeTests(unittest.TestCase):
    def test_reuse_mode_uses_one_session_instance_for_every_request(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),  # init
            _FakeResponse(308, headers={"Range": "bytes=0-99"}),  # chunk 1
            _FakeResponse(308, headers={"Range": "bytes=100-199"}),  # chunk 2
            _FakeResponse(200, json_body={"id": "f1"}),  # chunk 3 (completes)
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            _run_with_scripted_responses(responses, connection_mode="reuse")
        # every HTTP request landed on the exact same session instance
        instances = _FakeSession._all_instances
        self.assertEqual(len(instances), 1)
        self.assertEqual(len(instances[0].calls), 4)

    def test_fresh_per_request_creates_and_closes_a_new_session_each_time(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),  # init
            _FakeResponse(308, headers={"Range": "bytes=0-99"}),  # chunk 1
            _FakeResponse(308, headers={"Range": "bytes=100-199"}),  # chunk 2
            _FakeResponse(200, json_body={"id": "f1"}),  # chunk 3 (completes)
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            _run_with_scripted_responses(responses, connection_mode="fresh_per_request")
        instances = _FakeSession._all_instances
        # one instance per HTTP request (init + 3 chunk PUTs = 4)
        self.assertEqual(len(instances), 4)
        for inst in instances:
            self.assertTrue(inst.closed, "each fresh_per_request session must be closed after its request")

    def test_reuse_mode_session_is_not_closed_between_requests(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(200, json_body={"id": "f1"}),
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            _run_with_scripted_responses(
                responses, connection_mode="reuse", file_size_bytes=50, chunk_size_bytes=1000
            )
        instances = _FakeSession._all_instances
        self.assertEqual(len(instances), 1)
        self.assertFalse(instances[0].closed)

    def test_resumable_session_url_is_identical_across_requests_in_both_modes(self):
        for mode in ("reuse", "fresh_per_request"):
            responses = [
                _FakeResponse(200, headers={"Location": _LOCATION}),
                _FakeResponse(200, json_body={"id": "f1"}),
            ]
            with mock.patch.object(drive_io, "get_drive_service") as get_service:
                get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
                _run_with_scripted_responses(
                    responses, connection_mode=mode, file_size_bytes=50, chunk_size_bytes=1000
                )
            urls = [call[1] for inst in _FakeSession._all_instances for call in inst.calls if call[0] == "put"]
            self.assertTrue(all(u == _LOCATION for u in urls), f"mode={mode}: {urls}")

    def test_invalid_connection_mode_raises(self):
        with self.assertRaises(ValueError):
            drive_io.run_full_resumable_diagnostic(
                file_size_bytes=10, chunk_size_bytes=10, connection_mode="bogus", folder_id="folder-1"
            )


class RangeHandlingTests(unittest.TestCase):
    def test_valid_range_advances_confirmed_offset(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(308, headers={"Range": "bytes=0-99"}),
            _FakeResponse(200, json_body={"id": "f1"}),
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            result = _run_with_scripted_responses(responses)
        self.assertEqual(result["status"], "success")
        chunk_entries = [r for r in result["requests"] if r["kind"] == "chunk"]
        self.assertEqual(chunk_entries[1]["offset_start"], 100)

    def test_missing_range_triggers_status_query_not_a_guess(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(308, headers={}),  # chunk 1: 308, no Range
            _FakeResponse(308, headers={"Range": "bytes=0-99"}),  # status query
            _FakeResponse(200, json_body={"id": "f1"}),
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            result = _run_with_scripted_responses(responses)
        kinds = [r["kind"] for r in result["requests"]]
        self.assertIn("status_query", kinds)
        # the retried chunk must resume from the status-query-confirmed
        # offset (100), not a guessed end+1
        chunk_entries = [r for r in result["requests"] if r["kind"] == "chunk"]
        self.assertEqual(chunk_entries[-1]["offset_start"], 100)


class RecoveryTests(unittest.TestCase):
    def test_502_triggers_status_query_then_resume(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(502),  # chunk 1 fails
            _FakeResponse(308, headers={"Range": "bytes=0-99"}),  # status query
            _FakeResponse(200, json_body={"id": "f1"}),
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            result = _run_with_scripted_responses(responses)
        self.assertEqual(result["status"], "success")

    def test_confirmed_offset_unchanged_when_status_query_reports_no_progress(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(502),  # chunk 1 (offset 0) fails
            _FakeResponse(308, headers={}),  # status query: nothing received -> 0
            _FakeResponse(502),  # retried chunk 1 fails again
            _FakeResponse(308, headers={}),  # status query again: still 0
            _FakeResponse(502),  # third attempt
        ]
        with mock.patch.object(drive_io, "get_drive_service"):
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["confirmed_offset"], 0)

    def test_bounded_recovery_never_loops_forever(self):
        responses = [_FakeResponse(200, headers={"Location": _LOCATION})]
        responses += [_FakeResponse(502) for _ in range(20)]  # far more than the bound allows
        with mock.patch.object(drive_io, "get_drive_service"):
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "failure")
        # bounded: init + a small, fixed number of chunk/status-query pairs
        self.assertLess(len(result["requests"]), 15)

    def test_no_session_restart_on_expired_session(self):
        """This diagnostic pass deliberately does not restart the resumable
        session on a 404 -- it must be reported as a failed/expired outcome,
        not silently worked around."""
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(502),
            _FakeResponse(404),  # status query says the session is gone
        ]
        with mock.patch.object(drive_io, "get_drive_service"):
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "failure")
        # only one init POST -- no second session was ever opened
        init_entries = [r for r in result["requests"] if r["kind"] == "init"]
        self.assertEqual(len(init_entries), 1)


class ConfirmedOffsetOnStatusQueryCompleteTests(unittest.TestCase):
    """A status query reporting 200/201 means Drive considers the upload
    fully complete -- confirmed_offset must always be file_size_bytes in
    that case, never whatever byte offset was last attempted/confirmed
    before the status query ran. Reporting the stale offset would make a
    healthy upload look like it got stuck partway through."""

    def test_case_a_stale_offset_after_partial_progress_then_complete(self):
        # chunk 1 advances via 308 to offset 100, chunk 2 fails outright,
        # and the status query says the whole 300-byte upload is done.
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(308, headers={"Range": "bytes=0-99"}),  # chunk 1
            _FakeResponse(502),  # chunk 2 (offset 100) fails outright
            _FakeResponse(200, json_body={"id": "f1"}),  # status query: complete
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["confirmed_offset"], 300)
        self.assertEqual(result["cleanup"], "trashed")

    def test_transport_exception_then_status_query_complete(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            TimeoutError("boom"),  # chunk 1 raises
            _FakeResponse(200, json_body={"id": "f1"}),  # status query: complete
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["confirmed_offset"], 300)

    def test_missing_range_then_status_query_complete(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(308, headers={}),  # chunk 1: 308, no Range
            _FakeResponse(200, json_body={"id": "f1"}),  # status query: complete
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["confirmed_offset"], 300)

    def test_unexpected_http_status_then_status_query_complete(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(502),  # chunk 1: unexpected status
            _FakeResponse(200, json_body={"id": "f1"}),  # status query: complete
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["confirmed_offset"], 300)


class SecurityTests(unittest.TestCase):
    def test_session_url_and_secrets_never_logged_or_returned(self):
        secret_location = "https://secret-session-url.example/abc?upload_id=SECRET_TOKEN"
        responses = [
            _FakeResponse(200, headers={"Location": secret_location}),
            TimeoutError("boom access_token=LEAKED_SECRET"),
            _FakeResponse(308, headers={"Range": "bytes=0-99"}),
            _FakeResponse(200, json_body={"id": "f1"}),
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            get_service.return_value.files.return_value.update.return_value.execute.return_value = {}
            with self.assertLogs(level="WARNING") as logs:
                result = _run_with_scripted_responses(responses)
        joined = "\n".join(logs.output)
        self.assertNotIn(secret_location, joined)
        self.assertNotIn("SECRET_TOKEN", joined)
        self.assertNotIn("LEAKED_SECRET", joined)
        self.assertNotIn(secret_location, str(result))
        self.assertNotIn("SECRET_TOKEN", str(result))


class CleanupTests(unittest.TestCase):
    def test_completed_upload_is_trashed(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(200, json_body={"id": "f1", "name": "n.xlsx"}),
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            fake_service = mock.Mock()
            get_service.return_value = fake_service
            result = _run_with_scripted_responses(responses, chunk_size_bytes=1000, file_size_bytes=50)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["cleanup"], "trashed")
        fake_service.files.return_value.update.assert_called_once()
        _, kwargs = fake_service.files.return_value.update.call_args
        self.assertEqual(kwargs["fileId"], "f1")
        self.assertEqual(kwargs["body"], {"trashed": True})

    def test_incomplete_upload_has_nothing_to_clean_up(self):
        responses = [
            _FakeResponse(200, headers={"Location": _LOCATION}),
            _FakeResponse(502),
            _FakeResponse(404),
        ]
        with mock.patch.object(drive_io, "get_drive_service") as get_service:
            result = _run_with_scripted_responses(responses, chunk_size_bytes=100, file_size_bytes=300)
        self.assertEqual(result["status"], "failure")
        self.assertNotIn("cleanup", result)
        get_service.return_value.files.return_value.update.assert_not_called()


class EndpointTests(unittest.TestCase):
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
            resp = self.client.post("/admin/drive/resumable-full-diagnostic", json={})
        self.assertEqual(resp.status_code, 401)

    def _post(self, payload, *, run_result=None):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.run_full_resumable_diagnostic", return_value=run_result
        ) as run_mock:
            resp = self.client.post(
                "/admin/drive/resumable-full-diagnostic", json=payload, headers={"X-Admin-Key": "secret"}
            )
        return resp, run_mock

    def test_success_dispatches_with_validated_args(self):
        fake_result = {
            "status": "success",
            "connection_mode": "reuse",
            "file_size_bytes": 10143332,
            "chunk_size_bytes": 1048576,
            "confirmed_offset": 10143332,
            "requests": [],
        }
        resp, run_mock = self._post(
            {"file_size_bytes": 10143332, "chunk_size_bytes": 1048576, "connection_mode": "reuse"},
            run_result=fake_result,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "success")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.kwargs["file_size_bytes"], 10143332)
        self.assertEqual(run_mock.call_args.kwargs["chunk_size_bytes"], 1048576)
        self.assertEqual(run_mock.call_args.kwargs["connection_mode"], "reuse")

    def test_missing_file_size_bytes_rejected(self):
        resp, run_mock = self._post({"chunk_size_bytes": 1048576, "connection_mode": "reuse"})
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_file_size_bytes_bool_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": True, "chunk_size_bytes": 1048576, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_file_size_bytes_oversized_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 500 * 1024 * 1024, "chunk_size_bytes": 1048576, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_file_size_bytes_numeric_string_rejected(self):
        # no implicit coercion -- "10143332" must not silently become 10143332
        resp, run_mock = self._post(
            {"file_size_bytes": "10143332", "chunk_size_bytes": 1048576, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_file_size_bytes_float_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 10143332.9, "chunk_size_bytes": 1048576, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_file_size_bytes_null_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": None, "chunk_size_bytes": 1048576, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_chunk_size_not_multiple_of_256kib_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 1000, "chunk_size_bytes": 100, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_chunk_size_bool_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 1000, "chunk_size_bytes": True, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_chunk_size_numeric_string_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 1000, "chunk_size_bytes": "1048576", "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_chunk_size_float_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 1000, "chunk_size_bytes": 1048576.0, "connection_mode": "reuse"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_connection_mode_missing_rejected(self):
        resp, run_mock = self._post({"file_size_bytes": 1000, "chunk_size_bytes": 262144})
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_connection_mode_invalid_value_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 1000, "chunk_size_bytes": 262144, "connection_mode": "turbo"}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_connection_mode_non_string_rejected(self):
        resp, run_mock = self._post(
            {"file_size_bytes": 1000, "chunk_size_bytes": 262144, "connection_mode": 1}
        )
        self.assertEqual(resp.status_code, 400)
        run_mock.assert_not_called()

    def test_folder_id_not_accepted_from_request_body(self):
        fake_result = {"status": "success", "connection_mode": "reuse", "requests": [], "confirmed_offset": 0}
        resp, run_mock = self._post(
            {
                "file_size_bytes": 1000,
                "chunk_size_bytes": 262144,
                "connection_mode": "reuse",
                "folder_id": "attacker-controlled-folder",
            },
            run_result=fake_result,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(run_mock.call_args.kwargs["folder_id"], "attacker-controlled-folder")


if __name__ == "__main__":
    unittest.main()
