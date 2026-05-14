
---

# docs/security.md

今後かなり重要になる。

最低限:

```md
# Security

## Current

- ADMIN_API_KEY
- allowed_domains
- Signed URL
- download_logs
- Slack notifications

## Planned

- Google Login for admin
- Email OTP / PIN authentication
- admin audit logs
- archive lifecycle management

## Notes

- env.yaml must not be committed
- Signed URL credentials use IAMCredentials