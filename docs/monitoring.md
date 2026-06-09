# Monitoring

ICE Report Generator の Cloud Logging / Cloud Monitoring 設定です。障害時の一次対応は `docs/operations.md`、セキュリティ方針は `docs/security.md` を参照します。

## 現在の構成

- GCP project: `ice-sh`
- Cloud Run service: `report-generator`
- Notification channel: `ICE Report Generator alerts - Shinohara`
- Notification channel resource: `projects/ice-sh/notificationChannels/9771605581263899912`
- Notification channel type: `email`
- Uptime check: `projects/ice-sh/uptimeCheckConfigs/ice-report-generator-api-health-uptime-8PiDl_nq6TM`
- Uptime target: `https://report-generator-635067190197.asia-northeast1.run.app/api-health`

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
| `ICE Report Generator - api-health uptime failure` | `projects/ice-sh/alertPolicies/17507730695585410786` | `/api-health` uptime check の 5分窓 pass fraction が 1.0 未満 | email |

### Uptime Check

`/api-health` は外形監視の対象です。

| 項目 | 値 |
| --- | --- |
| Display name | `ICE Report Generator - api-health uptime` |
| Resource | `projects/ice-sh/uptimeCheckConfigs/ice-report-generator-api-health-uptime-8PiDl_nq6TM` |
| Host | `report-generator-635067190197.asia-northeast1.run.app` |
| Path | `/api-health` |
| Protocol | HTTPS |
| Period | 5分 |
| Timeout | 10秒 |
| Regions | `ASIA_PACIFIC`, `USA_OREGON`, `EUROPE` |
| Expected status | `200` |
| Content matcher | response body contains `ok` |

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

$uptimeChecks = Invoke-RestMethod `
  -Uri 'https://monitoring.googleapis.com/v3/projects/ice-sh/uptimeCheckConfigs' `
  -Headers $headers `
  -Method Get

[pscustomobject]@{
  metrics = @($metrics.metrics | Where-Object { $_.name -like 'ice_report_*' } | Select-Object name,filter)
  channels = @($channels.notificationChannels | Where-Object { $_.displayName -like 'ICE Report Generator alerts*' } | Select-Object name,displayName,type,enabled,verificationStatus)
  uptimeChecks = @($uptimeChecks.uptimeCheckConfigs | Where-Object { $_.displayName -like 'ICE Report Generator -*' } | Select-Object name,displayName,period,timeout)
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

- OTP / SES 系 alert が発火したら `docs/operations.md` の OTP 送信停止時の一次対応に沿って確認する
- Cloud Logging には生 PIN、生メールアドレス、生 token を出さない
- 調査時は `token_hash`、`email_hash`、`recipient_hash`、`delivery_id` を使う
- notification channel を変更する場合は、4つの alert policy すべてに新 channel を接続する
- email channel の verification が必要な場合は、受信メールの案内に従って確認する
- uptime alert が発火した場合は、最新 revision、直近 deploy、Cloud Run service URL、Secret 更新、`/api-health` の直接応答を確認する

## Warning / Critical 方針

現時点では notification channel は email 1本です。warning と critical の通知先分離は、Slack webhook、Google Chat、PagerDuty などの別 channel を採用する段階で実装します。

critical:

- `/api-health` uptime failure
- OTP delivery failure
- SES auth failure
- Cloud Run runtime errors

warning候補:

- OTP verify failure の急増
- rate limit の急増
- SES bounce / complaint
- admin auth failure の急増

既存 critical alert は、単発でも利用者影響または送信停止につながるため 5分窓で1件以上を維持します。実運用でノイズが出た場合は、対象 policy の発火履歴と incident 内容を確認してから threshold を変更します。

## SES Bounce / Complaint

SES bounce / complaint は採用候補ですが、現時点では ICE Report Generator 側の実装対象外です。

採用方針:

- AWS SES の event publishing または SNS notification を第一候補にする
- CloudWatch alarm または SNS -> 運用通知 channel のどちらに寄せるかは、AWS側の通知運用と権限設計を確認してから決める
- report-generator の Cloud Run runtime には bounce / complaint 受信用 endpoint を追加しない
- 受信者アドレスなどの機微情報を Slack / Notion に転記する場合は hash または件数に留める

次に実装する場合の作業単位:

- SES identity または configuration set の event destination 現状確認
- bounce / complaint 用 SNS topic または CloudWatch alarm の設計
- 通知先 channel と転記ルールの決定
- `docs/operations.md` の bounce / complaint 一次対応手順追加
