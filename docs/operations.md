# Operations Playbook

ICE Report Generator の本番運用手順です。SES reputation warningの一次対応をここに記録します。

## SES bounce / complaint warning response

SES bounce / complaint はAWS側のCloudWatch alarmでwarning検知します。Cloud RunにはSES callback endpointを追加しません。

### Monitoring resources

| 項目 | 値 |
| --- | --- |
| SNS topic | `arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts` |
| Notification owner | ICE GM department mailing list |
| Notification endpoint | `info-ice-gm@impress.co.jp` |
| Bounce alarm | `ice-report-ses-bounce-rate-warning` |
| Complaint alarm | `ice-report-ses-complaint-rate-warning` |

### Initial triage

1. CloudWatch alarm名を確認する。
2. event種別がbounce warningかcomplaint warningかを確認する。
3. SES account / identity状態を確認する。
4. 同時間帯にOTP delivery failure、SES auth failure、Cloud Run runtime errorが増えていないか確認する。
5. Notion / Slack / docsにはaggregate count、alarm名、time window、statusだけを記録する。

記録しないもの:

- raw recipient email
- provider event payload
- message body
- MIME content
- credential
- PIN
- token

### AWS checks

```bash
export AWS_PAGER=""
export AWS_REGION="ap-northeast-1"
export SES_IDENTITY="ice-sv.jp"
export SNS_TOPIC_ARN="arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts"
export BOUNCE_ALARM_NAME="ice-report-ses-bounce-rate-warning"
export COMPLAINT_ALARM_NAME="ice-report-ses-complaint-rate-warning"

aws sts get-caller-identity --region "$AWS_REGION"
aws sesv2 get-account --region "$AWS_REGION"
aws sesv2 get-email-identity --email-identity "$SES_IDENTITY" --region "$AWS_REGION"

aws cloudwatch describe-alarms \
  --region "$AWS_REGION" \
  --alarm-names "$BOUNCE_ALARM_NAME" "$COMPLAINT_ALARM_NAME" \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Metric:MetricName,Threshold:Threshold,Actions:AlarmActions}' \
  --output table

aws sns list-subscriptions-by-topic \
  --topic-arn "$SNS_TOPIC_ARN" \
  --region "$AWS_REGION" \
  --query 'Subscriptions[].{Protocol:Protocol,Endpoint:Endpoint,Status:SubscriptionArn}' \
  --output table
```

### Severity decision

warningとして扱う条件:

- CloudWatch reputation alarmのみが発火している。
- `/api-health` は正常。
- SES account statusがhealthy相当で、sendingが有効。
- OTP delivery failureや`mail_provider_auth_failed`が急増していない。

criticalへ引き上げる条件:

- OTP delivery failureが増加している。
- SES sendingが停止または制限されている。
- SES accountがreview / pause相当の状態になっている。
- 複数利用者からOTP未達が報告されている。

### Containment

1. 影響が特定deliveryに偏る場合は、対象deliveryの停止または宛先範囲の見直しを検討する。
2. 送信リトライや手動再送がcomplaint riskを高めている場合は一時停止する。
3. OTP / PIN / one-time download session / rate limitは緩めない。
4. Cloud Run runtimeの変更は、OTP delivery incidentへ発展した場合だけ検討する。

### Recording

Notionへ記録する項目:

- alarm名
- detected time window
- alarm state
- SES account / identity status
- aggregate countまたはmetric value
- affected delivery_idが判明している場合はdelivery_id
- containment action
- rollback or provider changeの要否
