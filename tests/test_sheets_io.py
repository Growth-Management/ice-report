import importlib
import os
import sys
import unittest
from unittest import mock

import sheets_io


def _reimport_real(root: str, target: str):
    """Some sibling test modules install incomplete stand-ins for
    "google.auth.exceptions.RefreshError" and "googleapiclient.errors.HttpError"
    (both aliased to plain RuntimeError) via sys.modules.setdefault(...) at
    *import* time (see test_admin_iap_auth.py's `_install_import_stubs()`),
    which can land before this file is even collected depending on pytest's
    file order. If both exceptions alias to the same class, sheets_io's
    isinstance-based branching can no longer tell them apart. Evict any
    matching sys.modules entries and force a genuine reimport of the real,
    actually-installed package to get trustworthy, distinct classes, then put
    back whatever was there so later test files still see what they expect.
    """
    saved = {n: m for n, m in sys.modules.items() if n == root or n.startswith(root + ".")}
    for n in saved:
        del sys.modules[n]
    try:
        module = importlib.import_module(target)
    finally:
        for n in list(sys.modules):
            if n == root or n.startswith(root + "."):
                del sys.modules[n]
        sys.modules.update(saved)
    return module


_REAL_REFRESH_ERROR = _reimport_real("google", "google.auth.exceptions").RefreshError
_REAL_HTTP_ERROR = _reimport_real("googleapiclient", "googleapiclient.errors").HttpError


class SheetsIoTests(unittest.TestCase):
    def setUp(self):
        sheets_io.RefreshError = _REAL_REFRESH_ERROR
        sheets_io.HttpError = _REAL_HTTP_ERROR

    def test_default_auth_mode_uses_adc_credentials(self):
        credentials = object()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sheets_io.google.auth,
            "default",
            return_value=(credentials, "ice-sh"),
        ) as default, mock.patch.object(sheets_io, "build", return_value="service") as build:
            service = sheets_io.get_sheets_service()

        self.assertEqual(service, "service")
        default.assert_called_once_with(scopes=[sheets_io.SHEETS_READONLY_SCOPE])
        build.assert_called_once_with("sheets", "v4", credentials=credentials, cache_discovery=False)

    def test_unsupported_auth_mode_rejected(self):
        with mock.patch.dict(os.environ, {"SHEETS_AUTH_MODE": "bogus"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "sheets_auth_mode_unsupported"):
                sheets_io._sheets_credentials()

    def test_oauth_auth_mode_uses_own_sheets_oauth_env_vars(self):
        env = {
            "SHEETS_AUTH_MODE": "oauth",
            "SHEETS_OAUTH_CLIENT_ID": "sheets-client-id",
            "SHEETS_OAUTH_CLIENT_SECRET": "sheets-client-secret",
            "SHEETS_OAUTH_REFRESH_TOKEN": "sheets-refresh-token",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            credentials = sheets_io._sheets_credentials()

        self.assertEqual(credentials.client_id, "sheets-client-id")
        self.assertEqual(credentials.client_secret, "sheets-client-secret")
        self.assertEqual(credentials.refresh_token, "sheets-refresh-token")
        self.assertEqual(credentials.scopes, [sheets_io.SHEETS_READONLY_SCOPE])

    def test_oauth_auth_mode_falls_back_to_drive_oauth_env_vars(self):
        """Production is expected to reuse the same sinohara@impress.co.jp
        Drive OAuth credential (extended to also carry the Sheets scope at
        consent time) rather than provision a second one -- so when no
        SHEETS_OAUTH_* value is set, it must fall back to DRIVE_OAUTH_*."""
        env = {
            "SHEETS_AUTH_MODE": "oauth",
            "DRIVE_OAUTH_CLIENT_ID": "drive-client-id",
            "DRIVE_OAUTH_CLIENT_SECRET": "drive-client-secret",
            "DRIVE_OAUTH_REFRESH_TOKEN": "drive-refresh-token",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            credentials = sheets_io._sheets_credentials()

        self.assertEqual(credentials.client_id, "drive-client-id")
        self.assertEqual(credentials.client_secret, "drive-client-secret")
        self.assertEqual(credentials.refresh_token, "drive-refresh-token")

    def test_oauth_auth_mode_prefers_sheets_specific_over_drive_fallback(self):
        env = {
            "SHEETS_AUTH_MODE": "oauth",
            "SHEETS_OAUTH_CLIENT_ID": "sheets-client-id",
            "SHEETS_OAUTH_CLIENT_SECRET": "sheets-client-secret",
            "SHEETS_OAUTH_REFRESH_TOKEN": "sheets-refresh-token",
            "DRIVE_OAUTH_CLIENT_ID": "drive-client-id",
            "DRIVE_OAUTH_CLIENT_SECRET": "drive-client-secret",
            "DRIVE_OAUTH_REFRESH_TOKEN": "drive-refresh-token",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            credentials = sheets_io._sheets_credentials()

        self.assertEqual(credentials.client_id, "sheets-client-id")

    def test_oauth_auth_mode_requires_complete_config(self):
        env = {"SHEETS_AUTH_MODE": "oauth", "SHEETS_OAUTH_CLIENT_ID": "sheets-client-id"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "sheets_oauth_config_missing"):
                sheets_io._sheets_credentials()

    def test_secret_name_config_reads_secret_manager(self):
        env = {
            "SHEETS_AUTH_MODE": "oauth",
            "SHEETS_OAUTH_CLIENT_ID_SECRET_NAME": "sheets-oauth-client-id",
            "SHEETS_OAUTH_CLIENT_SECRET_SECRET_NAME": "sheets-oauth-client-secret",
            "SHEETS_OAUTH_REFRESH_TOKEN_SECRET_NAME": "sheets-oauth-refresh-token",
        }
        values = {
            "sheets-oauth-client-id": "client-id",
            "sheets-oauth-client-secret": "client-secret",
            "sheets-oauth-refresh-token": "refresh-token",
        }

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            sheets_io,
            "_access_secret",
            side_effect=lambda secret_name, **kwargs: values[secret_name],
        ):
            credentials = sheets_io._sheets_oauth_credentials()

        self.assertEqual(credentials.client_id, "client-id")
        self.assertEqual(credentials.client_secret, "client-secret")
        self.assertEqual(credentials.refresh_token, "refresh-token")

    def test_read_sheet_values_returns_rows(self):
        service = mock.Mock()
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["対象月", "動画リワード"], ["2026-08-01", 14017945]]
        }

        rows = sheets_io.read_sheet_values("sheet-id", "data!A1:D10", service=service)

        self.assertEqual(rows, [["対象月", "動画リワード"], ["2026-08-01", 14017945]])
        service.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
            spreadsheetId="sheet-id",
            range="data!A1:D10",
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        )

    def test_refresh_error_is_sanitized(self):
        with self.assertRaises(sheets_io.SheetsOperationError) as ctx:
            sheets_io._raise_sheets_error(_REAL_REFRESH_ERROR("invalid token"))

        self.assertEqual(ctx.exception.code, "sheets_oauth_refresh_failed")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_http_error_is_sanitized_as_access_denied(self):
        response = mock.Mock(status=403)
        http_error = _REAL_HTTP_ERROR(response, b"forbidden")

        with self.assertRaises(sheets_io.SheetsOperationError) as ctx:
            sheets_io._raise_sheets_error(http_error)

        self.assertEqual(ctx.exception.code, "sheets_access_denied")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
