from __future__ import annotations

from mail_provider import MailDeliveryRequest


def build_otp_pin_mail(
    *,
    from_email: str,
    to_email: str,
    pin: str,
    ttl_minutes: int,
    service_name: str = "ICE Report Generator",
    reply_to_emails: list[str] | None = None,
    metadata: dict[str, str] | None = None,
) -> MailDeliveryRequest:
    subject = f"[{service_name}] ダウンロード認証PINのお知らせ"
    text_body = (
        f"{service_name} のダウンロード認証PINをお送りします。\n\n"
        f"PIN: {pin}\n"
        f"有効期限: {ttl_minutes}分\n\n"
        "このPINに心当たりがない場合は破棄してください。"
    )
    html_body = f"""
<html>
  <body>
    <p>{service_name} のダウンロード認証PINをお送りします。</p>
    <p><strong>PIN: {pin}</strong></p>
    <p>有効期限: {ttl_minutes}分</p>
    <p>このPINに心当たりがない場合は破棄してください。</p>
  </body>
</html>
""".strip()

    return MailDeliveryRequest(
        from_email=from_email,
        to_emails=[to_email],
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        reply_to_emails=list(reply_to_emails or []),
        metadata=dict(metadata or {}),
    )
