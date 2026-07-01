import os
import sys
import types
import unittest
from unittest.mock import patch


class _RequestStub:
    headers = {}
    path = "/"
    method = "GET"
    remote_addr = ""


class _RequestContextStub:
    def __init__(self, path, headers=None, method="GET"):
        self.path = path
        self.headers = headers or {}
        self.method = method

    def __enter__(self):
        _RequestStub.path = self.path
        _RequestStub.headers = self.headers
        _RequestStub.method = self.method

    def __exit__(self, exc_type, exc, traceback):
        _RequestStub.path = "/"
        _RequestStub.headers = {}
        _RequestStub.method = "GET"


class _FlaskStub:
    def __init__(self, name):
        self.name = name

    def route(self, *args, **kwargs):
        return self._decorator

    def get(self, *args, **kwargs):
        return self._decorator

    def post(self, *args, **kwargs):
        return self._decorator

    def patch(self, *args, **kwargs):
        return self._decorator

    def test_request_context(self, path, headers=None, method="GET"):
        return _RequestContextStub(path, headers=headers, method=method)

    @staticmethod
    def _decorator(func):
        return func


def _install_import_stubs():
    flask_stub = types.ModuleType("flask")
    flask_stub.Flask = _FlaskStub
    flask_stub.Request = object
    flask_stub.jsonify = lambda value=None, **kwargs: value if value is not None else kwargs
    flask_stub.make_response = lambda *args, **kwargs: args[0] if args else None
    flask_stub.redirect = lambda *args, **kwargs: None
    flask_stub.request = _RequestStub
    sys.modules.setdefault("flask", flask_stub)

    google_stub = types.ModuleType("google")
    google_auth_stub = types.ModuleType("google.auth")
    google_auth_transport_stub = types.ModuleType("google.auth.transport")
    google_auth_transport_requests_stub = types.ModuleType("google.auth.transport.requests")
    google_cloud_stub = types.ModuleType("google.cloud")
    google_cloud_stub.bigquery = types.SimpleNamespace()
    google_cloud_stub.firestore = types.SimpleNamespace(Client=object)
    google_cloud_stub.secretmanager = types.SimpleNamespace()
    google_cloud_stub.storage = types.SimpleNamespace(Client=object)
    google_auth_transport_requests_stub.Request = object
    google_stub.auth = google_auth_stub
    google_stub.cloud = google_cloud_stub
    sys.modules.setdefault("google", google_stub)
    sys.modules.setdefault("google.auth", google_auth_stub)
    sys.modules.setdefault("google.auth.transport", google_auth_transport_stub)
    sys.modules.setdefault("google.auth.transport.requests", google_auth_transport_requests_stub)
    sys.modules.setdefault("google.cloud", google_cloud_stub)

    create_report_stub = types.ModuleType("create_report")
    create_report_stub.DEFAULT_TEMPLATE = "template.xlsx"
    create_report_stub.generate_report = lambda *args, **kwargs: {}
    create_report_stub.preview_default_query_mapping = lambda *args, **kwargs: {}
    sys.modules.setdefault("create_report", create_report_stub)

    distribution_stub = types.ModuleType("distribution")
    for name in (
        "add_delivery_version",
        "archive_report_definition",
        "create_delivery_record",
        "create_report_definition",
        "download_report_definition_template",
        "find_delivery_by_token",
        "get_current_version",
        "get_report_definition",
        "list_delivery_records",
        "list_download_log_records",
        "list_report_definitions",
        "log_download",
        "make_signed_download_url",
        "publish_report_definition_template",
        "render_download_form",
        "rollback_report_definition_template",
        "set_delivery_active",
        "update_report_definition",
        "validate_delivery_access",
    ):
        setattr(distribution_stub, name, lambda *args, **kwargs: None)
    sys.modules.setdefault("distribution", distribution_stub)

    mail_provider_stub = types.ModuleType("mail_provider")
    mail_provider_stub.MailDeliveryError = RuntimeError
    sys.modules.setdefault("mail_provider", mail_provider_stub)

    mail_runtime_stub = types.ModuleType("mail_runtime")
    mail_runtime_stub.send_otp_pin_email = lambda *args, **kwargs: None
    sys.modules.setdefault("mail_runtime", mail_runtime_stub)


_install_import_stubs()

import app as app_module


class AdminIapAuthTest(unittest.TestCase):
    ENV_KEYS = {
        "ADMIN_API_KEY",
        "ADMIN_AUTH_FAIL_CLOSED",
        "ADMIN_IAP_ALLOWED_EMAILS",
        "ADMIN_IAP_AUTH_ENABLED",
        "ADMIN_IAP_SERVICE_NAME",
        "K_SERVICE",
    }

    def _env(self, **values):
        env = {key: value for key, value in values.items() if value is not None}
        return patch.dict(os.environ, env, clear=False)

    def setUp(self):
        self.saved_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key in self.ENV_KEYS:
            if self.saved_env[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self.saved_env[key]

    def test_iap_allowed_email_authenticates_without_admin_key(self):
        with self._env(
            ADMIN_IAP_AUTH_ENABLED="1",
            ADMIN_IAP_ALLOWED_EMAILS="sinohara@impress.co.jp",
            K_SERVICE="report-generator-admin",
        ):
            with app_module.app.test_request_context(
                "/deliveries",
                headers={
                    "X-Goog-Authenticated-User-Email": "accounts.google.com:sinohara@impress.co.jp"
                },
            ):
                ok, error = app_module._check_admin()

        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_iap_allowed_email_accepts_multiple_explicit_users(self):
        with self._env(
            ADMIN_IAP_AUTH_ENABLED="1",
            ADMIN_IAP_ALLOWED_EMAILS="sinohara@impress.co.jp; admin2@example.com",
            K_SERVICE="report-generator-admin",
        ):
            with app_module.app.test_request_context(
                "/deliveries",
                headers={
                    "X-Goog-Authenticated-User-Email": "accounts.google.com:ADMIN2@example.com"
                },
            ):
                ok, error = app_module._check_admin()

        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_iap_disallows_user_outside_explicit_allowlist(self):
        with self._env(
            ADMIN_IAP_AUTH_ENABLED="1",
            ADMIN_IAP_ALLOWED_EMAILS="sinohara@impress.co.jp,admin2@example.com",
            K_SERVICE="report-generator-admin",
        ):
            with app_module.app.test_request_context(
                "/deliveries",
                headers={
                    "X-Goog-Authenticated-User-Email": "accounts.google.com:outsider@example.com"
                },
            ):
                with patch.object(app_module, "_log_security_event"), patch.object(
                    app_module, "_log_admin_audit_event"
                ):
                    ok, error = app_module._check_admin()

        self.assertFalse(ok)
        self.assertIsNotNone(error)

    def test_iap_auth_requires_admin_service(self):
        with self._env(
            ADMIN_IAP_AUTH_ENABLED="1",
            ADMIN_IAP_ALLOWED_EMAILS="sinohara@impress.co.jp",
            K_SERVICE="report-generator",
        ):
            enabled = app_module._admin_iap_auth_enabled()

        self.assertFalse(enabled)

    def test_iap_actor_context_uses_hash_only(self):
        with self._env(
            ADMIN_IAP_AUTH_ENABLED="1",
            ADMIN_IAP_ALLOWED_EMAILS="sinohara@impress.co.jp",
            K_SERVICE="report-generator-admin",
        ):
            with app_module.app.test_request_context(
                "/deliveries",
                headers={
                    "X-Goog-Authenticated-User-Email": "accounts.google.com:sinohara@impress.co.jp"
                },
            ):
                actor = app_module._admin_actor_context()

        self.assertEqual(actor["actor_type"], "iap_user")
        self.assertEqual(actor["admin_key_fingerprint"], "")
        self.assertTrue(actor["iap_email_hash"])
        self.assertNotIn("sinohara", actor["iap_email_hash"])


if __name__ == "__main__":
    unittest.main()
