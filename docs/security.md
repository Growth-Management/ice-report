# Security

ICE Report Generator のセキュリティ方針と現在の保護制御です。運用手順は `docs/operations.md`、SES 切替・確認手順は `docs/ses-cutover-checklist.md` を参照します。

## 保護対象

- 管理 API と管理画面
- 配布レコード、配布URL、delivery token
- OTP / PIN challenge と download session
- GCS 上のレポートファイル
- Firestore の `deliveries`、`download_logs`、`security_events`
- Secret Manager の `ADMIN_API_KEY` と SES 関連設定
- SES Web Identity role / trust policy
- Cloud Logging、Slack 通知、Notion などの運用記録

## 現在の本番制御

### 管理 API

管理 API は `ADMIN_API_KEY` と `X-Admin-Key` header で保護します。

- 本番では `ADMIN_API_KEY` を Secret Manager から Cloud Run に注入する
- key の値はチャット、ログ、Notion、スクリーンショットに残さない
- `ADMIN_API_KEY` が未設定の場合、現在の実装は local/dev 向けに admin check を通すため、本番では未設定を許容しない
- 管理 API の呼び出し確認は `docs/operations.md` の手順を使う

### 配布URL

配布URLは token を含む URL です。Firestore には検索用に `token_hash` も保存します。

- token は `secrets.token_urlsafe(32)` で生成する
- 配布レコードは `active` と `expires_at` で有効性を制御する
- 許可判定は `allowed_emails` または `allowed_domains` で行う
- 停止時は配布レコードの `active=false` にする
- version 追加時も配布URLは変えず、`current_version` を更新する

### OTP / PIN

ダウンロード前にメールアドレスへ 6 桁 PIN を送信します。

- PIN は平文保存せず、`OTP_HASH_SECRET` 由来の HMAC SHA-256 で hash 化する
- 本番では `OTP_HASH_SECRET` を設定する
- fallback は `SECRET_KEY`、`ADMIN_API_KEY`、最後に `PROJECT_ID` だが、本番で `PROJECT_ID` fallback に落としてはいけない
- PIN TTL default は `OTP_PIN_TTL_MINUTES=10`
- PIN 入力上限 default は `OTP_MAX_ATTEMPTS=5`
- PIN 再発行間隔 default は `OTP_RESEND_INTERVAL_SECONDS=60`
- 新しい PIN 発行時は同じ token / email の既存 challenge を revoke する
- `OTP_RATE_LIMIT_IP_PER_MINUTE` default は `3`
- `OTP_RATE_LIMIT_IP_PER_10_MINUTES` default は `10`
- `OTP_RATE_LIMIT_EMAIL_PER_MINUTE` default は `3`
- `OTP_RATE_LIMIT_EMAIL_PER_10_MINUTES` default は `10`

### Download Session

PIN 認証成功後、短時間の download session cookie を発行します。

- session token は平文保存せず hash 化する
- cookie は `Secure`、`HttpOnly`、`SameSite=Lax`
- session TTL default は `DOWNLOAD_SESSION_TTL_MINUTES=15`
- `DOWNLOAD_SESSION_ONE_TIME` default は `true`
- session がない、期限切れ、使用済みの場合は再度 PIN 認証を要求する

### Signed URL

GCS ファイルは直接公開せず、認証後に短時間の Signed URL へ redirect します。

- Signed URL は V4 GET のみ
- 有効期限 default は `DOWNLOAD_SIGNED_URL_SECONDS=300`
- signing service account は `SIGNED_URL_SERVICE_ACCOUNT` で指定する
- runtime service account は必要な GCS / IAMCredentials 権限だけを持つ

### SES 認証

本番メール送信は AWS long-lived access key ではなく、Cloud Run service account の Google-signed ID token を AWS STS `AssumeRoleWithWebIdentity` へ渡す構成です。

- Cloud Run runtime service account: `ice-report-runner@ice-sh.iam.gserviceaccount.com`
- AWS account: `855532282119`
- SES region: `ap-northeast-1`
- sender: `report-noreply@ice-sv.jp`
- custom MAIL FROM: `bounce.ice-sv.jp`
- `AWS_SES_ACCESS_KEY_ID` / `AWS_SES_SECRET_ACCESS_KEY` は本番経路の前提にしない
- `ice-report-ops` の既存 Access Key は 2 本とも無効化済み

### Logging

Cloud Logging には調査可能なイベント名を出します。ただし、機微情報の生値は出しません。

主なイベント:

- `ICE_REPORT_SECURITY_EVENT`
- `ICE_REPORT_OTP_PIN`
- `ICE_REPORT_OTP_DELIVERY_SENT`
- `ICE_REPORT_MAIL_DELIVERY_ATTEMPT`
- `ICE_REPORT_SES_STS_ASSUMED`

Cloud Logging で使う識別子:

- `token_hash`
- `email_hash`
- `recipient_hash`
- `delivery_id`
- `safe_reason`
- `provider_error_code`

Cloud Logging に出してはいけない値:

- 生 PIN
- 生 token
- 生メールアドレス
- Secret / Access Key / Admin Key
- Authorization header

### Firestore Security Events

`security_events` は監査・障害調査用です。Cloud Logging とは異なり、実装上は token や email を含む record が保存されます。

- `security_events` は機微情報を含む operational record として扱う
- 読み取り権限は運用担当に限定する
- Notion や Slack へ転記する場合は hash / event_type / delivery_id までに留める
- retention / archive lifecycle は今後の実装課題として管理する

### Slack 通知

Slack 通知には delivery_id、顧客名、対象月、email、配布URL、GCS URI が含まれる場合があります。

- webhook URL は Secret Manager で管理する
- Slack 通知先 channel は運用担当に限定する
- 通知本文は外部共有しない
- webhook secret の更新時は疎通確認後に旧値を無効化する

## 本番必須設定

本番 Cloud Run で未設定を許容しないもの:

- `ADMIN_API_KEY`
- `OTP_HASH_SECRET`
- `MAIL_PROVIDER=ses`
- `AWS_SES_ROLE_ARN`
- `AWS_SES_WEB_IDENTITY_AUDIENCE`
- `AWS_SES_REGION`
- `AWS_SES_FROM_ADDRESS`
- `PROJECT_ID=ice-sh`
- `BIGQUERY_PROJECT_ID=jumpplus-4a5f4`
- `PUBLIC_BASE_URL`

本番で削除候補として扱うもの:

- `AWS_SES_ACCESS_KEY_ID`
- `AWS_SES_SECRET_ACCESS_KEY`
- access key 前提の手順書
- 旧 fallback 名だけを前提にした deploy メモ

## 権限方針

### GCP

- deploy 用 service account と runtime service account を分離する
- runtime service account に project-wide な過剰権限を付けない
- Secret Manager は必要な Secret 単位で `roles/secretmanager.secretAccessor` を付与する
- GCS、Firestore、BigQuery、IAMCredentials は report-generator が必要な範囲に限定する

### AWS

- SES 送信用 role は `ses:SendEmail` / `ses:SendRawEmail` を中心に最小化する
- trust policy は Google OIDC の `aud` と Cloud Run service account の `sub` で制限する
- SES read 確認は必要時だけ `ses:GetAccount` / `ses:GetEmailIdentity` を持つ確認主体で行う
- 作業用 credential は必要時だけ発行し、確認後に無効化する

## 運用時の禁止事項

- credential 値をローカルファイル、チャット、Notion、ログに貼らない
- IDE や画面共有に credential 断片が出た場合は安全側でローテーション対象にする
- OTP / PIN の TTL、attempt 上限、session one-time 条件を障害対応で緩めない
- SES 障害時に未検証 provider へ無断で切り替えない
- 配布URLや Signed URL を公開チャンネルへ貼らない

## 監視・アラート

Cloud Logging の user-defined log-based metrics と Cloud Monitoring alert policies を作成済みです。詳細は `docs/monitoring.md` を参照します。

critical 相当:

- OTP 送信停止
- `mail_provider_auth_failed` 継続
- Cloud Run revision 起動失敗

warning 相当:

- `otp_delivery_failed` 急増
- `otp_verify_failed` 急増
- rate limit 急増
- SES bounce / complaint 増加

未実装:

- `/api-health` uptime check
- warning と critical の通知先分離
- SES bounce / complaint の CloudWatch / SES 側監視連携

## 残論点

### Google Login for Admin

現状の admin 認証は `ADMIN_API_KEY` です。次段階では Google Login / IAP / Cloud Run IAM などの導入可否を検討します。

確認観点:

- 管理画面利用者を Google identity で追跡できるか
- API client / script 利用と人間の管理画面利用を分けられるか
- emergency 時に break-glass 手順を残せるか

### Admin Audit Logs

現状は download / security event が中心で、管理操作の audit log は十分ではありません。

追加候補:

- delivery 作成
- version 追加
- delivery disable / enable
- cleanup 実行
- admin key 認証失敗

### Archive Lifecycle Management

GCS、Firestore、Slack、Notion に分散する運用記録の保持期間が未整理です。

追加候補:

- `security_events` の retention
- `download_logs` の retention
- 期限切れ delivery の扱い
- GCS report object の削除または archive 方針
- Google Drive backup 後の GCS cleanup 方針
