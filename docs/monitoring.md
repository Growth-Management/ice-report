# Monitoring

ICE Report Generator の Cloud Logging / Cloud Monitoring 設定です。障害時の一次対応は `docs/operations.md`、セキュリティ方針は `docs/security.md` を参照します。

## 現在の構成

- GCP project: `ice-sh`
- Cloud Run service: `report-generator`
- Notification channel: `ICE Report Generator alerts - Shinohara`
- Notification channel resource: `projects/ice-sh/notificationChannels/9771605581263899912`
- Notification channel type: `email`

## Log-Based Metrics

次の user-defined log-based metrics を作成済みです。

| Metric | 用途 |
| --- | --- |
| `logging.googleapis.com/user/ice_report_mail_delivery_failure_count` | `ICE_REPORT_MAIL_DELIVERY_ATTEMPT result=failure` を検知 |
| `logging.googleapis.com/user/ice_report_otp_delivery_failed_count` | `ICE_REPORT_SECURITY_EVENT type=otp_delivery_failed` を検知 |
| `logging.googleapis.com/user/ice_report_mail_auth_failed_count` | `mail_provider_auth_failed` または `safe_reason=mail_provider_auth_failed` を検知 |
| `logging.googleapis.com/user/ice_report_runtime_error_count` | Cloud Run `report-generator` の `severity>=ERROR` を検知 |

### Filters

```text
resource.type="cloud_run_revision"
resource.labels.service_name="report-generator"
textPayload:"ICE_REPORT_MAIL_DELIVERY_ATTEMPT"
textPayload:"result=failure"
```

```text
resource.type="cloud_run_revision"
resource.labels.service_name="report-generator"
textPayload:"ICE_REPORT_SECURITY_EVENT type=otp_delivery_failed"
```

```text
resource.type="cloud_run_revision"
resource.labels.service_name="report-generator"
(textPayload:"mail_provider_auth_failed" OR textPayload:"safe_reason=mail_provider_auth_failed")
```

```text
resource.type="cloud_run_revision"
resource.labels.service_name="report-generator"
severity>=ERROR
```

## Alert Policies

次の alert policies を作成済みです。いずれも enabled です。

| Policy | Resource | 条件 | 通知 |
| --- | --- | --- | --- |
| `ICE Report Generator - OTP delivery failure` | `projects/ice-sh/alertPolicies/9748171527687951983` | mail delivery failure または OTP delivery failed が 5分窓で 1件以上 | email |
| `ICE Report Generator - SES auth failure` | `projects/ice-sh/alertPolicies/9748171527687953827` | `mail_provider_auth_failed` が 5分窓で 1件以上 | email |
| `ICE Report Generator - runtime errors` | `projects/ice-sh/alertPolicies/9748171527687953440` | Cloud Run ERROR log が 5分窓で 1件以上 | email |

## 確認コマンド

ローカルの `gcloud alpha monitoring` が未導入でも確認できるよう、REST API で確認します。

```powershell
$token = (& gcloud.cmd auth print-access-token).Trim()
$headers = @{ Authorization = "Bearer $token" }

$metrics = Invoke-RestMethod `
  -Uri 'https://logging.googleapis.com/v2/projects/ice-sh/metrics' `
  -Headers $headers `
  -Method Get

$channels = Invoke-RestMethod `
  -Uri 'https://monitoring.googleapis.com/v3/projects/ice-sh/notificationChannels' `
  -Headers $headers `
  -Method Get

$policies = Invoke-RestMethod `
  -Uri 'https://monitoring.googleapis.com/v3/projects/ice-sh/alertPolicies' `
  -Headers $headers `
  -Method Get

[pscustomobject]@{
  metrics = @($metrics.metrics | Where-Object { $_.name -like 'ice_report_*' } | Select-Object name,filter)
  channels = @($channels.notificationChannels | Where-Object { $_.displayName -like 'ICE Report Generator alerts*' } | Select-Object name,displayName,type,enabled,verificationStatus)
  policies = @($policies.alertPolicies | Where-Object { $_.displayName -like 'ICE Report Generator -*' } | ForEach-Object {
    [pscustomobject]@{
      displayName = $_.displayName
      name = $_.name
      enabled = $_.enabled
      channelCount = if ($_.notificationChannels) { @($_.notificationChannels).Count } else { 0 }
      conditionCount = @($_.conditions).Count
    }
  })
}
```

## 運用メモ

- Alert が発火したら `docs/operations.md` の OTP 送信停止時の一次対応に沿って確認する
- Cloud Logging には生 PIN、生メールアドレス、生 token を出さない
- 調査時は `token_hash`、`email_hash`、`recipient_hash`、`delivery_id` を使う
- notification channel を変更する場合は、3つの alert policy すべてに新 channel を接続する
- email channel の verification が必要な場合は、受信メールの案内に従って確認する

## 今後の改善候補

- `/api-health` の uptime check を追加する
- alert thresholds を実運用のノイズ量に合わせて調整する
- warning と critical の通知先を分ける
- SES bounce / complaint の定期確認を CloudWatch / SES 側監視と連携する
