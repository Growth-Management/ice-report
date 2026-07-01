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
            patch=lambda *args, **kwargs: (lambda func: func),
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
        "archive_report_definition",
        "create_delivery_record",
        "create_report_definition",
        "find_delivery_by_token",
        "get_current_version",
        "get_report_definition",
        "list_delivery_records",
        "list_download_log_records",
        "list_report_definitions",
        "log_download",
        "make_signed_download_url",
        "render_download_form",
        "set_delivery_active",
        "update_report_definition",
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

    def test_public_report_definition_versions_are_whitelisted(self):
        distribution = _load_distribution_module()

        item = distribution._public_report_definition(
            "monthly-downloads",
            {
                "current_version": 2,
                "versions": [
                    {
                        "version": 1,
                        "status": "published",
                        "note": "initial",
                        "query_sql": "select raw_email from table",
                        "template_mapping": {"A1": "email"},
                        "signed_url": "https://example.test/signed",
                        "created_by": "user@example.com",
                    },
                    {
                        "version": 2,
                        "status": "published",
                        "change_summary": "current version",
                        "template_file_name": "template.xlsx",
                        "query_config_id": "plus-monthly-v2",
                        "mapping_version_id": "mapping-v2",
                    },
                ],
            },
            include_versions=True,
        )

        versions = item["versions"]
        self.assertEqual([version["version"] for version in versions], [2, 1])
        self.assertTrue(versions[0]["current"])
        self.assertEqual(versions[0]["template_name"], "template.xlsx")
        self.assertEqual(versions[0]["query_config_id"], "plus-monthly-v2")

        forbidden = {
            "query_sql",
            "template_mapping",
            "signed_url",
            "created_by",
        }
        for version in versions:
            self.assertTrue(forbidden.isdisjoint(version))

    def test_report_definition_reserved_id_is_rejected_before_firestore(self):
        distribution = _load_distribution_module()

        with self.assertRaisesRegex(ValueError, "report_id not found"):
            distribution._validate_report_id("__reserved__")

    def test_report_definition_payload_is_limited_to_editable_metadata(self):
        distribution = _load_distribution_module()

        payload = distribution._report_definition_payload(
            {
                "name": " 月次DL ",
                "owner": "システム管理室",
                "primary_operator": "篠原邦昭",
                "customer_name": "一ツ橋企画",
                "default_report_month": "2026-06",
                "gcs_prefix": "reports/plus/",
                "drive_folder_name": "OMF",
                "query_sql": "select raw_email from table",
                "template_mapping": {"A1": "raw_email"},
                "allowed_emails": ["user@example.com"],
            }
        )

        self.assertEqual(payload["name"], "月次DL")
        self.assertEqual(set(payload), set(distribution.REPORT_DEFINITION_EDITABLE_FIELDS))
        self.assertNotIn("query_sql", payload)
        self.assertNotIn("template_mapping", payload)
        self.assertNotIn("allowed_emails", payload)

    def test_report_definition_id_pattern_accepts_slug(self):
        distribution = _load_distribution_module()

        self.assertEqual(
            distribution._validate_report_id("plus-monthly_downloads.v1"),
            "plus-monthly_downloads.v1",
        )


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


class TemplatePreviewTest(unittest.TestCase):
    def _install_openpyxl_stub(self) -> None:
        class _Worksheet:
            title = "Preview"
            max_row = 2
            max_column = 2
            tables = {"Table1": object()}
            sheet_state = "visible"

        class _Workbook:
            worksheets = [_Worksheet()]

            def close(self):
                return None

        openpyxl_stub = types.ModuleType("openpyxl")
        openpyxl_stub.load_workbook = lambda *args, **kwargs: _Workbook()
        sys.modules["openpyxl"] = openpyxl_stub

    def _workbook_bytes(self) -> bytes:
        return b"fake-xlsx-content-without-cell-values"

    def test_template_preview_returns_structure_without_cell_values(self):
        _install_app_import_stubs()
        self._install_openpyxl_stub()
        import app as app_module

        preview = app_module._preview_xlsx_template_bytes(
            self._workbook_bytes(),
            r"C:\fake\template.xlsx",
        )

        self.assertEqual(preview["file_name"], "template.xlsx")
        self.assertEqual(preview["sheet_count"], 1)
        self.assertEqual(preview["sheets"][0]["name"], "Preview")
        self.assertEqual(preview["sheets"][0]["max_row"], 2)
        self.assertEqual(preview["sheets"][0]["max_column"], 2)
        self.assertEqual(preview["sheets"][0]["table_count"], 1)
        self.assertIn("sha256", preview)
        self.assertNotIn("raw-email@example.com", str(preview))
        self.assertNotIn("secret-value", str(preview))

    def test_template_preview_rejects_non_xlsx_file_name(self):
        _install_app_import_stubs()
        import app as app_module

        with self.assertRaisesRegex(ValueError, "must end with .xlsx"):
            app_module._preview_xlsx_template_bytes(self._workbook_bytes(), "template.xls")

    def test_template_preview_rejects_too_large_file(self):
        _install_app_import_stubs()
        import app as app_module

        with self.assertRaisesRegex(ValueError, "too large"):
            app_module._preview_xlsx_template_bytes(b"12345", "template.xlsx", max_bytes=4)


if __name__ == "__main__":
    unittest.main()
