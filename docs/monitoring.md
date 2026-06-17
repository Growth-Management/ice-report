# Monitoring

ICE Report Generator の監視設定メモです。Cloud Run / GCP 側の監視に加えて、SES reputation はAWS側のCloudWatch alarmでwarning検知します。

## SES Bounce / Complaint Reputation Alarms

設定日: 2026-06-18 JST

### 方針

- SES bounce / complaint はAWS側のCloudWatch alarmでwarning検知する。
- Cloud RunにSES callback endpointは追加しない。
- SES configuration set event publishingは初期採用しない。
- Notion / Slack / docsには raw recipient email、provider event payload、message body、credential、PIN、tokenを記録しない。
- docs-only更新のため、Cloud Run deployは不要。

### AWS対象

| 項目 | 値 |
| --- | --- |
| AWS account | `855532282119` |
| Region | `ap-northeast-1` |
| SES identity | `ice-sv.jp` |
| Sender | `report-noreply@ice-sv.jp` |
| Custom MAIL FROM | `bounce.ice-sv.jp` |

### SNS通知先

| 項目 | 値 |
| --- | --- |
| SNS topic | `arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts` |
| Subscription endpoint | `info-ice-gm@impress.co.jp` |
| Subscription ARN | `arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts:4888397b-8a62-452a-8fd9-05b45ad37d6f` |
| Owner | ICE GM department mailing list |

### CloudWatch alarms

| Alarm | Metric | Threshold | Period | EvaluationPeriods | TreatMissingData | Action |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `ice-report-ses-bounce-rate-warning` | `AWS/SES` `Reputation.BounceRate` | `0.02` | `300` | `1` | `notBreaching` | SNS topic |
| `ice-report-ses-complaint-rate-warning` | `AWS/SES` `Reputation.ComplaintRate` | `0.001` | `300` | `1` | `notBreaching` | SNS topic |

Alarm ARNs:

- `arn:aws:cloudwatch:ap-northeast-1:855532282119:alarm:ice-report-ses-bounce-rate-warning`
- `arn:aws:cloudwatch:ap-northeast-1:855532282119:alarm:ice-report-ses-complaint-rate-warning`

### Verification

2026-06-18 JST Cloud Shellで確認済み。

- SES identity `ice-sv.jp` は `VerifiedForSendingStatus=true`、`VerificationStatus=SUCCESS`。
- DKIM status は `SUCCESS`。
- Custom MAIL FROM `bounce.ice-sv.jp` は `MailFromDomainStatus=SUCCESS`。
- CloudWatch `AWS/SES` metrics に `Reputation.BounceRate` と `Reputation.ComplaintRate` が存在。
- 作成前のCloudWatch alarmは0件。
- SNS subscriptionは部署メーリングリストでconfirmed済み。
- 作成後の両alarmは `INSUFFICIENT_DATA`。これはmetric dataが不足している初期状態で、`TreatMissingData=notBreaching` により欠損データはbreaching扱いにしない。

### 確認コマンド

```bash
export AWS_PAGER=""
export AWS_REGION="ap-northeast-1"
export SNS_TOPIC_ARN="arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts"
export BOUNCE_ALARM_NAME="ice-report-ses-bounce-rate-warning"
export COMPLAINT_ALARM_NAME="ice-report-ses-complaint-rate-warning"

aws cloudwatch describe-alarms \
  --region "$AWS_REGION" \
  --alarm-names "$BOUNCE_ALARM_NAME" "$COMPLAINT_ALARM_NAME" \
  --query 'MetricAlarms[].{Name:AlarmName,Arn:AlarmArn,State:StateValue,Metric:MetricName,Threshold:Threshold,Period:Period,EvaluationPeriods:EvaluationPeriods,TreatMissingData:TreatMissingData,Actions:AlarmActions}' \
  --output table

aws sns list-subscriptions-by-topic \
  --topic-arn "$SNS_TOPIC_ARN" \
  --region "$AWS_REGION" \
  --query 'Subscriptions[].{Protocol:Protocol,Endpoint:Endpoint,Status:SubscriptionArn}' \
  --output table
```
