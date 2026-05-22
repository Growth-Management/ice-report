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

## 3. Cloud Run 更新例

```bash
gcloud run services update report-generator \
  --project=ice-sh \
  --region=asia-northeast1 \
  --update-env-vars=MAIL_PROVIDER=ses,MAIL_SERVICE_NAME="ICE Report Generator" \
  --update-secrets=AWS_SES_ROLE_ARN=aws-ses-role-arn:latest,AWS_SES_WEB_IDENTITY_AUDIENCE=aws-ses-web-identity-audience:latest,AWS_SES_REGION=aws-ses-region:latest,AWS_SES_FROM_ADDRESS=aws-ses-from-address:latest,AWS_SES_FROM_NAME=aws-ses-from-name:latest
```

設定を段階適用したい場合は、先に Secret だけ追加し、最後に `MAIL_PROVIDER=ses` を有効化します。

## 4. デプロイ後の確認

- Cloud Run revision が起動失敗していない
- `MAIL_PROVIDER=ses` で起動している
- OTP リクエストで `ICE_REPORT_OTP_DELIVERY_SENT` が出る
- `ICE_REPORT_SES_STS_ASSUMED` が出る
- `otp_delivery_failed` や `mail_provider_auth_failed` が増えていない
- SES 側で bounce / complaint が異常増加していない

## 5. Smoke Test

1. テスト用 delivery を 1 件作成する
2. 許可済みメールアドレスから PIN 発行を実行する
3. メール受信、PIN 認証、Signed URL ダウンロードまで通す
4. `security_events` に `otp_delivery_sent` と `download_session_success` が残ることを確認する

## 6. ロールバック

異常時は次の順で戻します。

1. 直前の正常 revision にトラフィックを戻す
2. もしくは `MAIL_PROVIDER=logging` に戻す
3. OTP 送信可否を再確認する
4. STS trust policy や audience 変更は原因切り分け完了まで広げない

## 7. 切替後の削除候補

即削除せず、切替安定後に確認付きで整理します。

- `AWS_SES_ACCESS_KEY_ID`
- `AWS_SES_SECRET_ACCESS_KEY`
- access key 前提の手順書
- 旧 fallback 名を前提にした deploy メモ

## 8. 監視メモ

critical 候補:

- OTP送信停止
- Secret取得失敗
- SES 認証失敗が継続

warning 候補:

- `otp_delivery_failed` 急増
- `otp_verify_failed` 急増
- rate limit 急増
