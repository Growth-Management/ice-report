from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class MailDeliveryRequest:
    from_email: str
    to_emails: list[str]
    subject: str
    text_body: str
    html_body: str = ""
    reply_to_emails: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class MailDeliveryResult:
    provider: str
    accepted_recipients: list[str]
    rejected_recipients: list[str] = field(default_factory=list)
    provider_message_id: str = ""
    requested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class MailDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        safe_reason: str = "mail_provider_failed",
        retryable: bool = False,
        provider_error_code: str = "",
    ) -> None:
        super().__init__(message)
        self.safe_reason = safe_reason
        self.retryable = retryable
        self.provider_error_code = provider_error_code


class MailProvider(ABC):
    @abstractmethod
    def send(self, request: MailDeliveryRequest) -> MailDeliveryResult:
        raise NotImplementedError


class LoggingMailProvider(MailProvider):
    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def send(self, request: MailDeliveryRequest) -> MailDeliveryResult:
        self._logger.warning(
            "ICE_REPORT_MAIL_PROVIDER type=logging recipient_hashes=%s subject=%s metadata=%s",
            ",".join(_fingerprint(email) for email in request.to_emails),
            request.subject,
            _safe_metadata(request.metadata),
        )
        return MailDeliveryResult(
            provider="logging",
            accepted_recipients=list(request.to_emails),
        )


class NoopMailProvider(MailProvider):
    def send(self, request: MailDeliveryRequest) -> MailDeliveryResult:
        return MailDeliveryResult(
            provider="noop",
            accepted_recipients=[],
            rejected_recipients=list(request.to_emails),
        )


class SesMailProvider(MailProvider):
    def __init__(
        self,
        *,
        client: Any,
        source_email: str,
        configuration_set_name: str = "",
    ) -> None:
        self._client = client
        self._source_email = source_email
        self._configuration_set_name = configuration_set_name

    def send(self, request: MailDeliveryRequest) -> MailDeliveryResult:
        if not request.to_emails:
            raise MailDeliveryError(
                "recipient is required",
                safe_reason="mail_provider_invalid_request",
            )

        message = {
            "Subject": {"Charset": "UTF-8", "Data": request.subject},
            "Body": {
                "Text": {"Charset": "UTF-8", "Data": request.text_body},
            },
        }

        if request.html_body:
            message["Body"]["Html"] = {
                "Charset": "UTF-8",
                "Data": request.html_body,
            }

        params: dict[str, Any] = {
            "Source": request.from_email or self._source_email,
            "Destination": {"ToAddresses": request.to_emails},
            "Message": message,
        }

        if request.reply_to_emails:
            params["ReplyToAddresses"] = request.reply_to_emails

        if self._configuration_set_name:
            params["ConfigurationSetName"] = self._configuration_set_name

        if request.metadata:
            params["Tags"] = [
                {"Name": key[:256], "Value": value[:256]}
                for key, value in request.metadata.items()
            ]

        try:
            response = self._client.send_email(**params)
        except Exception as exc:
            safe_reason, retryable, provider_error_code = _classify_ses_exception(exc)
            raise MailDeliveryError(
                "ses send_email failed",
                safe_reason=safe_reason,
                retryable=retryable,
                provider_error_code=provider_error_code,
            ) from exc

        return MailDeliveryResult(
            provider="ses",
            accepted_recipients=list(request.to_emails),
            provider_message_id=response.get("MessageId", ""),
        )


def build_mail_provider(
    *,
    provider_name: str,
    ses_client: Any | None = None,
    source_email: str = "",
    configuration_set_name: str = "",
) -> MailProvider:
    normalized = (provider_name or "logging").strip().lower()

    if normalized == "logging":
        return LoggingMailProvider()

    if normalized == "noop":
        return NoopMailProvider()

    if normalized == "ses":
        if ses_client is None:
            raise MailDeliveryError(
                "ses client is required",
                safe_reason="mail_provider_not_configured",
            )

        if not source_email:
            raise MailDeliveryError(
                "source email is required",
                safe_reason="mail_provider_not_configured",
            )

        return SesMailProvider(
            client=ses_client,
            source_email=source_email,
            configuration_set_name=configuration_set_name,
        )

    raise MailDeliveryError(
        f"unsupported provider: {provider_name}",
        safe_reason="mail_provider_not_configured",
    )


def _classify_ses_exception(exc: Exception) -> tuple[str, bool, str]:
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    code = str(error.get("Code") or exc.__class__.__name__ or "unknown")
    normalized = code.lower()

    transient_markers = (
        "throttl",
        "timeout",
        "requesttimeout",
        "serviceunavailable",
        "internal",
        "temporar",
        "too many",
        "connection",
        "endpoint",
    )
    rejected_markers = (
        "messageRejected",
        "mailfromdomainnotverified",
        "configuration",
        "invalid",
        "notverified",
        "accessdenied",
        "accountpaused",
        "sendingpaused",
    )

    if any(marker.lower() in normalized for marker in transient_markers):
        return "mail_provider_transient", True, code

    if any(marker.lower() in normalized for marker in rejected_markers):
        return "mail_provider_rejected", False, code

    return "mail_provider_failed", True, code


def _fingerprint(value: str) -> str:
    if not value:
        return ""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _safe_metadata(metadata: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in metadata.items():
        normalized_key = key.lower()
        if "token" in normalized_key or "email" in normalized_key:
            safe[key] = _fingerprint(value)
        else:
            safe[key] = value

    return safe
