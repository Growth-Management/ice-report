import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "delete-firestore-retention-records.py"
)


def load_script_module():
    spec = importlib.util.spec_from_file_location(
        "delete_firestore_retention_records",
        SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeSnapshot:
    def __init__(self, exists):
        self.exists = exists


class _FakeDocument:
    def __init__(self, collection, document_id, existing):
        self.collection = collection
        self.document_id = document_id
        self._existing = existing

    def get(self):
        return _FakeSnapshot(self._existing)


class _FakeCollection:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def document(self, document_id):
        existing = (self.name, document_id) in self.client.existing
        return _FakeDocument(self.name, document_id, existing)


class _FakeBatch:
    def __init__(self, client):
        self.client = client
        self.pending = []

    def delete(self, document_ref):
        self.pending.append((document_ref.collection, document_ref.document_id))

    def commit(self):
        self.client.deleted.extend(self.pending)
        self.pending = []


class _FakeFirestoreClient:
    def __init__(self, project=None, existing=None):
        self.project = project
        self.existing = existing or {("download_logs", "log-1")}
        self.deleted = []

    def collection(self, name):
        return _FakeCollection(self, name)

    def batch(self):
        return _FakeBatch(self)


class _FakeFirestoreModule:
    def __init__(self):
        self.latest_client = None

    def Client(self, project=None):
        self.latest_client = _FakeFirestoreClient(project=project)
        return self.latest_client


class FirestoreRetentionDeletionTest(unittest.TestCase):
    def setUp(self):
        self.module = load_script_module()

    def _manifest_file(self, payload):
        temp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        with temp:
            json.dump(payload, temp)
        return temp.name

    def _base_manifest(self, records=None):
        return {
            "approvalId": "RET-001",
            "approvedBy": "篠原邦昭",
            "approvedAt": "2026-06-22T00:00:00+09:00",
            "reason": "Retention period satisfied and approved.",
            "records": records
            or [
                {
                    "collection": "download_logs",
                    "id": "log-1",
                    "reason": "created_at older than retention threshold",
                }
            ],
        }

    def _args(self, manifest_path, **overrides):
        values = {
            "project": "ice-sh",
            "manifest": manifest_path,
            "execute": False,
            "confirm": "",
            "allow_deliveries": False,
            "allow_missing": False,
            "max_records": 50,
            "batch_size": 50,
        }
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def _firestore_patch(self):
        fake_firestore = _FakeFirestoreModule()
        google_module = types.ModuleType("google")
        cloud_module = types.ModuleType("google.cloud")
        cloud_module.firestore = fake_firestore
        return fake_firestore, patch.dict(
            sys.modules,
            {
                "google": google_module,
                "google.cloud": cloud_module,
                "google.cloud.firestore": fake_firestore,
            },
        )

    def test_validate_manifest_rejects_deliveries_without_explicit_flag(self):
        manifest = self._base_manifest(
            [
                {
                    "collection": "deliveries",
                    "id": "delivery-1",
                    "reason": "inactive beyond retention threshold",
                }
            ]
        )

        with self.assertRaises(SystemExit) as ctx:
            self.module.validate_manifest(
                manifest,
                allow_deliveries=False,
                max_records=50,
            )

        self.assertIn("--allow-deliveries", str(ctx.exception))

    def test_execute_requires_confirmation_phrase(self):
        manifest_path = self._manifest_file(self._base_manifest())

        with self.assertRaises(SystemExit) as ctx:
            self.module.build_result(
                self._args(manifest_path, execute=True, confirm="wrong")
            )

        self.assertIn(self.module.CONFIRMATION_PHRASE, str(ctx.exception))

    def test_dry_run_inspects_targets_without_deleting(self):
        manifest_path = self._manifest_file(self._base_manifest())
        fake_firestore, firestore_patch = self._firestore_patch()

        with firestore_patch:
            result = self.module.build_result(self._args(manifest_path))

        self.assertTrue(result["dryRun"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["recordCount"], 1)
        self.assertEqual(result["existingCount"], 1)
        self.assertEqual(result["deletedCount"], 0)
        self.assertEqual(fake_firestore.latest_client.deleted, [])
        self.assertEqual(result["targets"][0]["collection"], "download_logs")
        self.assertEqual(result["targets"][0]["id"], "log-1")

    def test_execute_deletes_existing_targets_only(self):
        manifest_path = self._manifest_file(
            self._base_manifest(
                [
                    {
                        "collection": "download_logs",
                        "id": "log-1",
                        "reason": "created_at older than retention threshold",
                    },
                    {
                        "collection": "download_logs",
                        "id": "missing-log",
                        "reason": "already removed during prior approved run",
                    },
                ]
            )
        )
        fake_firestore, firestore_patch = self._firestore_patch()

        with firestore_patch:
            result = self.module.build_result(
                self._args(
                    manifest_path,
                    execute=True,
                    confirm=self.module.CONFIRMATION_PHRASE,
                    allow_missing=True,
                )
            )

        self.assertTrue(result["executed"])
        self.assertEqual(result["recordCount"], 2)
        self.assertEqual(result["existingCount"], 1)
        self.assertEqual(result["missingCount"], 1)
        self.assertEqual(result["deletedCount"], 1)
        self.assertEqual(fake_firestore.latest_client.deleted, [("download_logs", "log-1")])


if __name__ == "__main__":
    unittest.main()
