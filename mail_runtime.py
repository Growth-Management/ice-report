from __future__ import annotations

import os

from mail_provider import (
    MailDeliveryError,
    MailDeliveryResult,
    build_mail_provider,
)
from otp_mail import build_otp_pin_mail


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _mail_provider_name() -> str:
    return os.environ.get("MAIL_PROVIDER", "logging").strip().lower()


def _mail_from_email() -> str:
    return os.environ.get("MAIL_FROM_EMAIL", "").strip()


def _mail_reply_to_emails() -> list[str]:
    return _split_csv(os.environ.get("MAIL_REPLY_TO_EMAILS", ""))


def _mail_service_name() -> str:
    return os.environ.get("MAIL_SERVICE_NAME", "ICE Report Generator").strip()


def _ses_region_name() -> str:
    return (
        os.environ.get("MAIL_PROVIDER_SES_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ap-northeast-1"
    ).strip()


def _ses_configuration_set_name() -> str:
    return os.environ.get("MAIL_PROVIDER_SES_CONFIGURATION_SET", "").strip()


def _ses_timeout_seconds() -> int:
    raw = os.environ.get("MAIL_PROVIDER_TIMEOUT_SECONDS", "10").strip() or "10"
    try:
        value = int(raw)
    except ValueError as exc:
        raise MailDeliveryError(
            "MAIL_PROVIDER_TIMEOUT_SECONDS must be integer",
            safe_reason="mail_provider_not_configured",
        ) from exc

    return max(1, value)


def _build_ses_client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise MailDeliveryError(
            "boto3 and botocore are required for ses provider",
            safe_reason="mail_provider_not_configured",
        ) from exc

    timeout_seconds = _ses_timeout_seconds()
    config = Config(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"max_attempts": 2, "mode": "standard"},
    )
    return boto3.client(
        "ses",
        region_name=_ses_region_name(),
        config=config,
    )


def build_runtime_mail_provider():
    provider_name = _mail_provider_name()

    if provider_name == "ses":
        ses_client = _build_ses_client()
    else:
        ses_client = None

    return build_mail_provider(
        provider_name=provider_name,
        ses_client=ses_client,
        source_email=_mail_from_email(),
        configuration_set_name=_ses_configuration_set_name(),
    )


def send_otp_pin_email(
    *,
    to_email: str,
    pin: str,
    ttl_minutes: int,
    token: str,
    delivery_id: str,
) -> MailDeliveryResult:
    request = build_otp_pin_mail(
        from_email=_mail_from_email(),
        to_email=to_email,
        pin=pin,
        ttl_minutes=ttl_minutes,
        service_name=_mail_service_name(),
        reply_to_emails=_mail_reply_to_emails(),
        metadata={
            "flow": "otp_pin",
            "token": token,
            "delivery_id": delivery_id,
        },
    )

    provider = build_runtime_mail_provider()
    return provider.send(request)
