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
- `ICE_REPORT_MAIL_DELIVERY_ATTEMPT` で attempt / safe_reason / retryable / will_retry が追える
- OTP リクエストで `ICE_REPORT_OTP_DELIVERY_SENT` が出る
- `ICE_REPORT_SES_STS_ASSUMED` が出る
- `otp_delivery_failed` や `mail_provider_auth_failed` が増えていない
- Cloud Logging に生PIN、生メールアドレス、生トークンが出ていない
- SES 側で bounce / complaint が異常増加していない

### SES account / identity 状態の確認権限

Cloud Run と同じ Web Identity 経路で確認する場合は、AWS側の確認用roleに次のread権限が必要です。runtime送信roleへ付けるか、別のops確認roleへ分けるかは最小権限の方針に合わせて決めます。

- `ses:GetAccount`
- `ses:GetEmailIdentity`

ローカルからの確認補助:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-ses-web-identity.ps1
```

このスクリプトは長期AWS credentialを保存せず、GCP service account impersonation でID tokenを発行し、AWS STS `AssumeRoleWithWebIdentity` で一時credentialを取得します。

AWS IAM user `arn:aws:iam::855532282119:user/ice-report-ops` など、SES read権限を持つ通常AWS profileで確認する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-ses-direct.ps1 -Profile ice-report-ops
```

現時点の整理:

- SES read確認対象のAWS accountは `855532282119`
- `arn:aws:iam::855532282119:user/ice-report-ops` には `ses:GetAccount` / `ses:GetEmailIdentity` が付与済み
- ローカル環境には `ice-report-ops` のAWS CLI profileは未登録
- ローカルでSES確認を行う前に、`aws configure --profile ice-report-ops` などでprofile作成または認証情報設定が必要
- 以前使っていた `ice-report-ses-sender` はSES送信テストには使えたが、IAM role作成権限はない。read確認・IAM管理の主体としては扱わない
- `ice-report-ops` のAccess Keyは2本とも無効化済み。今後ローカルからdirect確認が必要な場合は、必要時のみ新しい作業用credentialを発行する

2026-05-25 確認結果:

- caller: `arn:aws:iam::855532282119:user/ice-report-ops`
- `ProductionAccessEnabled=True`
- `SendingEnabled=True`
- `EnforcementStatus=HEALTHY`
- `Max24HourSend=50000.0`
- `MaxSendRate=14.0`
- `SentLast24Hours=0.0`
- identity `ice-sv.jp` は `VerificationStatus=SUCCESS`
- `VerifiedForSendingStatus=True`
- DKIM は `SUCCESS`
- custom MAIL FROM は `bounce.ice-sv.jp`
- `MailFromDomainStatus=SUCCESS`
- `BehaviorOnMxFailure=USE_DEFAULT_VALUE`

## 7. Smoke Test

1. テスト用 delivery を 1 件作成する
2. 許可済みメールアドレスから PIN 発行を実行する
3. メール受信、PIN 認証、Signed URL ダウンロードまで通す
4. `security_events` に `otp_delivery_sent` と `download_session_success` が残ることを確認する

### Cloud Logging で見るログ例

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND (textPayload:"ICE_REPORT_SES_STS_ASSUMED" OR textPayload:"ICE_REPORT_MAIL_DELIVERY_ATTEMPT" OR textPayload:"ICE_REPORT_OTP_DELIVERY_SENT" OR textPayload:"mail_provider_auth_failed")' \
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

即削除せず、切替安定後に確認付きで整理します。棚卸し結果と確認コマンドは `docs/env-compatibility.md` を正とします。

- `AWS_SES_ACCESS_KEY_ID`
- `AWS_SES_SECRET_ACCESS_KEY`
- access key 前提の手順書
- 旧 fallback 名を前提にした deploy メモ

2026-05-26 棚卸し / 2026-06-01 削除実績:

- 本番 Cloud Run `report-generator` に旧 env 名は残っていない
- この 2 Secret は現在の本番 mail runtime では使わない
- Secret Manager の `aws-ses-access-key-id` / `aws-ses-secret-access-key` は 2026-06-01 に明示承認後削除済み

## 10. 監視メモ

critical 候補:

- OTP送信停止
- Secret取得失敗
- SES 認証失敗が継続

warning 候補:

- `otp_delivery_failed` 急増
- `otp_verify_failed` 急増
- rate limit 急増

## 11. SES bounce / complaint monitoring

Bounce / complaint monitoring is handled on the AWS side. Do not add a public
Cloud Run callback endpoint for SES notification payloads.

Current decision:

- Use SES identity or configuration set notifications to Amazon SNS, or SES
  event publishing to an AWS-native destination.
- Use CloudWatch reputation or event metrics as warning-level signals if AWS
  operations adopts CloudWatch alarms.
- Keep Notion / chat records to aggregate counts, hashes, delivery_id, and
  status only. Do not copy raw recipient email or provider event payloads.

Before enabling this monitoring path, confirm:

- SNS topic ARN or CloudWatch alarm names
- notification destination and owner
- whether `AWS_SES_CONFIGURATION_SET` is applied to all OTP send requests when
  configuration set event publishing is used
- whether email feedback forwarding remains enabled or is intentionally replaced
  by SNS / event publishing

Runbook:

- Monitoring design: `docs/monitoring.md` section `SES Bounce / Complaint`
- First response: `docs/operations.md` section
  `SES bounce / complaint warning response`
