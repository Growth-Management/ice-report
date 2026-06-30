import importlib
import sys
import types
import unittest
from datetime import datetime, timezone


def _install_google_stubs():
    google_stub = sys.modules.get("google") or types.ModuleType("google")
    google_auth_stub = types.ModuleType("google.auth")
    google_auth_transport_stub = types.ModuleType("google.auth.transport")
    google_auth_transport_requests_stub = types.ModuleType("google.auth.transport.requests")
    google_cloud_stub = types.ModuleType("google.cloud")

    google_auth_stub.default = lambda: (types.SimpleNamespace(refresh=lambda request: None, token="token"), None)
    google_auth_transport_requests_stub.Request = object
    google_cloud_stub.firestore = types.SimpleNamespace(Client=object, Query=types.SimpleNamespace(DESCENDING="DESC"))
    google_cloud_stub.secretmanager = types.SimpleNamespace()
    google_cloud_stub.storage = types.SimpleNamespace(Client=object)

    google_stub.auth = google_auth_stub
    google_stub.cloud = google_cloud_stub

    sys.modules["google"] = google_stub
    sys.modules["google.auth"] = google_auth_stub
    sys.modules["google.auth.transport"] = google_auth_transport_stub
    sys.modules["google.auth.transport.requests"] = google_auth_transport_requests_stub
    sys.modules["google.cloud"] = google_cloud_stub


def _load_distribution_module():
    _install_google_stubs()
    sys.modules.pop("distribution", None)
    return importlib.import_module("distribution")


def _install_app_import_stubs():
    if "flask" not in sys.modules:
        flask_stub = types.ModuleType("flask")
        flask_stub.Flask = lambda name: types.SimpleNamespace(
            get=lambda *args, **kwargs: (lambda func: func),
            post=lambda *args, **kwargs: (lambda func: func),
            route=lambda *args, **kwargs: (lambda func: func),
        )
        flask_stub.jsonify = lambda value=None, **kwargs: value if value is not None else kwargs
        flask_stub.make_response = lambda value=None, *args, **kwargs: value
        flask_stub.redirect = lambda *args, **kwargs: None
        flask_stub.request = types.SimpleNamespace(headers={}, path="/", method="GET", remote_addr="", form={}, cookies={})
        sys.modules["flask"] = flask_stub

    _install_google_stubs()

    create_report_stub = types.ModuleType("create_report")
    create_report_stub.DEFAULT_TEMPLATE = "template.xlsx"
    create_report_stub.generate_report = lambda *args, **kwargs: {}
    sys.modules.setdefault("create_report", create_report_stub)

    distribution_stub = sys.modules.get("distribution") or types.ModuleType("distribution")
    for name in (
        "add_delivery_version",
        "create_delivery_record",
        "find_delivery_by_token",
        "get_current_version",
        "list_delivery_records",
        "list_download_log_records",
        "list_report_definitions",
        "log_download",
        "make_signed_download_url",
        "render_download_form",
        "set_delivery_active",
        "validate_delivery_access",
    ):
        if not hasattr(distribution_stub, name):
            setattr(distribution_stub, name, lambda *args, **kwargs: None)
    sys.modules["distribution"] = distribution_stub

    mail_provider_stub = types.ModuleType("mail_provider")
    mail_provider_stub.MailDeliveryError = RuntimeError
    sys.modules.setdefault("mail_provider", mail_provider_stub)

    mail_runtime_stub = types.ModuleType("mail_runtime")
    mail_runtime_stub.send_otp_pin_email = lambda *args, **kwargs: None
    sys.modules.setdefault("mail_runtime", mail_runtime_stub)


class ReportDefinitionPublicViewTest(unittest.TestCase):
    def test_public_report_definition_excludes_sensitive_and_editor_fields(self):
        distribution = _load_distribution_module()

        item = distribution._public_report_definition(
            "monthly-downloads",
            {
                "name": "月次ダウンロード数",
                "status": "active",
                "current_version": 3,
                "versions": [{"version": 1}, {"version": 2}, {"version": 3}],
                "owner": "システム管理室",
                "primary_operator": "篠原邦昭",
                "gcs_prefix": "reports/plus/",
                "drive": {"folder_name": "OMFダウンロード数報告"},
                "allowed_emails": ["user@example.com"],
                "query_sql": "select secret_value from table",
                "template_mapping": {"A1": "raw_email"},
                "token": "download-token",
                "signed_url": "https://example.test/signed",
                "updated_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
            },
        )

        self.assertEqual(item["report_id"], "monthly-downloads")
        self.assertEqual(item["name"], "月次ダウンロード数")
        self.assertEqual(item["version_count"], 3)
        self.assertEqual(item["drive_folder_name"], "OMFダウンロード数報告")
        self.assertEqual(item["updated_at"], "2026-06-30T00:00:00+00:00")

        forbidden = {
            "allowed_emails",
            "query_sql",
            "template_mapping",
            "token",
            "signed_url",
        }
        self.assertTrue(forbidden.isdisjoint(item))


class SelectedReportSummaryTest(unittest.TestCase):
    def test_selected_report_summary_escapes_values_and_omits_token(self):
        _install_app_import_stubs()
        import app as app_module

        html = app_module._render_selected_report_summary(
            {
                "customer_name": "<Customer>",
                "report_month": "2026-06",
                "current_version": 2,
                "active": True,
                "token": "raw-token",
                "public_download_url": "https://example.test/d/raw-token",
                "expires_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "versions": [
                    {"version": 1, "file_name": "old.xlsx"},
                    {"version": 2, "file_name": "current.xlsx"},
                ],
            }
        )

        self.assertIn("選択中レポート", html)
        self.assertIn("&lt;Customer&gt;", html)
        self.assertIn("current.xlsx", html)
        self.assertNotIn("raw-token", html)
        self.assertNotIn("https://example.test", html)


if __name__ == "__main__":
    unittest.main()
