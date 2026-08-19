import importlib
import sys
import types
import unittest
from unittest.mock import patch


def _install_stubs():
    if "flask" not in sys.modules:
        flask_stub = types.ModuleType("flask")
        flask_stub.Request = object
        sys.modules["flask"] = flask_stub

    if "google.cloud" not in sys.modules:
        google_stub = sys.modules.get("google") or types.ModuleType("google")
        google_auth_stub = types.ModuleType("google.auth")
        google_auth_transport_stub = types.ModuleType("google.auth.transport")
        google_auth_transport_requests_stub = types.ModuleType("google.auth.transport.requests")
        google_cloud_stub = types.ModuleType("google.cloud")

        google_auth_stub.default = lambda: (
            types.SimpleNamespace(refresh=lambda request: None, token="token"),
            None,
        )
        google_auth_transport_requests_stub.Request = object
        google_cloud_stub.firestore = types.SimpleNamespace(
            Client=object, Query=types.SimpleNamespace(DESCENDING="DESC")
        )
        google_cloud_stub.secretmanager = types.SimpleNamespace()
        google_cloud_stub.storage = types.SimpleNamespace(Client=object)

        google_stub.auth = google_auth_stub
        google_stub.cloud = google_cloud_stub

        sys.modules["google"] = google_stub
        sys.modules["google.auth"] = google_auth_stub
        sys.modules["google.auth.transport"] = google_auth_transport_stub
        sys.modules["google.auth.transport.requests"] = google_auth_transport_requests_stub
        sys.modules["google.cloud"] = google_cloud_stub


def _load_distribution():
    _install_stubs()
    sys.modules.pop("distribution", None)
    return importlib.import_module("distribution")


class SlackNotificationTest(unittest.TestCase):
    def setUp(self):
        self.distribution = _load_distribution()

    def _post_payload(self, mock_post):
        _, kwargs = mock_post.call_args
        return kwargs["json"]

    def test_notify_slack_event_uses_given_color_and_active_label(self):
        with patch.object(
            self.distribution, "get_slack_webhook_url", return_value="https://example.invalid/webhook"
        ), patch.object(self.distribution.requests, "post") as mock_post:
            self.distribution.notify_slack_event(
                "ICEレポート配布URLが作成されました",
                {
                    "delivery_id": "abc123",
                    "customer_name": "テスト顧客",
                    "report_month": "2026-08",
                    "version": 1,
                    "file_name": "x.xlsx",
                    "active": True,
                    "timestamp": "2026-08-19T00:00:00Z",
                },
                color=self.distribution.SLACK_COLOR_GOOD,
            )

        mock_post.assert_called_once()
        payload = self._post_payload(mock_post)
        self.assertNotIn("<!channel>", payload["text"])
        attachment = payload["attachments"][0]
        self.assertEqual(attachment["color"], self.distribution.SLACK_COLOR_GOOD)
        fields = {f["title"]: f["value"] for f in attachment["fields"]}
        self.assertEqual(fields["状態"], "active")
        self.assertEqual(fields["delivery_id"], "abc123")

    def test_notify_slack_event_disabled_state_label(self):
        with patch.object(
            self.distribution, "get_slack_webhook_url", return_value="https://example.invalid/webhook"
        ), patch.object(self.distribution.requests, "post") as mock_post:
            self.distribution.notify_slack_event(
                "ICEレポート配布URLの状態が変更されました",
                {"delivery_id": "abc123", "active": False},
                color=self.distribution.SLACK_COLOR_WARN,
            )

        payload = self._post_payload(mock_post)
        attachment = payload["attachments"][0]
        self.assertEqual(attachment["color"], self.distribution.SLACK_COLOR_WARN)
        fields = {f["title"]: f["value"] for f in attachment["fields"]}
        self.assertEqual(fields["状態"], "disabled")

    def test_notify_slack_error_uses_danger_color_and_channel_alert(self):
        with patch.object(
            self.distribution, "get_slack_webhook_url", return_value="https://example.invalid/webhook"
        ), patch.object(self.distribution.requests, "post") as mock_post:
            self.distribution.notify_slack_error(
                "ICEレポート生成に失敗しました",
                {"delivery_id": "abc123", "reason": "BadRequest"},
            )

        mock_post.assert_called_once()
        payload = self._post_payload(mock_post)
        self.assertTrue(payload["text"].startswith("<!channel> "))
        attachment = payload["attachments"][0]
        self.assertEqual(attachment["color"], self.distribution.SLACK_COLOR_DANGER)
        fields = {f["title"]: f["value"] for f in attachment["fields"]}
        self.assertEqual(fields["reason"], "BadRequest")
        self.assertEqual(fields["delivery_id"], "abc123")

    def test_notify_slack_noop_without_webhook_url(self):
        with patch.object(
            self.distribution, "get_slack_webhook_url", return_value=None
        ), patch.object(self.distribution.requests, "post") as mock_post:
            self.distribution.notify_slack("title", [("a", "b")])

        mock_post.assert_not_called()

    def test_notify_slack_swallows_request_exceptions(self):
        with patch.object(
            self.distribution, "get_slack_webhook_url", return_value="https://example.invalid/webhook"
        ), patch.object(self.distribution.requests, "post", side_effect=RuntimeError("boom")):
            self.distribution.notify_slack("title", [("a", "b")])


if __name__ == "__main__":
    unittest.main()
