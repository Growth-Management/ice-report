import os
import unittest
from unittest import mock

from google.auth.exceptions import RefreshError

import drive_io


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


if __name__ == "__main__":
    unittest.main()
