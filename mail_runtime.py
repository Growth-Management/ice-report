from __future__ import annotations

import logging
import os
import random
import hashlib
import time
from email.utils import formataddr
from threading import Lock

import requests

from mail_provider import (
    MailDeliveryError,
    MailDeliveryResult,
    build_mail_provider,
)
from otp_mail import build_otp_pin_mail

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)
_SES_CLIENT_CACHE: dict[str, dict[str, object]] = {}
_SES_CLIENT_CACHE_LOCK = Lock()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue

        normalized = value.strip()
        if normalized:
            return normalized

    return default


def _mail_provider_name() -> str:
    return os.environ.get("MAIL_PROVIDER", "logging").strip().lower()


def _mail_from_address() -> str:
    return _env_first("AWS_SES_FROM_ADDRESS", "MAIL_FROM_EMAIL")


def _mail_from_name() -> str:
    return _env_first("AWS_SES_FROM_NAME", "MAIL_FROM_NAME")


def _mail_from_email() -> str:
    address = _mail_from_address()
    name = _mail_from_name()

    if name and address:
        return formataddr((name, address))

    return address


def _mail_reply_to_emails() -> list[str]:
    return _split_csv(os.environ.get("MAIL_REPLY_TO_EMAILS", ""))


def _mail_service_name() -> str:
    return os.environ.get("MAIL_SERVICE_NAME", "ICE Report Generator").strip()


def _ses_role_arn() -> str:
    return _env_first("AWS_SES_ROLE_ARN")


def _ses_web_identity_audience() -> str:
    return _env_first("AWS_SES_WEB_IDENTITY_AUDIENCE")


def _ses_region_name() -> str:
    return _env_first(
        "AWS_SES_REGION",
        "MAIL_PROVIDER_SES_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )


def _ses_configuration_set_name() -> str:
    return _env_first(
        "AWS_SES_CONFIGURATION_SET",
        "MAIL_PROVIDER_SES_CONFIGURATION_SET",
    )


def _ses_timeout_seconds() -> int:
    raw = _env_first(
        "AWS_SES_TIMEOUT_SECONDS",
        "MAIL_PROVIDER_TIMEOUT_SECONDS",
        default="10",
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise MailDeliveryError(
            "AWS_SES_TIMEOUT_SECONDS must be integer",
            safe_reason="mail_provider_not_configured",
        ) from exc

    return max(1, value)


def _ses_role_session_name() -> str:
    raw = _env_first(
        "AWS_SES_ROLE_SESSION_NAME",
        default="ice-report-ses",
    )
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+=,.@-"
    normalized = "".join(char if char in allowed else "-" for char in raw)
    normalized = normalized.strip("-") or "ice-report-ses"
    return normalized[:64]


def _ses_role_session_duration_seconds() -> int:
    raw = _env_first(
        "AWS_SES_ROLE_SESSION_DURATION_SECONDS",
        default="900",
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise MailDeliveryError(
            "AWS_SES_ROLE_SESSION_DURATION_SECONDS must be integer",
            safe_reason="mail_provider_not_configured",
        ) from exc

    return min(max(value, 900), 3600)


def _int_env(name: str, default_value: int, *, min_value: int, max_value: int) -> int:
    raw = os.environ.get(name, str(default_value)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise MailDeliveryError(
            f"{name} must be integer",
            safe_reason="mail_provider_not_configured",
        ) from exc

    return min(max(value, min_value), max_value)


def _float_env(name: str, default_value: float, *, min_value: float, max_value: float) -> float:
    raw = os.environ.get(name, str(default_value)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise MailDeliveryError(
            f"{name} must be number",
            safe_reason="mail_provider_not_configured",
        ) from exc

    return min(max(value, min_value), max_value)


def _mail_delivery_max_attempts() -> int:
    return _int_env("MAIL_DELIVERY_MAX_ATTEMPTS", 2, min_value=1, max_value=5)


def _mail_delivery_retry_base_seconds() -> float:
    return _float_env(
        "MAIL_DELIVERY_RETRY_BASE_SECONDS",
        0.5,
        min_value=0.0,
        max_value=10.0,
    )


def _fingerprint(value: str) -> str:
    if not value:
        return ""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _require_non_empty(name: str, value: str) -> str:
    if value:
        return value

    raise MailDeliveryError(
        f"{name} is required",
        safe_reason="mail_provider_not_configured",
    )


def validate_runtime_mail_configuration() -> None:
    provider_name = _mail_provider_name()
    if provider_name != "ses":
        return

    _require_non_empty("AWS_SES_ROLE_ARN", _ses_role_arn())
    _require_non_empty(
        "AWS_SES_WEB_IDENTITY_AUDIENCE",
        _ses_web_identity_audience(),
    )
    _require_non_empty("AWS_SES_FROM_ADDRESS", _mail_from_address())
    _require_non_empty("AWS_SES_REGION", _ses_region_name())
    _ses_timeout_seconds()
    _ses_role_session_name()
    _ses_role_session_duration_seconds()
    _mail_delivery_max_attempts()
    _mail_delivery_retry_base_seconds()


def _build_botocore_config():
    try:
        from botocore.config import Config
    except ImportError as exc:
        raise MailDeliveryError(
            "boto3 and botocore are required for ses provider",
            safe_reason="mail_provider_not_configured",
        ) from exc

    timeout_seconds = _ses_timeout_seconds()
    return Config(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"max_attempts": 2, "mode": "standard"},
    )


def _fetch_google_identity_token(*, audience: str, timeout_seconds: int) -> str:
    try:
        response = requests.get(
            _METADATA_IDENTITY_URL,
            headers={"Metadata-Flavor": "Google"},
            params={"audience": audience, "format": "full"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise MailDeliveryError(
            "failed to fetch Google identity token",
            safe_reason="mail_provider_auth_failed",
            retryable=True,
        ) from exc

    token = response.text.strip()
    if not token:
        raise MailDeliveryError(
            "Google identity token is empty",
            safe_reason="mail_provider_auth_failed",
            retryable=True,
        )

    return token


def _get_cached_ses_client(cache_key: str):
    with _SES_CLIENT_CACHE_LOCK:
        cached = _SES_CLIENT_CACHE.get(cache_key)
        if not cached:
            return None

        expires_at_epoch = float(cached.get("expires_at_epoch") or 0)
        if expires_at_epoch <= time.time() + 60:
            _SES_CLIENT_CACHE.pop(cache_key, None)
            return None

        return cached.get("client")


def _store_cached_ses_client(
    *,
    cache_key: str,
    client,
    expires_at_epoch: float,
) -> None:
    with _SES_CLIENT_CACHE_LOCK:
        _SES_CLIENT_CACHE[cache_key] = {
            "client": client,
            "expires_at_epoch": expires_at_epoch,
        }


def _build_ses_client():
    try:
        import boto3
    except ImportError as exc:
        raise MailDeliveryError(
            "boto3 is required for ses provider",
            safe_reason="mail_provider_not_configured",
        ) from exc

    validate_runtime_mail_configuration()

    region_name = _ses_region_name()
    role_arn = _ses_role_arn()
    audience = _ses_web_identity_audience()
    session_name = _ses_role_session_name()
    duration_seconds = _ses_role_session_duration_seconds()
    timeout_seconds = _ses_timeout_seconds()
    config = _build_botocore_config()
    cache_key = "|".join(
        [
            region_name,
            role_arn,
            audience,
            session_name,
            str(duration_seconds),
            str(timeout_seconds),
        ]
    )

    cached_client = _get_cached_ses_client(cache_key)
    if cached_client is not None:
        return cached_client

    web_identity_token = _fetch_google_identity_token(
        audience=audience,
        timeout_seconds=timeout_seconds,
    )

    sts_client = boto3.client("sts", region_name=region_name, config=config)

    try:
        response = sts_client.assume_role_with_web_identity(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            WebIdentityToken=web_identity_token,
            DurationSeconds=duration_seconds,
        )
    except Exception as exc:
        raise MailDeliveryError(
            f"assume_role_with_web_identity failed: {exc}",
            safe_reason="mail_provider_auth_failed",
            retryable=True,
        ) from exc

    credentials = response.get("Credentials") or {}
    access_key_id = credentials.get("AccessKeyId") or ""
    secret_access_key = credentials.get("SecretAccessKey") or ""
    session_token = credentials.get("SessionToken") or ""
    expiration = credentials.get("Expiration")

    if not access_key_id or not secret_access_key or not session_token or expiration is None:
        raise MailDeliveryError(
            "AWS STS credentials are incomplete",
            safe_reason="mail_provider_auth_failed",
            retryable=True,
        )

    ses_client = boto3.client(
        "ses",
        region_name=region_name,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token,
        config=config,
    )

    _store_cached_ses_client(
        cache_key=cache_key,
        client=ses_client,
        expires_at_epoch=expiration.timestamp(),
    )

    logging.info(
        "ICE_REPORT_SES_STS_ASSUMED region=%s role_arn=%s expires_at=%s",
        region_name,
        role_arn,
        expiration.isoformat(),
    )

    return ses_client


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
            "token_hash": _fingerprint(token),
            "delivery_id": delivery_id,
        },
    )

    max_attempts = _mail_delivery_max_attempts()
    base_delay_seconds = _mail_delivery_retry_base_seconds()
    last_error: MailDeliveryError | None = None

    for attempt in range(1, max_attempts + 1):
        provider_name = _mail_provider_name()
        try:
            provider = build_runtime_mail_provider()
            result = provider.send(request)
            logging.info(
                "ICE_REPORT_MAIL_DELIVERY_ATTEMPT result=success flow=otp_pin provider=%s attempt=%s max_attempts=%s delivery_id=%s token_hash=%s recipient_hash=%s provider_message_id=%s",
                result.provider,
                attempt,
                max_attempts,
                delivery_id,
                _fingerprint(token),
                _fingerprint(to_email),
                result.provider_message_id,
            )
            return result
        except MailDeliveryError as exc:
            last_error = exc
            should_retry = exc.retryable and attempt < max_attempts
            logging.warning(
                "ICE_REPORT_MAIL_DELIVERY_ATTEMPT result=failure flow=otp_pin provider=%s attempt=%s max_attempts=%s delivery_id=%s token_hash=%s recipient_hash=%s safe_reason=%s provider_error_code=%s retryable=%s will_retry=%s",
                provider_name,
                attempt,
                max_attempts,
                delivery_id,
                _fingerprint(token),
                _fingerprint(to_email),
                exc.safe_reason,
                exc.provider_error_code,
                exc.retryable,
                should_retry,
            )

            if not should_retry:
                raise

            delay_seconds = base_delay_seconds * (2 ** (attempt - 1))
            if delay_seconds > 0:
                time.sleep(delay_seconds + random.uniform(0, base_delay_seconds))

    if last_error:
        raise last_error

    raise MailDeliveryError(
        "mail delivery failed without error",
        safe_reason="mail_provider_failed",
    )


validate_runtime_mail_configuration()
