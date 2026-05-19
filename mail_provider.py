from __future__ import annotations

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
    ) -> None:
        super().__init__(message)
        self.safe_reason = safe_reason
        self.retryable = retryable


class MailProvider(ABC):
    @abstractmethod
    def send(self, request: MailDeliveryRequest) -> MailDeliveryResult:
        raise NotImplementedError


class LoggingMailProvider(MailProvider):
    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def send(self, request: MailDeliveryRequest) -> MailDeliveryResult:
        self._logger.warning(
            "ICE_REPORT_MAIL_PROVIDER type=logging to=%s subject=%s metadata=%s",
            ",".join(request.to_emails),
            request.subject,
            request.metadata,
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
            raise MailDeliveryError(
                f"ses send_email failed: {exc}",
                safe_reason="mail_provider_failed",
                retryable=True,
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
