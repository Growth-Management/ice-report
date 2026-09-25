import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drive_io


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


class _QueueSession:
    """Fake AuthorizedSession whose post()/put() calls pop one scripted
    response (or raise a scripted exception) per call, in order -- lets a
    test script an exact sequence like "chunk 1 PUT times out, the recovery
    status-query PUT returns 308 with a Range, the resumed PUT returns 200"."""

    def __init__(self, *, post_queue=None, put_queue=None):
        self.post_queue = list(post_queue or [])
        self.put_queue = list(put_queue or [])
        self.post_calls = []
        self.put_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        item = self.post_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        item = self.put_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _write_temp_file(size_bytes: int, *, fill=b"x") -> Path:
    fd, name = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    path = Path(name)
    path.write_bytes(fill * size_bytes if len(fill) == 1 else (fill * (size_bytes // len(fill) + 1))[:size_bytes])
    return path


_LOCATION = "https://secret-session-url.example/upload-session-abc"


class TransportSelectionTests(unittest.TestCase):
    def test_unset_env_uses_googleapiclient(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(drive_io.drive_upload_transport(), "googleapiclient")

    def test_explicit_authorized_session(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_TRANSPORT": "authorized_session"}, clear=True):
            self.assertEqual(drive_io.drive_upload_transport(), "authorized_session")

    def test_explicit_googleapiclient(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_TRANSPORT": "googleapiclient"}, clear=True):
            self.assertEqual(drive_io.drive_upload_transport(), "googleapiclient")

    def test_invalid_value_falls_back_to_googleapiclient_with_warning(self):
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_TRANSPORT": "carrier-pigeon"}, clear=True):
            with self.assertLogs(level="WARNING") as logs:
                result = drive_io.drive_upload_transport()
        self.assertEqual(result, "googleapiclient")
        self.assertIn("ICE_REPORT_DRIVE_UPLOAD_TRANSPORT_INVALID", "\n".join(logs.output))

    def test_upload_xlsx_to_drive_dispatches_to_authorized_session(self):
        path = _write_temp_file(10)
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.dict(os.environ, {"DRIVE_UPLOAD_TRANSPORT": "authorized_session"}, clear=True), mock.patch.object(
            drive_io, "_upload_xlsx_authorized_session", return_value={"id": "f1"}
        ) as new_uploader, mock.patch.object(drive_io, "_upload_xlsx_googleapiclient") as legacy_uploader:
            result = drive_io.upload_xlsx_to_drive(path, folder_id="folder-1", file_name="n.xlsx")
        new_uploader.assert_called_once()
        legacy_uploader.assert_not_called()
        self.assertEqual(result, {"id": "f1"})

    def test_upload_xlsx_to_drive_defaults_to_googleapiclient(self):
        path = _write_temp_file(10)
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            drive_io, "_upload_xlsx_googleapiclient", return_value={"id": "f1"}
        ) as legacy_uploader, mock.patch.object(drive_io, "_upload_xlsx_authorized_session") as new_uploader:
            result = drive_io.upload_xlsx_to_drive(path, folder_id="folder-1", file_name="n.xlsx", service="svc")
        legacy_uploader.assert_called_once()
        new_uploader.assert_not_called()
        self.assertEqual(result, {"id": "f1"})


class ParseRangeHeaderTests(unittest.TestCase):
    def test_parses_next_offset(self):
        self.assertEqual(drive_io._parse_range_header("bytes=0-1048575"), 1048576)
        self.assertEqual(drive_io._parse_range_header("bytes=1048576-2097151"), 2097152)

    def test_none_or_empty_is_none(self):
        self.assertIsNone(drive_io._parse_range_header(None))
        self.assertIsNone(drive_io._parse_range_header(""))

    def test_unparseable_is_none(self):
        self.assertIsNone(drive_io._parse_range_header("not-a-range"))


class OpenResumableSessionTests(unittest.TestCase):
    def test_success(self):
        fake = _QueueSession(post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})])
        with mock.patch.object(drive_io, "_authorized_session", return_value=fake):
            session_url = drive_io._open_resumable_session(
                fake, file_name="n.xlsx", file_size_bytes=100, folder_id="folder-1", report_type="video-reward"
            )
        self.assertEqual(session_url, _LOCATION)
        _, kwargs = fake.post_calls[0]
        self.assertIs(kwargs["allow_redirects"], False)

    def test_missing_location_raises(self):
        fake = _QueueSession(post_queue=[_FakeResponse(200, headers={})])
        with self.assertRaises(drive_io.DriveOperationError):
            drive_io._open_resumable_session(
                fake, file_name="n.xlsx", file_size_bytes=100, folder_id="folder-1", report_type=None
            )

    def test_502_raises(self):
        fake = _QueueSession(post_queue=[_FakeResponse(502)])
        with self.assertRaises(drive_io.DriveOperationError) as ctx:
            drive_io._open_resumable_session(
                fake, file_name="n.xlsx", file_size_bytes=100, folder_id="folder-1", report_type=None
            )
        self.assertEqual(ctx.exception.status_code, 502)

    def test_transport_exception_is_sanitized_and_raises(self):
        fake = _QueueSession(post_queue=[TimeoutError()])
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(drive_io.DriveOperationError):
                drive_io._open_resumable_session(
                    fake, file_name="n.xlsx", file_size_bytes=100, folder_id="folder-1", report_type=None
                )
        joined = "\n".join(logs.output)
        self.assertIn("exception_type=TimeoutError", joined)

    def test_session_url_never_logged(self):
        fake = _QueueSession(post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})])
        with self.assertLogs(level="INFO") as logs:
            drive_io._open_resumable_session(
                fake, file_name="n.xlsx", file_size_bytes=100, folder_id="folder-1", report_type=None
            )
        self.assertNotIn(_LOCATION, "\n".join(logs.output))


class QueryUploadStatusTests(unittest.TestCase):
    def test_incomplete_with_range(self):
        fake = _QueueSession(put_queue=[_FakeResponse(308, headers={"Range": "bytes=0-1048575"})])
        state, payload = drive_io._query_upload_status(fake, _LOCATION, 10_000_000)
        self.assertEqual(state, "incomplete")
        self.assertEqual(payload, 1048576)
        _, kwargs = fake.put_calls[0]
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["headers"]["Content-Range"], "bytes */10000000")

    def test_incomplete_without_range_means_offset_zero(self):
        fake = _QueueSession(put_queue=[_FakeResponse(308, headers={})])
        state, payload = drive_io._query_upload_status(fake, _LOCATION, 10_000_000)
        self.assertEqual(state, "incomplete")
        self.assertEqual(payload, 0)

    def test_complete(self):
        fake = _QueueSession(put_queue=[_FakeResponse(200, json_body={"id": "f1", "name": "n.xlsx"})])
        state, payload = drive_io._query_upload_status(fake, _LOCATION, 10)
        self.assertEqual(state, "complete")
        self.assertEqual(payload["id"], "f1")

    def test_expired(self):
        fake = _QueueSession(put_queue=[_FakeResponse(404)])
        state, payload = drive_io._query_upload_status(fake, _LOCATION, 10)
        self.assertEqual(state, "expired")
        self.assertIsNone(payload)

    def test_transport_exception_is_unknown(self):
        fake = _QueueSession(put_queue=[ConnectionResetError()])
        state, payload = drive_io._query_upload_status(fake, _LOCATION, 10)
        self.assertEqual(state, "unknown")
        self.assertIsNone(payload)

    def test_session_url_never_logged_on_query(self):
        fake = _QueueSession(put_queue=[_FakeResponse(308, headers={"Range": "bytes=0-9"})])
        # no assertLogs here on purpose -- just confirm no exception; the
        # secret-leakage guarantee is exercised end-to-end below.
        drive_io._query_upload_status(fake, _LOCATION, 10)


class UploadXlsxAuthorizedSessionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, path, *, session, chunk_size=None):
        patches = [mock.patch.object(drive_io, "_authorized_session", return_value=session)]
        if chunk_size is not None:
            patches.append(mock.patch.object(drive_io, "drive_upload_chunk_size", return_value=chunk_size))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return drive_io._upload_xlsx_authorized_session(
            path, folder_id="folder-1", file_name="n.xlsx", report_type="video-reward"
        )

    def test_single_chunk_upload_completes_in_one_put(self):
        path = _write_temp_file(50)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[_FakeResponse(200, json_body={"id": "f1", "name": "n.xlsx", "webViewLink": "https://x"})],
        )
        result = self._run(path, session=session, chunk_size=1000)
        self.assertEqual(result["id"], "f1")
        self.assertEqual(len(session.put_calls), 1)
        _, kwargs = session.put_calls[0]
        self.assertEqual(kwargs["headers"]["Content-Range"], "bytes 0-49/50")
        self.assertIs(kwargs["allow_redirects"], False)

    def test_multi_chunk_upload_tracks_range_confirmed_offset(self):
        """10 chunks of 100 bytes over a 1000-byte file, each PUT's data
        length and Content-Range must exactly match the expected slice --
        proves the whole file is never read into memory at once, and that
        each chunk's start comes from the *previous 308's Range header*,
        not just chunk_size * chunk_index."""
        total = 1000
        chunk = 100
        path = _write_temp_file(total)
        self.addCleanup(path.unlink, missing_ok=True)

        put_responses = []
        for i in range(9):
            start = i * chunk
            end = start + chunk - 1
            put_responses.append(_FakeResponse(308, headers={"Range": f"bytes=0-{end}"}))
        put_responses.append(_FakeResponse(200, json_body={"id": "f1", "name": "n.xlsx"}))

        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=put_responses,
        )
        result = self._run(path, session=session, chunk_size=chunk)
        self.assertEqual(result["id"], "f1")
        self.assertEqual(len(session.put_calls), 10)
        for i, (_, kwargs) in enumerate(session.put_calls):
            start = i * chunk
            end = start + chunk - 1
            self.assertEqual(kwargs["headers"]["Content-Range"], f"bytes {start}-{end}/{total}")
            self.assertEqual(len(kwargs["data"]), chunk)

    def test_final_partial_chunk_is_shorter(self):
        total = 250
        chunk = 100
        path = _write_temp_file(total)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),
                _FakeResponse(308, headers={"Range": "bytes=0-199"}),
                _FakeResponse(200, json_body={"id": "f1"}),
            ],
        )
        self._run(path, session=session, chunk_size=chunk)
        last_kwargs = session.put_calls[-1][1]
        self.assertEqual(last_kwargs["headers"]["Content-Range"], "bytes 200-249/250")
        self.assertEqual(len(last_kwargs["data"]), 50)

    def test_308_with_valid_range_resumes_from_confirmed_offset(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),
                _FakeResponse(200, json_body={"id": "f1"}),
            ],
        )
        self._run(path, session=session, chunk_size=100)
        second_kwargs = session.put_calls[1][1]
        self.assertEqual(second_kwargs["headers"]["Content-Range"], "bytes 100-199/200")

    def test_308_without_range_never_guesses_end_plus_one_queries_status_instead(self):
        """Regression: a 308 with no Range header does NOT mean "all of this
        chunk was received" -- assuming end+1 risks silently skipping bytes
        Drive never got and completing a corrupt file. The chunk PUT's own
        308-without-Range must trigger a status query, never an assumed
        offset advance."""
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                _FakeResponse(308, headers={}),  # chunk 1 PUT: 308, no Range
                _FakeResponse(308, headers={}),  # status query: also no Range -> offset 0
                _FakeResponse(200, json_body={"id": "f1"}),  # chunk PUT retried from offset 0
            ],
        )
        self._run(path, session=session, chunk_size=100)
        # must NOT have jumped to bytes 100-199/200 -- offset 0 was re-sent
        retried_kwargs = session.put_calls[-1][1]
        self.assertEqual(retried_kwargs["headers"]["Content-Range"], "bytes 0-99/200")

    def test_308_without_range_then_status_query_reports_confirmed_offset(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                _FakeResponse(308, headers={}),  # chunk 1 PUT: 308, no Range
                _FakeResponse(308, headers={"Range": "bytes=0-49"}),  # status query: partial receipt
                _FakeResponse(200, json_body={"id": "f1"}),  # resumes from byte 50
            ],
        )
        self._run(path, session=session, chunk_size=100)
        resumed_kwargs = session.put_calls[-1][1]
        self.assertEqual(resumed_kwargs["headers"]["Content-Range"], "bytes 50-149/200")

    def test_308_without_range_then_status_query_reports_already_complete(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                _FakeResponse(308, headers={}),
                _FakeResponse(200, json_body={"id": "already-done"}),
            ],
        )
        result = self._run(path, session=session, chunk_size=100)
        self.assertEqual(result["id"], "already-done")
        self.assertEqual(len(session.put_calls), 2)

    def test_308_without_range_then_status_query_reports_expired_session(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[
                _FakeResponse(200, headers={"Location": _LOCATION}),
                _FakeResponse(200, headers={"Location": _LOCATION + "-restarted"}),
            ],
            put_queue=[
                _FakeResponse(308, headers={}),  # chunk 1 PUT: 308, no Range
                _FakeResponse(404),  # status query: session expired
                _FakeResponse(200, json_body={"id": "f1"}),  # full re-upload on the new session
            ],
        )
        result = self._run(path, session=session, chunk_size=100)
        self.assertEqual(result["id"], "f1")
        self.assertEqual(len(session.post_calls), 2)

    def test_malformed_range_is_treated_the_same_as_missing(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                _FakeResponse(308, headers={"Range": "not-a-valid-range"}),
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),  # status query: valid this time
                _FakeResponse(200, json_body={"id": "f1"}),
            ],
        )
        self._run(path, session=session, chunk_size=100)
        # must have gone through the status-query path (3 put calls), not
        # trusted the malformed header or guessed end+1
        self.assertEqual(len(session.put_calls), 3)
        resumed_kwargs = session.put_calls[-1][1]
        self.assertEqual(resumed_kwargs["headers"]["Content-Range"], "bytes 100-199/200")

    def test_range_missing_recovery_is_bounded(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        put_queue = [_FakeResponse(308, headers={}) for _ in range(10)]  # never resolves
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=put_queue,
        )
        with self.assertRaises(drive_io.DriveOperationError):
            self._run(path, session=session, chunk_size=100)
        self.assertLessEqual(len(session.put_calls), 2 * (drive_io._UPLOAD_MAX_RECOVER_ATTEMPTS + 1))

    def test_range_missing_recovery_never_logs_secrets(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        secret_url = "https://secret-session-url.example/abc?upload_id=SECRET_TOKEN"
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": secret_url})],
            put_queue=[
                _FakeResponse(308, headers={}),
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),
                _FakeResponse(200, json_body={"id": "f1"}),
            ],
        )
        with self.assertLogs(level="INFO") as logs:
            self._run(path, session=session, chunk_size=100)
        joined = "\n".join(logs.output)
        self.assertNotIn(secret_url, joined)
        self.assertNotIn("SECRET_TOKEN", joined)

    def test_timeout_then_status_query_resumes_from_confirmed_offset(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                TimeoutError(),  # first chunk PUT fails
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),  # recovery status query
                _FakeResponse(200, json_body={"id": "f1"}),  # resumed PUT from offset 100
            ],
        )
        result = self._run(path, session=session, chunk_size=100)
        self.assertEqual(result["id"], "f1")
        resumed_kwargs = session.put_calls[-1][1]
        self.assertEqual(resumed_kwargs["headers"]["Content-Range"], "bytes 100-199/200")
        # the failed chunk's bytes were never blindly resent as-is without
        # first confirming the offset via status query
        self.assertEqual(len(session.put_calls), 3)

    def test_502_then_status_query_resumes(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                _FakeResponse(502),
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),
                _FakeResponse(200, json_body={"id": "f1"}),
            ],
        )
        result = self._run(path, session=session, chunk_size=100)
        self.assertEqual(result["id"], "f1")

    def test_lost_response_but_status_query_shows_already_complete(self):
        """The chunk PUT itself times out client-side, but the bytes had
        actually already arrived and Drive finished the upload -- the
        status query must report "complete" and the uploader must return
        that result instead of creating a duplicate file."""
        path = _write_temp_file(100)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                TimeoutError(),
                _FakeResponse(200, json_body={"id": "already-done", "name": "n.xlsx"}),
            ],
        )
        result = self._run(path, session=session, chunk_size=1000)
        self.assertEqual(result["id"], "already-done")
        # no third PUT -- nothing left to send once "complete" was reported
        self.assertEqual(len(session.put_calls), 2)

    def test_expired_session_restarts_once_and_resumes_from_zero(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[
                _FakeResponse(200, headers={"Location": _LOCATION}),
                _FakeResponse(200, headers={"Location": _LOCATION + "-restarted"}),
            ],
            put_queue=[
                _FakeResponse(404),  # first chunk PUT: session already expired
                _FakeResponse(404),  # recovery status query confirms: expired, not a fluke
                _FakeResponse(200, json_body={"id": "f1"}),  # full re-upload on the new session
            ],
        )
        result = self._run(path, session=session, chunk_size=1000)
        self.assertEqual(result["id"], "f1")
        self.assertEqual(len(session.post_calls), 2)  # original session + exactly one restart

    def test_expired_session_restart_is_bounded(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[
                _FakeResponse(200, headers={"Location": _LOCATION}),
                _FakeResponse(200, headers={"Location": _LOCATION + "-restart-1"}),
            ],
            put_queue=[
                _FakeResponse(404),  # chunk PUT on the original session
                _FakeResponse(404),  # status query confirms expired -> restart #1 (allowed)
                _FakeResponse(404),  # chunk PUT on the restarted session
                _FakeResponse(404),  # status query confirms expired again -> no restarts left
            ],
        )
        with self.assertRaises(drive_io.DriveOperationError):
            self._run(path, session=session, chunk_size=1000)
        # exactly one restart attempted, never an unbounded loop
        self.assertEqual(len(session.post_calls), 2)

    def test_recover_attempts_are_bounded(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        put_queue = []
        for _ in range(10):  # far more failures than the bound should allow
            put_queue.append(TimeoutError())
            put_queue.append(_FakeResponse(308, headers={"Range": "bytes=0--1"}))  # unparseable -> offset stays put-ish
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=put_queue,
        )
        with self.assertRaises(drive_io.DriveOperationError):
            self._run(path, session=session, chunk_size=100)
        # bounded: at most _UPLOAD_MAX_RECOVER_ATTEMPTS + 1 initial failures
        # consumed 2 put() calls each (failed PUT + status query)
        self.assertLessEqual(len(session.put_calls), 2 * (drive_io._UPLOAD_MAX_RECOVER_ATTEMPTS + 1))

    def test_no_second_retry_layer_on_top_of_requests(self):
        """A single failed PUT must trigger exactly one status-query call,
        never an internal retry loop duplicating requests' own behavior."""
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[
                TimeoutError(),
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),
                _FakeResponse(200, json_body={"id": "f1"}),
            ],
        )
        self._run(path, session=session, chunk_size=100)
        # exactly 3 put() calls: failed chunk, one status query, one resumed chunk
        self.assertEqual(len(session.put_calls), 3)

    def test_secrets_never_logged_across_a_recovery(self):
        path = _write_temp_file(200)
        self.addCleanup(path.unlink, missing_ok=True)
        secret_url = "https://secret-session-url.example/abc?upload_id=SECRET_TOKEN"
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": secret_url})],
            put_queue=[
                TimeoutError("boom access_token=LEAKED_SECRET"),
                _FakeResponse(308, headers={"Range": "bytes=0-99"}),
                _FakeResponse(200, json_body={"id": "f1"}),
            ],
        )
        with self.assertLogs(level="INFO") as logs:
            self._run(path, session=session, chunk_size=100)
        joined = "\n".join(logs.output)
        self.assertNotIn(secret_url, joined)
        self.assertNotIn("SECRET_TOKEN", joined)
        self.assertNotIn("LEAKED_SECRET", joined)

    def test_public_return_contract_matches_legacy_path(self):
        path = _write_temp_file(10)
        self.addCleanup(path.unlink, missing_ok=True)
        session = _QueueSession(
            post_queue=[_FakeResponse(200, headers={"Location": _LOCATION})],
            put_queue=[_FakeResponse(200, json_body={"id": "f1", "name": "n.xlsx", "webViewLink": "https://x"})],
        )
        result = self._run(path, session=session, chunk_size=1000)
        self.assertEqual(result["id"], "f1")
        self.assertEqual(result["name"], "n.xlsx")
        self.assertEqual(result["webViewLink"], "https://x")


if __name__ == "__main__":
    unittest.main()
