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

現時点の GCP notification channel は email 1本です。AWS SES bounce /
complaint warning は AWS SNS topic `ice-report-ses-reputation-alerts` から
ICE GM department mailing list へ通知します。

判断:

- GCP critical alert は現状の email channel を維持する
- AWS SES reputation warning は既存 SNS topic / mailing list を維持する
- GCP warning channel は、GCP側でwarning alertを追加する段階まで新設しない
- Slack webhook、Google Chat、PagerDuty などの追加 channel は、運用担当者と
  一次対応SLAが決まってから採用する

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

### Noise review

GCP側のalert関連ログ件数は read-only helper で確認します。この script は
Cloud Logging を読むだけで、notification channel、alert policy、threshold を
変更しません。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-monitoring-noise.ps1
```

JSONで保存する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-monitoring-noise.ps1 -AsJson |
  Set-Content -Encoding UTF8 artifacts\monitoring-noise-review.json
```

定期 read-only operational check でも同じ確認を実行し、次の artifact を
`artifacts/operations-readonly/` へ保存します。

- `monitoring-noise-review-<timestamp>.json`
- `monitoring-noise-review-<timestamp>-summary.txt`

`operations-readonly-run-metadata-<timestamp>.json` には
`monitoringCriticalSignalsTotal`、`monitoringWarningSignalsTotal`、
`monitoringThresholdChangeRecommended`、`monitoringChannelSplitRecommended`
を記録します。

確認頻度:

- 定期: 月1回
- alert発火後: incident close後
- deploy後: runtime error alert が発火した場合

Notionへ転記する項目:

- 確認日時、確認者、対象期間
- signal別件数
- incident化した件数
- 利用者影響の有無
- 直近deploy、設定変更、手動操作との関係
- threshold変更要否と理由

転記しない項目:

- PIN、token、生メールアドレス
- message body、provider event JSON
- secret値、credential、長期クラウド認証情報
- raw log payload全体

### Threshold decision

threshold は件数だけで変更しません。次を満たす場合にだけ変更候補にします。

- 同一policyが2週間で3回以上発火している
- いずれも利用者影響または運用対応を必要としない
- deploy、手動smoke、意図した誤key確認など既知作業と対応付けられる
- threshold変更後も本当のOTP送信停止、SES認証失敗、runtime errorを検知できる

現時点の判断:

- `/api-health`、OTP delivery failure、SES auth failure、runtime errors は
  criticalのまま維持する
- critical threshold は 5分窓で1件以上を維持する
- admin auth failure は warning観測対象に留め、alert化しない
- SES bounce / complaint はAWS側warningとして維持し、GCP側alertへ重複実装しない
- 週次 read-only check の monitoring noise review で複数回連続した
  非incident signal が確認されるまで、GCP warning channel は新設しない

threshold を変更する場合は、変更前に Notion へ次を記録します。

- policy名、現在のthreshold、変更案
- 直近の発火履歴とincident化有無
- 変更理由
- 変更後の検知漏れリスク
- rollback条件

## SES Bounce / Complaint

### Operating decision

Use AWS-side monitoring for SES bounce and complaint events. Do not add a
public Cloud Run endpoint to receive SES callbacks.

Decision:

- Primary path: SES identity or configuration set notifications to Amazon SNS,
  or SES event publishing to an AWS-native destination.
- Warning path: CloudWatch reputation or event metrics, if the AWS operations
  side adopts CloudWatch alarms.
- Out of scope for Cloud Run: inbound bounce / complaint webhook endpoint.
- Out of scope for Notion/Slack records: raw recipient email, message payload,
  MIME content, or provider event JSON with personal data.

Required AWS-side information before implementation:

- SES region: `ap-northeast-1`
- SES account: `855532282119`
- Sending identity: `ice-sv.jp`
- Custom MAIL FROM: `bounce.ice-sv.jp`
- Configuration set name, if event publishing is used
- SNS topic ARN or CloudWatch alarm names
- Notification destination and owner
- Whether email feedback forwarding remains enabled or is replaced by SNS /
  event publishing

Recommended first implementation:

1. Confirm whether SES identity-level feedback notifications or configuration
   set event publishing will be the source of truth.
2. If using SNS, create or select a topic for bounce / complaint events and
   subscribe the operational notification destination.
3. If using event publishing, ensure the SES configuration set is applied by
   `AWS_SES_CONFIGURATION_SET` and is present on all OTP send requests.
4. If using CloudWatch alarms, keep alarm thresholds as warning-level signals
   until live volume and noise are known.
5. Record event counts only. Use recipient hashes or aggregate counts when
   transferring details to Notion or chat tools.
6. Add the selected topic/alarm names to this document after AWS-side setup.

### Current AWS setup

Configured on 2026-06-18 JST.

| Item | Value |
| --- | --- |
| AWS account | `855532282119` |
| Region | `ap-northeast-1` |
| SES identity | `ice-sv.jp` |
| Sender | `report-noreply@ice-sv.jp` |
| Custom MAIL FROM | `bounce.ice-sv.jp` |

SNS notification:

| Item | Value |
| --- | --- |
| SNS topic | `arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts` |
| Subscription endpoint | `info-ice-gm@impress.co.jp` |
| Subscription status | Confirmed |
| Owner | ICE GM department mailing list |

CloudWatch alarms:

| Alarm | Metric | Threshold | Period | Evaluation periods | Treat missing data | Action |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `ice-report-ses-bounce-rate-warning` | `AWS/SES` `Reputation.BounceRate` | `0.02` | `300` | `1` | `notBreaching` | SNS topic |
| `ice-report-ses-complaint-rate-warning` | `AWS/SES` `Reputation.ComplaintRate` | `0.001` | `300` | `1` | `notBreaching` | SNS topic |

Alarm ARNs:

- `arn:aws:cloudwatch:ap-northeast-1:855532282119:alarm:ice-report-ses-bounce-rate-warning`
- `arn:aws:cloudwatch:ap-northeast-1:855532282119:alarm:ice-report-ses-complaint-rate-warning`

Verification:

- SES identity `ice-sv.jp` is verified for sending.
- DKIM status is `SUCCESS`.
- Custom MAIL FROM `bounce.ice-sv.jp` status is `SUCCESS`.
- CloudWatch `AWS/SES` metrics include `Reputation.BounceRate` and
  `Reputation.ComplaintRate`.
- SNS subscription is confirmed.
- Both alarms initially entered `INSUFFICIENT_DATA`. Because
  `TreatMissingData=notBreaching`, missing initial data is not treated as a
  breach.

AWS read-only check commands:

```powershell
aws cloudwatch describe-alarms `
  --profile ice-report-ops `
  --region ap-northeast-1 `
  --alarm-names ice-report-ses-bounce-rate-warning ice-report-ses-complaint-rate-warning

aws sns list-subscriptions-by-topic `
  --profile ice-report-ops `
  --region ap-northeast-1 `
  --topic-arn arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts
```

Official references:

- AWS SES event notifications:
  https://docs.aws.amazon.com/ses/latest/dg/monitor-sending-activity-using-notifications.html
- AWS SES sender reputation monitoring:
  https://docs.aws.amazon.com/ses/latest/dg/monitor-sender-reputation.html

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
