# SES Cutover Checklist

Cloud Run `report-generator` で OTP / PIN メール送信を Amazon SES へ切り替えるためのチェックリストです。

## 1. 前提確認

- DNS 設定と SES ドメイン検証が完了している
- DKIM が `verified` になっている
- 送信元アドレス `AWS_SES_FROM_ADDRESS` が verified identity 配下にある
- AWS 側に `ses:SendEmail` / `ses:SendRawEmail` を持つ送信用 IAM role がある
- IAM trust policy が Google OIDC の `aud` と Cloud Run service account の `sub` に制限されている
- Cloud Run runtime service account は `ice-report-runner@ice-sh.iam.gserviceaccount.com`

## 2. Secret Manager に置く値

長期AWS access key は置かず、次の設定だけを Secret Manager または Cloud Run 設定で渡します。

- `AWS_SES_ROLE_ARN`
- `AWS_SES_WEB_IDENTITY_AUDIENCE`
- `AWS_SES_REGION`
- `AWS_SES_FROM_ADDRESS`
- `AWS_SES_FROM_NAME` (必要時)
- `AWS_SES_CONFIGURATION_SET` (必要時)

`MAIL_PROVIDER=ses` と `MAIL_SERVICE_NAME` は平文の環境変数でも構いません。

### 推奨 Secret 名

- `aws-ses-role-arn`
- `aws-ses-web-identity-audience`
- `aws-ses-region`
- `aws-ses-from-address`
- `aws-ses-from-name`
- `aws-ses-configuration-set` (optional)

### Secret 登録例

```bash
printf '%s' 'arn:aws:iam::<AWS_ACCOUNT_ID>:role/<ROLE_NAME>' \
  | gcloud secrets create aws-ses-role-arn \
      --project=ice-sh \
      --replication-policy=automatic \
      --data-file=-

printf '%s' '<AUDIENCE>' \
  | gcloud secrets create aws-ses-web-identity-audience \
      --project=ice-sh \
      --replication-policy=automatic \
      --data-file=-

printf '%s' 'ap-northeast-1' \
  | gcloud secrets create aws-ses-region \
      --project=ice-sh \
      --replication-policy=automatic \
      --data-file=-

printf '%s' 'report-noreply@ice-sv.jp' \
  | gcloud secrets create aws-ses-from-address \
      --project=ice-sh \
      --replication-policy=automatic \
      --data-file=-

printf '%s' 'ICE Report Generator' \
  | gcloud secrets create aws-ses-from-name \
      --project=ice-sh \
      --replication-policy=automatic \
      --data-file=-
```

既存 Secret に追記する場合は `gcloud secrets versions add <SECRET_NAME> --data-file=-` を使います。

## 3. Secret Access 権限

Cloud Run runtime service account に、SES 用 Secret のみ `roles/secretmanager.secretAccessor` を付与します。

```bash
for secret in \
  aws-ses-role-arn \
  aws-ses-web-identity-audience \
  aws-ses-region \
  aws-ses-from-address \
  aws-ses-from-name
  do
    gcloud secrets add-iam-policy-binding "$secret" \
      --project=ice-sh \
      --member="serviceAccount:ice-report-runner@ice-sh.iam.gserviceaccount.com" \
      --role="roles/secretmanager.secretAccessor"
  done
```

`aws-ses-configuration-set` を使う場合は同じ付与を追加します。

## 4. Cloud Run 更新例

```bash
gcloud run services update report-generator \
  --project=ice-sh \
  --region=asia-northeast1 \
  --update-env-vars=MAIL_PROVIDER=ses,MAIL_SERVICE_NAME="ICE Report Generator" \
  --update-secrets=AWS_SES_ROLE_ARN=aws-ses-role-arn:latest,AWS_SES_WEB_IDENTITY_AUDIENCE=aws-ses-web-identity-audience:latest,AWS_SES_REGION=aws-ses-region:latest,AWS_SES_FROM_ADDRESS=aws-ses-from-address:latest,AWS_SES_FROM_NAME=aws-ses-from-name:latest
```

設定を段階適用したい場合は、先に Secret だけ追加し、最後に `MAIL_PROVIDER=ses` を有効化します。

### optional 設定を含める場合

```bash
gcloud run services update report-generator \
  --project=ice-sh \
  --region=asia-northeast1 \
  --update-env-vars=MAIL_PROVIDER=ses,MAIL_SERVICE_NAME="ICE Report Generator",AWS_SES_TIMEOUT_SECONDS=10,AWS_SES_ROLE_SESSION_NAME=ice-report-ses \
  --update-secrets=AWS_SES_ROLE_ARN=aws-ses-role-arn:latest,AWS_SES_WEB_IDENTITY_AUDIENCE=aws-ses-web-identity-audience:latest,AWS_SES_REGION=aws-ses-region:latest,AWS_SES_FROM_ADDRESS=aws-ses-from-address:latest,AWS_SES_FROM_NAME=aws-ses-from-name:latest,AWS_SES_CONFIGURATION_SET=aws-ses-configuration-set:latest
```

## 5. 反映確認

```bash
gcloud run services describe report-generator \
  --project=ice-sh \
  --region=asia-northeast1 \
  --format='yaml(spec.template.spec.containers[0].env)'
```

確認ポイント:

- `MAIL_PROVIDER=ses` になっている
- `AWS_SES_ROLE_ARN` などが Secret 参照として入っている
- 新 revision が Ready になっている
- 起動失敗で revision が詰まっていない

## 6. デプロイ後の確認

- Cloud Run revision が起動失敗していない
- `MAIL_PROVIDER=ses` で起動している
- OTP リクエストで `ICE_REPORT_OTP_DELIVERY_SENT` が出る
- `ICE_REPORT_SES_STS_ASSUMED` が出る
- `otp_delivery_failed` や `mail_provider_auth_failed` が増えていない
- SES 側で bounce / complaint が異常増加していない

## 7. Smoke Test

1. テスト用 delivery を 1 件作成する
2. 許可済みメールアドレスから PIN 発行を実行する
3. メール受信、PIN 認証、Signed URL ダウンロードまで通す
4. `security_events` に `otp_delivery_sent` と `download_session_success` が残ることを確認する

### Cloud Logging で見るログ例

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND (textPayload:"ICE_REPORT_SES_STS_ASSUMED" OR textPayload:"ICE_REPORT_OTP_DELIVERY_SENT" OR textPayload:"mail_provider_auth_failed")' \
  --project=ice-sh \
  --limit=50 \
  --format='value(timestamp,textPayload)'
```

## 8. ロールバック

異常時は次の順で戻します。

1. 直前の正常 revision にトラフィックを戻す
2. もしくは `MAIL_PROVIDER=logging` に戻す
3. OTP 送信可否を再確認する
4. STS trust policy や audience 変更は原因切り分け完了まで広げない

### provider を一時的に戻す例

```bash
gcloud run services update report-generator \
  --project=ice-sh \
  --region=asia-northeast1 \
  --update-env-vars=MAIL_PROVIDER=logging
```

## 9. 切替後の削除候補

即削除せず、切替安定後に確認付きで整理します。

- `AWS_SES_ACCESS_KEY_ID`
- `AWS_SES_SECRET_ACCESS_KEY`
- access key 前提の手順書
- 旧 fallback 名を前提にした deploy メモ

## 10. 監視メモ

critical 候補:

- OTP送信停止
- Secret取得失敗
- SES 認証失敗が継続

warning 候補:

- `otp_delivery_failed` 急増
- `otp_verify_failed` 急増
- rate limit 急増
- SES bounce / complaint reputation warning

## 11. SES bounce / complaint monitoring

2026-06-18 JSTにAWS側CloudWatch alarmを設定済みです。

### Current setup

| 項目 | 値 |
| --- | --- |
| AWS account | `855532282119` |
| Region | `ap-northeast-1` |
| SES identity | `ice-sv.jp` |
| Custom MAIL FROM | `bounce.ice-sv.jp` |
| SNS topic | `arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts` |
| Notification endpoint | `info-ice-gm@impress.co.jp` |

### Alarms

| Alarm | Metric | Threshold | Period | EvaluationPeriods | TreatMissingData |
| --- | --- | ---: | ---: | ---: | --- |
| `ice-report-ses-bounce-rate-warning` | `AWS/SES` `Reputation.BounceRate` | `0.02` | `300` | `1` | `notBreaching` |
| `ice-report-ses-complaint-rate-warning` | `AWS/SES` `Reputation.ComplaintRate` | `0.001` | `300` | `1` | `notBreaching` |

Alarm ARNs:

- `arn:aws:cloudwatch:ap-northeast-1:855532282119:alarm:ice-report-ses-bounce-rate-warning`
- `arn:aws:cloudwatch:ap-northeast-1:855532282119:alarm:ice-report-ses-complaint-rate-warning`

### Notes

- Alarm作成後の初期状態は `INSUFFICIENT_DATA`。
- Missing dataは `notBreaching` として扱う。
- Cloud Run callback endpointは追加しない。
- SES configuration set event publishingは初期非採用。
- docs-only更新のためCloud Run deployは不要。
- Notion / Slack / docsへ raw recipient email、provider event payload、message body、credential、PIN、tokenを転記しない。
