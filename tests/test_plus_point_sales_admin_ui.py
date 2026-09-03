import os
import re
import sys
import unittest
from unittest import mock

# Some sibling test modules (test_admin_iap_auth.py, test_admin_report_definitions.py)
# replace sys.modules["flask"], sys.modules["google*"], etc. with incomplete stubs at
# *module import time* to test app.py in isolation, and never restore them. Because
# `python -m unittest discover` imports every test module (running their module-level
# code) before executing any test, whichever of those files sorts alphabetically before
# this one leaves `app.app` bound to a stub Flask class with no test_client(). Force a
# clean, real re-import here so this file's result does not depend on collection order.
_POLLUTABLE_MODULE_PREFIXES = ("flask", "app")
for _name in list(sys.modules):
    if any(_name == prefix or _name.startswith(prefix + ".") for prefix in _POLLUTABLE_MODULE_PREFIXES):
        del sys.modules[_name]

import app  # noqa: E402  (must follow the sys.modules purge above)
import drive_io  # noqa: E402


class RenderAdminUiPlusPointSalesTabTests(unittest.TestCase):
    def test_tab_button_and_panel_present(self):
        html = app.render_admin_ui()
        self.assertIn('tabBtnPlusPointSales', html)
        self.assertIn("showAdminTab('plusPointSales')", html)
        self.assertIn('tabPanelPlusPointSales', html)
        self.assertIn('id="plusPointSalesMonth" type="month"', html)
        self.assertIn('onclick="generatePlusPointSales()"', html)
        self.assertIn('onclick="loadPlusPointSalesFiles()"', html)
        self.assertNotIn('__DEFAULT_PLUS_POINT_SALES_MONTH__', html)
        self.assertNotIn('__DEFAULT_REPORT_MONTH__', html)

    def test_default_month_is_previous_month_in_asia_tokyo(self):
        html = app.render_admin_ui()
        match = re.search(r'plusPointSalesMonth" type="month" value="(\d{4}-\d{2})"', html)
        self.assertIsNotNone(match, "default month value not found in rendered HTML")

        from plus_browser_point_sales_report import previous_month_first, tokyo_today

        expected = previous_month_first(tokyo_today()).strftime("%Y-%m")
        self.assertEqual(match.group(1), expected)

    def test_generate_button_posts_to_the_manual_endpoint(self):
        html = app.render_admin_ui()
        self.assertIn('/admin/reports/plus-browser-point-sales/generate', html)
        self.assertIn('/admin/reports/plus-browser-point-sales/files', html)
        # scheduled endpoint must never be reachable from the UI
        self.assertNotIn('scheduled-generate', html)

    def test_drive_link_opens_in_new_tab_safely(self):
        html = app.render_admin_ui()
        self.assertIn('target=\\"_blank\\"', html)
        self.assertIn('rel=\\"noopener noreferrer\\"', html)


class ListPlusPointSalesFilesEndpointTests(unittest.TestCase):
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
            resp = self.client.get("/admin/reports/plus-browser-point-sales/files")
        self.assertEqual(resp.status_code, 401)

    def test_returns_shaped_items_from_drive_helper(self):
        fake_files = [
            {
                "id": "file-1",
                "name": "J+ブラウザ版_ポイント売上_26年08月分.xlsx",
                "webViewLink": "https://drive.example/file-1",
                "createdTime": "2026-09-03T00:02:50.553Z",
                "modifiedTime": "2026-09-03T00:02:50.553Z",
                "size": "14128",
                # a raw Drive field that must not leak into the API response
                "ownedByMe": True,
            }
        ]
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.list_drive_files", return_value=fake_files
        ) as list_files:
            resp = self.client.get(
                "/admin/reports/plus-browser-point-sales/files?limit=5",
                headers={"X-Admin-Key": "secret"},
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(
            body["items"],
            [
                {
                    "id": "file-1",
                    "name": "J+ブラウザ版_ポイント売上_26年08月分.xlsx",
                    "webViewLink": "https://drive.example/file-1",
                    "createdTime": "2026-09-03T00:02:50.553Z",
                    "modifiedTime": "2026-09-03T00:02:50.553Z",
                    "size": "14128",
                }
            ],
        )
        self.assertNotIn("ownedByMe", body["items"][0])

        _, kwargs = list_files.call_args
        self.assertEqual(kwargs["limit"], 5)
        self.assertEqual(kwargs["name_contains"], "J+ブラウザ版_ポイント売上_")
        self.assertEqual(kwargs["mime_type"], drive_io.DRIVE_XLSX_MIME_TYPE)

        import plus_browser_point_sales_report as report

        self.assertEqual(kwargs["folder_id"], report.default_output_folder_id())

    def test_drive_error_is_surfaced_without_crashing(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.list_drive_files",
            side_effect=drive_io.DriveOperationError("drive_access_denied", status_code=403),
        ):
            resp = self.client.get(
                "/admin/reports/plus-browser-point-sales/files",
                headers={"X-Admin-Key": "secret"},
            )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["error"], "drive_access_denied")

    def test_default_folder_is_never_taken_from_request(self):
        # folder_id must come from server-side config, not query params
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.list_drive_files", return_value=[]
        ) as list_files:
            self.client.get(
                "/admin/reports/plus-browser-point-sales/files?folder_id=attacker-controlled",
                headers={"X-Admin-Key": "secret"},
            )

        import plus_browser_point_sales_report as report

        _, kwargs = list_files.call_args
        self.assertEqual(kwargs["folder_id"], report.default_output_folder_id())
        self.assertNotEqual(kwargs["folder_id"], "attacker-controlled")

    def test_template_file_is_excluded_from_the_listing(self):
        # Regression: the template's placeholder name ("..._yy年mm月分.xlsx")
        # also contains OUTPUT_FILE_NAME_PREFIX, so Drive's `name contains`
        # query matches the template file itself. It lives in the same
        # output folder as every generated report but must never be listed
        # as one (confirmed against real Drive data during production
        # smoke testing).
        import plus_browser_point_sales_report as report

        template_id = report.default_template_file_id()
        fake_files = [
            {"id": "generated-1", "name": "J+ブラウザ版_ポイント売上_26年08月分.xlsx"},
            {"id": template_id, "name": "J+ブラウザ版_ポイント売上_yy年mm月分.xlsx"},
        ]
        with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "secret"}), mock.patch(
            "drive_io.list_drive_files", return_value=fake_files
        ):
            resp = self.client.get(
                "/admin/reports/plus-browser-point-sales/files",
                headers={"X-Admin-Key": "secret"},
            )

        body = resp.get_json()
        ids = [item["id"] for item in body["items"]]
        self.assertEqual(ids, ["generated-1"])
        self.assertNotIn(template_id, ids)


class ListDriveFilesTests(unittest.TestCase):
    def test_query_and_request_shape(self):
        captured = {}

        class _FakeList:
            def execute(self):
                return {"files": [{"id": "1"}]}

        class _FakeFiles:
            def list(self, **kwargs):
                captured.update(kwargs)
                return _FakeList()

        class _FakeService:
            def files(self):
                return _FakeFiles()

        result = drive_io.list_drive_files(
            folder_id="folder-123",
            name_contains="J+ブラウザ版_ポイント売上_",
            mime_type=drive_io.DRIVE_XLSX_MIME_TYPE,
            limit=20,
            service=_FakeService(),
        )

        self.assertEqual(result, [{"id": "1"}])
        self.assertIn("'folder-123' in parents", captured["q"])
        self.assertIn("trashed = false", captured["q"])
        self.assertIn("name contains 'J+ブラウザ版_ポイント売上_'", captured["q"])
        self.assertIn(f"mimeType = '{drive_io.DRIVE_XLSX_MIME_TYPE}'", captured["q"])
        self.assertEqual(captured["orderBy"], "createdTime desc")
        self.assertEqual(captured["pageSize"], 20)
        self.assertTrue(captured["supportsAllDrives"])
        self.assertTrue(captured["includeItemsFromAllDrives"])

    def test_limit_is_clamped(self):
        captured = {}

        class _FakeList:
            def execute(self):
                return {"files": []}

        class _FakeFiles:
            def list(self, **kwargs):
                captured.update(kwargs)
                return _FakeList()

        class _FakeService:
            def files(self):
                return _FakeFiles()

        drive_io.list_drive_files(folder_id="f", limit=9999, service=_FakeService())
        self.assertEqual(captured["pageSize"], 100)

        drive_io.list_drive_files(folder_id="f", limit=0, service=_FakeService())
        self.assertEqual(captured["pageSize"], 1)

    def test_single_quotes_in_name_filter_are_escaped(self):
        captured = {}

        class _FakeList:
            def execute(self):
                return {"files": []}

        class _FakeFiles:
            def list(self, **kwargs):
                captured.update(kwargs)
                return _FakeList()

        class _FakeService:
            def files(self):
                return _FakeFiles()

        drive_io.list_drive_files(folder_id="f", name_contains="it's a test", service=_FakeService())
        self.assertIn("name contains 'it\\'s a test'", captured["q"])


if __name__ == "__main__":
    unittest.main()
