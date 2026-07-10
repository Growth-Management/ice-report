import importlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def _install_google_stubs():
    if "flask" not in sys.modules:
        flask_stub = types.ModuleType("flask")
        flask_stub.Request = object
        sys.modules["flask"] = flask_stub

    google_stub = sys.modules.get("google") or types.ModuleType("google")
    google_auth_stub = types.ModuleType("google.auth")
    google_auth_transport_stub = types.ModuleType("google.auth.transport")
    google_auth_transport_requests_stub = types.ModuleType("google.auth.transport.requests")
    google_cloud_stub = types.ModuleType("google.cloud")

    google_auth_stub.default = lambda: (types.SimpleNamespace(refresh=lambda request: None, token="token"), None)
    google_auth_transport_requests_stub.Request = object
    google_cloud_stub.bigquery = types.SimpleNamespace(Client=object, QueryJobConfig=object)
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


def _load_create_report_module():
    _install_google_stubs()
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = lambda *args, **kwargs: []
    pandas_stub.NA = object()
    pandas_stub.isna = lambda value: False
    pandas_stub.notna = lambda value: True
    pandas_stub.api = types.SimpleNamespace(
        types=types.SimpleNamespace(
            is_datetime64_any_dtype=lambda value: False,
        )
    )
    sys.modules["pandas"] = pandas_stub

    openpyxl_stub = types.ModuleType("openpyxl")
    openpyxl_stub.load_workbook = lambda *args, **kwargs: None
    worksheet_package_stub = types.ModuleType("openpyxl.worksheet")
    worksheet_stub = types.ModuleType("openpyxl.worksheet.worksheet")
    worksheet_stub.Worksheet = object
    utils_stub = types.ModuleType("openpyxl.utils")
    utils_stub.get_column_letter = lambda col: str(col)
    cell_stub = types.ModuleType("openpyxl.utils.cell")
    cell_stub.range_boundaries = lambda ref: (1, 1, 1, 1)
    sys.modules["openpyxl"] = openpyxl_stub
    sys.modules["openpyxl.worksheet"] = worksheet_package_stub
    sys.modules["openpyxl.worksheet.worksheet"] = worksheet_stub
    sys.modules["openpyxl.utils"] = utils_stub
    sys.modules["openpyxl.utils.cell"] = cell_stub

    sys.modules.pop("create_report", None)
    return importlib.import_module("create_report")


def _install_app_import_stubs():
    flask_stub = sys.modules.get("flask") or types.ModuleType("flask")
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
    create_report_stub.preview_default_query_mapping = lambda *args, **kwargs: {}
    sys.modules.setdefault("create_report", create_report_stub)

    distribution_stub = sys.modules.get("distribution") or types.ModuleType("distribution")
    for name in (
        "add_delivery_version",
        "archive_report_definition",
        "create_delivery_record",
        "create_report_definition",
        "download_report_definition_template",
        "find_delivery_by_token",
        "get_current_version",
        "get_report_definition",
        "get_report_definition_storage_allowlist",
        "list_delivery_records",
        "list_download_log_records",
        "list_report_definitions",
        "log_download",
        "make_signed_download_url",
        "preview_report_definition_schedule_run",
        "publish_report_definition_query_mapping",
        "publish_report_definition_template",
        "render_download_form",
        "rollback_report_definition_version",
        "rollback_report_definition_template",
        "run_report_definition_schedule_runs",
        "set_delivery_active",
        "set_report_definition_schedule",
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
                        "template_gcs_uri": "gs://bucket/report-templates/monthly/v2/template.xlsx",
                        "template_sha256": "a" * 64,
                        "template_sheets": [{"name": "Sheet1", "max_row": 10}],
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
            "template_gcs_uri",
            "template_sha256",
            "template_sheets",
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
                "drive_folder_name": "OMFダウンロード数報告",
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

    def test_report_definition_storage_allowlist_uses_safe_defaults(self):
        distribution = _load_distribution_module()

        with patch.dict("os.environ", {}, clear=True):
            allowlist = distribution.get_report_definition_storage_allowlist()

        self.assertIn("reports/plus/", allowlist["gcs_prefixes"])
        self.assertIn("OMFダウンロード数報告", allowlist["drive_folders"])

    def test_report_definition_payload_accepts_allowed_storage_destinations(self):
        distribution = _load_distribution_module()

        with patch.dict(
            "os.environ",
            {
                "REPORT_ALLOWED_GCS_PREFIXES": "gs://ice-report-files/reports/plus/,reports/plus/",
                "REPORT_ALLOWED_DRIVE_FOLDERS": "OMFダウンロード数報告,126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt",
            },
            clear=True,
        ):
            payload = distribution._report_definition_payload(
                {
                    "name": "Monthly downloads",
                    "gcs_prefix": "reports/plus/2606/",
                    "drive_folder_name": "OMFダウンロード数報告",
                    "signed_url": "https://example.test/signed",
                    "raw_email": "user@example.com",
                }
            )

        self.assertEqual(payload["gcs_prefix"], "reports/plus/2606/")
        self.assertEqual(payload["drive_folder_name"], "OMFダウンロード数報告")
        self.assertNotIn("signed_url", payload)
        self.assertNotIn("raw_email", payload)

    def test_report_definition_payload_rejects_unlisted_storage_destinations(self):
        distribution = _load_distribution_module()

        with patch.dict(
            "os.environ",
            {
                "REPORT_ALLOWED_GCS_PREFIXES": "reports/plus/",
                "REPORT_ALLOWED_DRIVE_FOLDERS": "OMFダウンロード数報告",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "gcs_prefix is not allowed"):
                distribution._report_definition_payload(
                    {"name": "Monthly downloads", "gcs_prefix": "reports/private/"}
                )
            with self.assertRaisesRegex(ValueError, "drive_folder_name is not allowed"):
                distribution._report_definition_payload(
                    {"name": "Monthly downloads", "drive_folder_name": "Unlisted folder"}
                )

    def test_report_definition_payload_rejects_parent_directory_storage_prefix(self):
        distribution = _load_distribution_module()

        with self.assertRaisesRegex(ValueError, "gcs_prefix is not allowed"):
            distribution._report_definition_payload(
                {"name": "Monthly downloads", "gcs_prefix": "reports/plus/../private/"}
            )

    def test_public_report_definition_includes_safe_schedule_metadata(self):
        distribution = _load_distribution_module()

        item = distribution._public_report_definition(
            "monthly-downloads",
            {
                "schedule": {
                    "enabled": True,
                    "frequency": "monthly",
                    "day_of_month": 5,
                    "time_of_day": "10:30",
                    "timezone": "Asia/Tokyo",
                    "created_by_email": "user@example.com",
                    "signed_url": "https://example.test/signed",
                    "query_sql": "select raw_email from table",
                }
            },
        )

        self.assertTrue(item["schedule_enabled"])
        self.assertEqual(
            item["schedule"],
            {
                "enabled": True,
                "frequency": "monthly",
                "day_of_month": 5,
                "time_of_day": "10:30",
                "timezone": "Asia/Tokyo",
            },
        )
        self.assertNotIn("created_by_email", str(item))
        self.assertNotIn("signed_url", str(item))
        self.assertNotIn("raw_email", str(item))

    def test_report_definition_schedule_payload_is_limited_to_safe_monthly_metadata(self):
        distribution = _load_distribution_module()

        payload = distribution._report_definition_schedule_payload(
            {
                "enabled": "true",
                "frequency": "monthly",
                "day_of_month": "28",
                "time_of_day": "23:59",
                "timezone": "Asia/Tokyo",
                "query_sql": "select secret_value from table",
            }
        )

        self.assertEqual(
            payload,
            {
                "enabled": True,
                "frequency": "monthly",
                "day_of_month": 28,
                "time_of_day": "23:59",
                "timezone": "Asia/Tokyo",
            },
        )
        self.assertNotIn("query_sql", payload)

        with self.assertRaisesRegex(ValueError, "between 1 and 28"):
            distribution._report_definition_schedule_payload({"day_of_month": 31})
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            distribution._report_definition_schedule_payload({"time_of_day": "9:00"})
        with self.assertRaisesRegex(ValueError, "not allowed"):
            distribution._report_definition_schedule_payload({"timezone": "UTC"})

    def test_set_report_definition_schedule_updates_only_safe_schedule_fields(self):
        distribution = _load_distribution_module()
        doc_data = {
            "name": "Monthly downloads",
            "versions": [{"version": 1}],
            "current_version": 1,
        }
        updates = []

        class _Snapshot:
            exists = True

            def to_dict(self):
                return dict(doc_data)

        class _Document:
            def get(self):
                return _Snapshot()

            def update(self, update_doc):
                updates.append(update_doc)
                doc_data.update(update_doc)

        class _Collection:
            def document(self, report_id):
                self.report_id = report_id
                return _Document()

        class _Client:
            def collection(self, name):
                self.collection_name = name
                return _Collection()

        distribution.get_firestore_client = lambda: _Client()

        result = distribution.set_report_definition_schedule(
            "monthly-downloads",
            {
                "enabled": True,
                "day_of_month": 12,
                "time_of_day": "08:15",
                "timezone": "Asia/Tokyo",
                "raw_email": "user@example.com",
            },
        )

        self.assertTrue(result["schedule_enabled"])
        self.assertEqual(result["schedule"]["day_of_month"], 12)
        self.assertEqual(updates[0]["schedule"]["time_of_day"], "08:15")
        self.assertTrue(updates[0]["schedule_enabled"])
        self.assertNotIn("raw_email", str(updates[0]))

    def test_schedule_preview_returns_safe_due_candidates_without_generation(self):
        distribution = _load_distribution_module()
        docs = [
            (
                "due-report",
                {
                    "name": "Due report",
                    "status": "active",
                    "current_version": 2,
                    "versions": [{"version": 1}, {"version": 2}],
                    "schedule": {
                        "enabled": True,
                        "frequency": "monthly",
                        "day_of_month": 6,
                        "time_of_day": "09:00",
                        "timezone": "Asia/Tokyo",
                        "query_sql": "select raw_email from table",
                    },
                    "template_gcs_uri": "gs://bucket/template.xlsx",
                    "allowed_emails": ["user@example.com"],
                },
            ),
            (
                "future-report",
                {
                    "name": "Future report",
                    "schedule": {
                        "enabled": True,
                        "frequency": "monthly",
                        "day_of_month": 7,
                        "time_of_day": "09:00",
                        "timezone": "Asia/Tokyo",
                    },
                },
            ),
            (
                "disabled-report",
                {
                    "name": "Disabled report",
                    "schedule": {
                        "enabled": False,
                        "frequency": "monthly",
                        "day_of_month": 6,
                        "time_of_day": "09:00",
                        "timezone": "Asia/Tokyo",
                    },
                },
            ),
            (
                "archived-report",
                {
                    "name": "Archived report",
                    "status": "archived",
                    "schedule": {
                        "enabled": True,
                        "frequency": "monthly",
                        "day_of_month": 6,
                        "time_of_day": "09:00",
                        "timezone": "Asia/Tokyo",
                    },
                },
            ),
        ]

        class _Snapshot:
            def __init__(self, doc_id, data):
                self.id = doc_id
                self._data = data

            def to_dict(self):
                return dict(self._data)

        class _Query:
            def limit(self, limit):
                self.limit_value = limit
                return self

            def stream(self):
                return [_Snapshot(doc_id, data) for doc_id, data in docs]

        class _Client:
            def collection(self, name):
                self.collection_name = name
                return _Query()

        distribution.get_firestore_client = lambda: _Client()

        preview = distribution.preview_report_definition_schedule_run(
            now=datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc),
        )

        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["counts"]["checked"], 3)
        self.assertEqual(preview["counts"]["scheduled"], 2)
        self.assertEqual(preview["counts"]["due"], 1)
        self.assertEqual(preview["due_items"][0]["report_id"], "due-report")
        self.assertEqual(preview["due_items"][0]["reason"], "due")
        self.assertEqual(preview["due_items"][0]["local_date"], "2026-07-06")

        future = next(item for item in preview["items"] if item["report_id"] == "future-report")
        self.assertEqual(future["reason"], "day_mismatch")
        disabled = next(item for item in preview["items"] if item["report_id"] == "disabled-report")
        self.assertEqual(disabled["reason"], "disabled")

        self.assertNotIn("archived-report", str(preview))
        self.assertNotIn("raw_email", str(preview))
        self.assertNotIn("allowed_emails", str(preview))
        self.assertNotIn("template_gcs_uri", str(preview))
        self.assertNotIn("query_sql", str(preview))

    def test_schedule_runs_dry_run_defaults_to_safe_guard_validation(self):
        distribution = _load_distribution_module()
        docs = [
            (
                "due-report",
                {
                    "name": "Due report",
                    "status": "active",
                    "current_version": 2,
                    "gcs_prefix": "reports/plus/",
                    "drive_folder_name": "126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt",
                    "versions": [
                        {"version": 1},
                        {
                            "version": 2,
                            "template_gcs_uri": "gs://bucket/template.xlsx",
                            "query_config_id": "plus-monthly-default-v1",
                            "mapping_version_id": "plus-monthly-table-mapping-v1",
                        },
                    ],
                    "schedule": {
                        "enabled": True,
                        "frequency": "monthly",
                        "day_of_month": 6,
                        "time_of_day": "09:00",
                        "timezone": "Asia/Tokyo",
                    },
                    "allowed_emails": ["user@example.com"],
                    "query_sql": "select raw_email from table",
                },
            ),
        ]
        run_docs = {}

        class _Snapshot:
            def __init__(self, doc_id, data):
                self.id = doc_id
                self._data = data

            def to_dict(self):
                return dict(self._data)

        class _ReportQuery:
            def limit(self, limit):
                return self

            def stream(self):
                return [_Snapshot(doc_id, data) for doc_id, data in docs]

        class _RunCollection:
            def document(self, doc_id):
                raise AssertionError("dry-run must not create scheduled run records")

        class _Client:
            def collection(self, name):
                if name == distribution.FIRESTORE_COLLECTION_REPORT_DEFINITIONS:
                    return _ReportQuery()
                return _RunCollection()

        distribution.get_firestore_client = lambda: _Client()

        result = distribution.run_report_definition_schedule_runs(
            now=datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc),
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["counts"]["checked"], 1)
        self.assertEqual(result["counts"]["due"], 1)
        self.assertEqual(result["counts"]["eligible"], 1)
        self.assertEqual(result["items"][0]["action"], "would_execute")
        self.assertEqual(run_docs, {})
        self.assertNotIn("raw_email", str(result))
        self.assertNotIn("allowed_emails", str(result))
        self.assertNotIn("template_gcs_uri", str(result))
        self.assertNotIn("query_sql", str(result))

    def test_schedule_runs_execute_requires_confirmation_and_idempotency_key(self):
        distribution = _load_distribution_module()

        with self.assertRaisesRegex(ValueError, "execute confirmation is required"):
            distribution.run_report_definition_schedule_runs({"mode": "execute"})

        with self.assertRaisesRegex(ValueError, "idempotency_key is required"):
            distribution.run_report_definition_schedule_runs(
                {"mode": "execute", "confirm": "RUN_DUE_REPORTS"}
            )

        with self.assertRaisesRegex(ValueError, "idempotency_key is invalid"):
            distribution.run_report_definition_schedule_runs(
                {
                    "mode": "execute",
                    "confirm": "RUN_DUE_REPORTS",
                    "idempotency_key": "bad key",
                }
            )

        with self.assertRaisesRegex(ValueError, "generation confirmation is required"):
            distribution.run_report_definition_schedule_runs(
                {
                    "mode": "execute",
                    "confirm": "RUN_DUE_REPORTS",
                    "idempotency_key": "manual-2026-07-06-0900",
                    "execute_step": "generate",
                }
            )

    def test_schedule_runs_execute_records_hashed_idempotency_and_rejects_duplicate(self):
        distribution = _load_distribution_module()
        docs = [
            (
                "due-report",
                {
                    "name": "Due report",
                    "status": "active",
                    "current_version": 2,
                    "gcs_prefix": "reports/plus/",
                    "drive_folder_name": "126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt",
                    "versions": [
                        {"version": 1},
                        {
                            "version": 2,
                            "template_gcs_uri": "gs://bucket/template.xlsx",
                            "query_config_id": "plus-monthly-default-v1",
                            "mapping_version_id": "plus-monthly-table-mapping-v1",
                        },
                    ],
                    "schedule": {
                        "enabled": True,
                        "frequency": "monthly",
                        "day_of_month": 6,
                        "time_of_day": "09:00",
                        "timezone": "Asia/Tokyo",
                    },
                },
            ),
        ]
        run_docs = {}

        class _Snapshot:
            def __init__(self, doc_id, data, exists=True):
                self.id = doc_id
                self._data = data
                self.exists = exists

            def to_dict(self):
                return dict(self._data)

        class _ReportQuery:
            def limit(self, limit):
                return self

            def stream(self):
                return [_Snapshot(doc_id, data) for doc_id, data in docs]

        class _RunRef:
            def __init__(self, doc_id):
                self.doc_id = doc_id

            def get(self):
                return _Snapshot(self.doc_id, run_docs.get(self.doc_id, {}), self.doc_id in run_docs)

            def set(self, data):
                run_docs[self.doc_id] = dict(data)

        class _RunCollection:
            def document(self, doc_id):
                return _RunRef(doc_id)

        class _Client:
            def collection(self, name):
                if name == distribution.FIRESTORE_COLLECTION_REPORT_DEFINITIONS:
                    return _ReportQuery()
                if name == distribution.FIRESTORE_COLLECTION_SCHEDULED_REPORT_RUNS:
                    return _RunCollection()
                raise AssertionError(name)

        distribution.get_firestore_client = lambda: _Client()
        payload = {
            "mode": "execute",
            "confirm": "RUN_DUE_REPORTS",
            "idempotency_key": "manual-2026-07-06-0900",
        }

        first = distribution.run_report_definition_schedule_runs(
            payload,
            now=datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc),
        )
        self.assertFalse(first["dry_run"])
        self.assertEqual(first["counts"]["validated"], 1)
        self.assertEqual(first["items"][0]["action"], "execute_guard_validated")
        self.assertEqual(len(run_docs), 1)
        run_doc = next(iter(run_docs.values()))
        self.assertEqual(run_doc["status"], "validated")
        self.assertEqual(run_doc["result_code"], "execute_guard_validated")
        self.assertNotIn("manual-2026-07-06-0900", str(run_doc))

        second = distribution.run_report_definition_schedule_runs(
            payload,
            now=datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(second["counts"]["duplicates"], 1)
        self.assertEqual(second["counts"]["validated"], 0)
        self.assertEqual(second["items"][0]["action"], "duplicate")
        self.assertNotIn("manual-2026-07-06-0900", str(second))

    def test_schedule_runs_generate_step_requires_extra_guard_and_returns_safe_result(self):
        distribution = _load_distribution_module()
        docs = [
            (
                "due-report",
                {
                    "name": "Due report",
                    "status": "active",
                    "current_version": 2,
                    "gcs_prefix": "reports/plus/",
                    "versions": [
                        {"version": 1},
                        {
                            "version": 2,
                            "template_gcs_uri": "gs://bucket/template.xlsx",
                            "query_config_id": "plus-monthly-default-v1",
                            "mapping_version_id": "plus-monthly-table-mapping-v1",
                        },
                    ],
                    "schedule": {
                        "enabled": True,
                        "frequency": "monthly",
                        "day_of_month": 6,
                        "time_of_day": "09:00",
                        "timezone": "Asia/Tokyo",
                    },
                    "query_sql": "select raw_email from table",
                },
            ),
        ]
        run_docs = {}
        executor_contexts = []

        class _Snapshot:
            def __init__(self, doc_id, data, exists=True):
                self.id = doc_id
                self._data = data
                self.exists = exists

            def to_dict(self):
                return dict(self._data)

        class _ReportQuery:
            def limit(self, limit):
                return self

            def stream(self):
                return [_Snapshot(doc_id, data) for doc_id, data in docs]

        class _RunRef:
            def __init__(self, doc_id):
                self.doc_id = doc_id

            def get(self):
                return _Snapshot(self.doc_id, run_docs.get(self.doc_id, {}), self.doc_id in run_docs)

            def set(self, data):
                run_docs[self.doc_id] = dict(data)

            def update(self, data):
                run_docs[self.doc_id].update(dict(data))

        class _RunCollection:
            def document(self, doc_id):
                return _RunRef(doc_id)

        class _Client:
            def collection(self, name):
                if name == distribution.FIRESTORE_COLLECTION_REPORT_DEFINITIONS:
                    return _ReportQuery()
                if name == distribution.FIRESTORE_COLLECTION_SCHEDULED_REPORT_RUNS:
                    return _RunCollection()
                raise AssertionError(name)

        def _executor(context):
            executor_contexts.append(dict(context))
            return {
                "status": "generated",
                "report_month": "2026-06",
                "output_file": "report.xlsx",
                "has_gcs_object": True,
                "gcs_uri": "gs://bucket/reports/plus/report.xlsx",
                "local_path": "/tmp/report.xlsx",
            }

        distribution.get_firestore_client = lambda: _Client()
        result = distribution.run_report_definition_schedule_runs(
            {
                "mode": "execute",
                "execute_step": "generate",
                "confirm": "RUN_DUE_REPORTS",
                "confirm_generation": "GENERATE_REPORTS",
                "idempotency_key": "manual-2026-07-06-0900",
            },
            now=datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc),
            executor=_executor,
        )

        self.assertEqual(result["execute_step"], "generate")
        self.assertEqual(result["counts"]["generated"], 1)
        self.assertEqual(result["items"][0]["action"], "generated")
        self.assertEqual(result["items"][0]["generation"]["report_month"], "2026-06")
        self.assertEqual(executor_contexts[0]["report_id"], "due-report")
        self.assertEqual(executor_contexts[0]["local_date"], "2026-07-06")
        run_doc = next(iter(run_docs.values()))
        self.assertEqual(run_doc["status"], "succeeded")
        self.assertEqual(run_doc["result_code"], "generation_succeeded")
        self.assertEqual(run_doc["generation"]["has_gcs_object"], True)
        self.assertNotIn("raw_email", str(result))
        self.assertNotIn("query_sql", str(result))
        self.assertNotIn("template_gcs_uri", str(result))
        self.assertNotIn("manual-2026-07-06-0900", str(result))
        self.assertNotIn("gs://bucket", str(result))
        self.assertNotIn("/tmp/report.xlsx", str(result))

    def test_report_definition_id_pattern_accepts_slug(self):
        distribution = _load_distribution_module()

        self.assertEqual(
            distribution._validate_report_id("plus-monthly_downloads.v1"),
            "plus-monthly_downloads.v1",
        )

    def test_template_object_name_uses_controlled_prefix(self):
        distribution = _load_distribution_module()

        self.assertEqual(
            distribution._template_object_name("/report-templates/", "plus", 3, r"C:\fake\template.xlsx"),
            "report-templates/plus/v3/template.xlsx",
        )

    def test_public_template_version_result_excludes_storage_and_sheet_details(self):
        distribution = _load_distribution_module()
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)

        version = distribution._build_template_version_doc(
            version=4,
            preview={
                "file_name": "template.xlsx",
                "size_bytes": 123,
                "sha256": "b" * 64,
                "sheet_count": 2,
                "sheets": [{"name": "Sheet1", "max_row": 99}],
            },
            gcs_uri="gs://bucket/report-templates/plus/v4/template.xlsx",
            note="publish",
            now=now,
        )
        result = distribution._public_template_version_result("plus", version)

        self.assertEqual(result["version"], 4)
        self.assertEqual(result["template_name"], "template.xlsx")
        self.assertEqual(result["template_size_bytes"], 123)
        self.assertEqual(result["template_sha256"], "b" * 64)
        self.assertNotIn("template_gcs_uri", result)
        self.assertNotIn("template_sheets", result)

    def test_publish_query_mapping_copies_template_metadata_without_exposing_details(self):
        distribution = _load_distribution_module()
        doc_data = {
            "name": "Monthly downloads",
            "versions": [
                {
                    "version": 1,
                    "status": "published",
                    "template_name": "template.xlsx",
                    "template_gcs_uri": "gs://bucket/report-templates/monthly/v1/template.xlsx",
                    "template_sha256": "c" * 64,
                    "template_sheets": [{"name": "Sheet1", "max_row": 10}],
                    "query_sql": "select raw_email from table",
                    "template_mapping": {"A1": "raw_email"},
                }
            ],
            "current_version": 1,
        }
        updates = []

        class _Snapshot:
            exists = True

            def to_dict(self):
                return dict(doc_data)

        class _Document:
            def get(self):
                return _Snapshot()

            def update(self, update_doc):
                updates.append(update_doc)
                doc_data.update(update_doc)

        class _Collection:
            def document(self, report_id):
                self.report_id = report_id
                return _Document()

        class _Client:
            def collection(self, name):
                self.collection_name = name
                return _Collection()

        distribution.get_firestore_client = lambda: _Client()

        result = distribution.publish_report_definition_query_mapping(
            "monthly-downloads",
            note="publish query mapping",
        )

        version_doc = updates[0]["versions"][-1]
        self.assertEqual(version_doc["version"], 2)
        self.assertEqual(version_doc["query_config_id"], "plus-monthly-default-v1")
        self.assertEqual(version_doc["mapping_version_id"], "plus-monthly-table-mapping-v1")
        self.assertEqual(
            version_doc["template_gcs_uri"],
            "gs://bucket/report-templates/monthly/v1/template.xlsx",
        )
        self.assertNotIn("query_sql", version_doc)
        self.assertNotIn("template_mapping", version_doc)

        query_mapping = result["query_mapping"]
        self.assertEqual(query_mapping["version"], 2)
        self.assertEqual(query_mapping["query_config_id"], "plus-monthly-default-v1")
        self.assertNotIn("template_gcs_uri", query_mapping)
        self.assertNotIn("template_sheets", query_mapping)
        self.assertNotIn("raw_email", str(result["item"]))

    def test_publish_query_mapping_rejects_unlisted_ids(self):
        distribution = _load_distribution_module()

        with patch.dict(
            "os.environ",
            {
                "REPORT_ALLOWED_QUERY_CONFIG_IDS": "plus-monthly-default-v1",
                "REPORT_ALLOWED_MAPPING_VERSION_IDS": "plus-monthly-table-mapping-v1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "query_config_id is not allowed"):
                distribution.publish_report_definition_query_mapping(
                    "monthly-downloads",
                    query_config_id="ad-hoc-query",
                )

    def test_rollback_report_definition_version_updates_only_current_version(self):
        distribution = _load_distribution_module()
        doc_data = {
            "name": "Monthly downloads",
            "versions": [
                {
                    "version": 1,
                    "status": "published",
                    "template_gcs_uri": "gs://bucket/template-v1.xlsx",
                    "query_sql": "select raw_email from table",
                    "template_mapping": {"A1": "raw_email"},
                    "signed_url": "https://example.test/signed",
                },
                {
                    "version": 2,
                    "status": "published",
                    "query_config_id": "plus-monthly-default-v1",
                    "mapping_version_id": "plus-monthly-table-mapping-v1",
                },
            ],
            "current_version": 2,
        }
        updates = []

        class _Snapshot:
            exists = True

            def to_dict(self):
                return dict(doc_data)

        class _Document:
            def get(self):
                return _Snapshot()

            def update(self, update_doc):
                updates.append(update_doc)
                doc_data.update(update_doc)

        class _Collection:
            def document(self, report_id):
                self.report_id = report_id
                return _Document()

        class _Client:
            def collection(self, name):
                self.collection_name = name
                return _Collection()

        distribution.get_firestore_client = lambda: _Client()

        result = distribution.rollback_report_definition_version("monthly-downloads", 1)

        self.assertEqual(updates[0]["current_version"], 1)
        self.assertNotIn("versions", updates[0])
        self.assertEqual(result["current_version"], 1)
        self.assertTrue(result["versions"][1]["current"])
        self.assertNotIn("query_sql", str(result))
        self.assertNotIn("template_mapping", str(result))
        self.assertNotIn("signed_url", str(result))
        self.assertNotIn("template_gcs_uri", str(result))

        with self.assertRaisesRegex(ValueError, "version not found"):
            distribution.rollback_report_definition_version("monthly-downloads", 99)


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


class QueryMappingPreviewTest(unittest.TestCase):
    def test_default_query_mapping_preview_excludes_sql_text(self):
        create_report = _load_create_report_module()
        queries = []

        class _QueryJobConfig:
            def __init__(self, dry_run=False, use_query_cache=True):
                self.dry_run = dry_run
                self.use_query_cache = use_query_cache

        class _Job:
            total_bytes_processed = 12345

        class _Client:
            def __init__(self, project):
                self.project = project

            def query(self, sql, job_config=None):
                queries.append({"sql": sql, "job_config": job_config})
                return _Job()

        create_report.bigquery = types.SimpleNamespace(
            Client=_Client,
            QueryJobConfig=_QueryJobConfig,
        )

        result = create_report.preview_default_query_mapping("ice-sh")

        self.assertEqual(result["query_config_id"], "plus-monthly-default-v1")
        self.assertEqual(result["mapping_version_id"], "plus-monthly-table-mapping-v1")
        self.assertEqual(result["query_count"], 2)
        self.assertEqual(result["mapping_source_count"], 2)
        self.assertEqual([item["sql_file"] for item in result["queries"]], ["paid.sql", "free.sql"])
        self.assertIn("mapping_preview", result)
        self.assertTrue(all(call["job_config"].dry_run for call in queries))
        self.assertNotIn("SELECT", str(result).upper())
        self.assertNotIn("FROM", str(result).upper())
        self.assertNotIn("template_gcs_uri", str(result))
        self.assertNotIn("template_mapping", str(result))
        self.assertNotIn("raw_email", str(result))


class RuntimeTemplateResolutionTest(unittest.TestCase):
    def _install_firestore_doc(self, distribution, doc_data):
        class _Snapshot:
            exists = True

            def to_dict(self):
                return dict(doc_data)

        class _Document:
            def get(self):
                return _Snapshot()

        class _Collection:
            def document(self, report_id):
                self.report_id = report_id
                return _Document()

        class _Client:
            def collection(self, name):
                self.collection_name = name
                return _Collection()

        distribution.get_firestore_client = lambda: _Client()

    def test_runtime_template_resolution_uses_current_version_internally(self):
        distribution = _load_distribution_module()
        self._install_firestore_doc(
            distribution,
            {
                "status": "active",
                "current_version": 2,
                "versions": [
                    {"version": 1, "template_name": "old.xlsx"},
                    {
                        "version": 2,
                        "template_name": "template.xlsx",
                        "template_gcs_uri": "gs://template-bucket/report-templates/plus/v2/template.xlsx",
                        "template_sha256": "c" * 64,
                        "template_size_bytes": 456,
                    },
                ],
            },
        )

        result = distribution.get_report_definition_runtime_template("plus")

        self.assertEqual(result["report_id"], "plus")
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["bucket"], "template-bucket")
        self.assertEqual(result["object_name"], "report-templates/plus/v2/template.xlsx")
        self.assertEqual(result["template_gcs_uri"], "gs://template-bucket/report-templates/plus/v2/template.xlsx")

        public = distribution._public_runtime_template_result(result)
        self.assertEqual(public["report_definition_version"], 2)
        self.assertEqual(public["template_name"], "template.xlsx")
        self.assertEqual(public["template_sha256"], "c" * 64)
        self.assertNotIn("template_gcs_uri", public)
        self.assertNotIn("bucket", public)
        self.assertNotIn("object_name", public)

    def test_runtime_template_resolution_requires_published_template(self):
        distribution = _load_distribution_module()
        self._install_firestore_doc(
            distribution,
            {
                "status": "active",
                "current_version": 1,
                "versions": [{"version": 1, "status": "draft"}],
            },
        )

        with self.assertRaisesRegex(ValueError, "published template is required"):
            distribution.get_report_definition_runtime_template("plus")

    def test_download_runtime_template_returns_local_path_and_safe_metadata(self):
        distribution = _load_distribution_module()
        self._install_firestore_doc(
            distribution,
            {
                "status": "active",
                "current_version": 3,
                "versions": [
                    {
                        "version": 3,
                        "template_name": "template.xlsx",
                        "template_gcs_uri": "gs://template-bucket/report-templates/plus/v3/template.xlsx",
                        "template_sha256": "d" * 64,
                    }
                ],
            },
        )

        class _Blob:
            def __init__(self, object_name):
                self.object_name = object_name

            def download_to_filename(self, filename):
                Path(filename).write_bytes(b"template")

        class _Bucket:
            def __init__(self, bucket_name):
                self.bucket_name = bucket_name

            def blob(self, object_name):
                self.object_name = object_name
                return _Blob(object_name)

        class _StorageClient:
            def bucket(self, bucket_name):
                self.bucket_name = bucket_name
                return _Bucket(bucket_name)

        distribution.get_storage_client = lambda: _StorageClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = distribution.download_report_definition_template(
                "plus",
                destination_dir=temp_dir,
            )

            self.assertTrue(Path(result["local_path"]).exists())
            self.assertEqual(Path(result["local_path"]).read_bytes(), b"template")
            self.assertEqual(result["template"]["report_id"], "plus")
            self.assertEqual(result["template"]["report_definition_version"], 3)
            self.assertNotIn("template_gcs_uri", result["template"])


if __name__ == "__main__":
    unittest.main()
