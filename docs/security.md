# Security

ICE Report Generator のセキュリティ方針と現在の保護制御です。運用手順は `docs/operations.md`、SES 切替・確認手順は `docs/ses-cutover-checklist.md` を参照します。

## 保護対象

- 管理 API と管理画面
- 配布レコード、配布URL、delivery token
- OTP / PIN challenge と download session
- GCS 上のレポートファイル
- Firestore の `deliveries`、`download_logs`、`security_events`、`admin_audit_logs`
- Secret Manager の `ADMIN_API_KEY` と SES 関連設定
- SES Web Identity role / trust policy
- Cloud Logging、Slack 通知、Notion などの運用記録

## 現在の本番制御

### 管理 API

管理 API は `ADMIN_API_KEY` と `X-Admin-Key` header で保護します。

- 本番では `ADMIN_API_KEY` を Secret Manager から Cloud Run に注入する
- key の値はチャット、ログ、Notion、スクリーンショットに残さない
- Cloud Run runtime または `ADMIN_AUTH_FAIL_CLOSED=1` では、`ADMIN_API_KEY` が未設定の場合も管理 API は `401` で fail closed する
- 誤った `X-Admin-Key`、header 未指定、または本番 secret 未設定は `admin_auth_failed` security event と `admin_audit_logs` の `admin_auth` failure として記録する
- local/dev でも本番に近い確認を行う場合は `ADMIN_API_KEY` と `ADMIN_AUTH_FAIL_CLOSED=1` を明示する
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
- legacy Secret Manager secret `aws-ses-access-key-id` / `aws-ses-secret-access-key` は 2026-06-01 に削除済み

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
- retention / archive lifecycle は下記の Archive Lifecycle Management を正とする

### Slack 通知

Slack 通知には delivery_id、顧客名、対象月、email、配布URL、GCS URI が含まれる場合があります。

- webhook URL は Secret Manager で管理する
- Slack 通知先 channel は運用担当に限定する
- 通知本文は外部共有しない
- webhook secret の更新時は疎通確認後に旧値を無効化する

webhook URL の露出疑いがある場合:

- webhook URL 本文をチャット、Notion、ログ、PR本文に貼らない
- Slack側で旧webhookを revoke / delete できる権限者が対応する
- 新webhookを使う場合は Secret Manager に新versionとして登録し、Cloud Run はSecret名だけを参照する
- 疎通確認後、旧webhookが利用不能であることをSlack側で確認する
- 確認記録にはchannel名、実施日時、実施者、成功/失敗だけを残し、URL値は残さない

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

本番で使わないもの:

- `AWS_SES_ACCESS_KEY_ID`
- `AWS_SES_SECRET_ACCESS_KEY`
- access key 前提の手順書
- 旧 fallback 名だけを前提にした deploy メモ

棚卸し結果は `docs/env-compatibility.md` に記録します。2026-06-01 時点で本番 Cloud Run に旧 env 名は残っておらず、Secret Manager の legacy access key secret 2 件も削除済みです。

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

## Secret Exposure Response

`env.yaml`、`webhook.txt`、`.env*`、access key CSV などに本番値が含まれていた可能性がある場合は、値を読んで共有するのではなく、次の順で扱います。

1. 対象ファイルの内容をチャット、ログ、Notionへ転記しない
2. `scripts/check-secret-exposure-metadata.ps1 -AsJson` で、git履歴・Cloud Run env・Secret Manager versionのメタデータだけを確認する
3. 有効なsecretが含まれていた可能性があるものは、先にrotationまたは失効確認を行う
4. 本番疎通を `scripts/check-operations-readonly.ps1 -AsJson` で確認する
5. 履歴削除が必要か、rotation済みとして履歴保持を許容するかを明示判断する

2026-06-11 時点の対応:

- `report-generator-admin-api-key` version 1 は旧versionとして disabled
- `report-generator-admin-api-key` version 2 は enabled
- 無効化後の read-only operational check は PASS
- `aws-ses-access-key-id` / `aws-ses-secret-access-key` は GCP Secret Manager 上では存在しない
- Slack webhook URL は Slack側で旧webhookの無効化/再発行済み確認が必要

Slack webhook の確認完了条件:

- 旧webhookがSlack側で revoke / delete 済み、または既に無効である
- 新webhookが必要な場合は、Secret Manager に保存済みで、ローカルファイルやdocsに値が残っていない
- ICE Report Generator の `SLACK_WEBHOOK_SECRET_NAME` はSecret名だけを保持している
- テスト通知または代替確認で、運用担当channelへの通知経路が確認済み
- Notion記録にはURL実値を含めない

Repo hygiene 残件確認:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-secret-exposure-metadata.ps1 -AsJson |
  Set-Content -Encoding UTF8 artifacts\secret-exposure-metadata.json

powershell.exe -ExecutionPolicy Bypass -File scripts\check-doc-legacy-references.ps1
```

確認対象:

- `env.yaml`
- `webhook.txt`
- `.env`
- `.env.*`
- `tools.yaml`
- `*_accessKeys.csv`
- GCP Secret Manager の関連secret metadata
- Cloud Run env の secret参照名と literal value有無
- docs内の旧env名・旧access key前提手順

記録する項目:

- 対象ファイルが現在のHEADでtrackingされているか
- git履歴上のcommit件数、first / latest commit
- legacy AWS access key env がCloud Run envに残っていないこと
- legacy Secret Manager secret が存在しない、または不要versionがdisabled/destroyedであること
- Slack webhook旧値の無効化/再発行済み確認結果
- docs legacy reference check の `unexpectedMatches`

記録しない項目:

- secret値、webhook URL実値、access key値
- `env.yaml`、`webhook.txt`、`.env*`、access key CSV の内容
- Secret Manager payload

判断:

- 有効なsecret値が露出していた可能性が残る場合は、履歴削除より先にrotation
  またはrevokeを完了する
- 既にrotation / revoke済みで、履歴に残るのが無効値だけと判断できる場合は、
  履歴rewriteを必須にしない
- `unexpectedMatches` が1件以上ある場合は、該当docsを修正してから作業終了にする

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

- warning と critical の通知先分離
- SES bounce / complaint の CloudWatch / SES 側監視連携

## 残論点

### Google Login for Admin

現状の admin 認証は `ADMIN_API_KEY` です。短期は API key hardening を継続し、中期は人間向け Admin UI を Admin 専用 Cloud Run service へ分離して IAP で保護する方針です。

確認観点:

- 管理画面利用者を Google identity で追跡できるか
- API client / script 利用と人間の管理画面利用を分けられるか
- emergency 時に break-glass 手順を残せるか

短期方針:

- `ADMIN_API_KEY` は machine/script と break-glass 用に継続する
- `ADMIN_AUTH_FAIL_CLOSED=1` または Cloud Run runtime では Admin API を fail closed する
- 認証失敗は `admin_auth_failed` security event と `ICE_REPORT_ADMIN_AUDIT action=admin_auth result=failure` に記録する
- break-glass 利用後は Admin key rotation を原則とし、手順は `docs/operations.md` を正とする

中期方針:

- Admin UI は `report-generator-admin` のような Admin 専用 Cloud Run service に分離する
- public service は `/d/*`、OTP、`/api-health` など利用者向け経路を維持し、IAPの影響範囲から外す
- Admin専用serviceは Cloud Run direct IAP を第一候補にする
- IAP service agent に `roles/run.invoker` を付与し、管理者user/groupへ `roles/iap.httpsResourceAccessor` を付与する
- script/API向けには `X-Admin-Key` を break-glass / machine 経路として残す

非採用:

- 現行単一service全体へIAPを直接適用する案は、public download / OTP 経路へ影響するため採用しない
- 長期AWS access key方式への復帰、旧fallback env名を前提にした復旧手順は採用しない

参考:

- Google Cloud: `Configure IAP for Cloud Run` https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run
- Google Cloud: `Enable IAP for Cloud Run` https://docs.cloud.google.com/iap/docs/enabling-cloud-run

### Admin Audit Logs

管理操作は Firestore の `admin_audit_logs` と Cloud Logging の `ICE_REPORT_ADMIN_AUDIT` に記録します。Cloud Logging は検索・アラート用の概要、Firestore は運用調査用の構造化 record として扱います。

対象 action:

- `admin_auth`
- `generate_report`
- `delivery_create`
- `delivery_version_add`
- `delivery_disable`
- `delivery_enable`
- `cleanup_expired_deliveries`

主要 field:

- `action`
- `result`: `success` または `failure`
- `target_type`
- `target_id`
- `status_code`
- `reason`
- `actor_type`
- `admin_key_fingerprint`
- `path`
- `method`
- `ip`
- `user_agent`
- `created_at`
- `detail`

禁止値:

- 生 `ADMIN_API_KEY`
- 生 PIN / OTP
- 生 delivery token / download session token
- 生メールアドレス
- Authorization header
- AWS / GCP credential 値

運用上の検索 key は `action`、`result`、`target_id`、`status_code`、`reason`、`created_at` を基本にします。Notion や Slack へ転記する場合は、`action`、`result`、`target_id`、`status_code`、`reason`、件数に留め、key fingerprint や詳細 record 全体は転載しません。

### Archive Lifecycle Management

月次運用の baseline は `docs/operations.md` の「月次運用」を正とします。GCS、Firestore、Slack、Notion、Google Drive に分散する運用記録は、次の方針で扱います。

- active delivery の current version が参照する GCS object は削除しない
- 期限切れ delivery は cleanup で `active=false` にし、Firestore record は即削除しない
- GCS report object は Google Drive backup と明示承認なしに削除しない
- cleanup 実行、overwrite、GCS削除、Firestore record 削除は Notion または運用記録に実施者、日時、対象、理由を残す
- Slack は通知経路であり、system of record にはしない。必要な判断記録は Notion または docs に転記する

保持期間の基準:

| 対象 | 保存場所 | 最短保持期間 | 削除条件 |
| --- | --- | --- | --- |
| active delivery | Firestore `deliveries` | active 中は削除不可 | `active=false`、問い合わせ対応期間終了、Notion承認済み |
| inactive / expired delivery | Firestore `deliveries` | `expires_at` から 400 日 | 関連GCS objectとDrive backupの状態確認後、Notion承認済み |
| report Excel object | GCS `gs://ice-report-files/...` | `expires_at` から 180 日、かつ対象月から 13 か月の遅い方 | active/current参照なし、Drive backup確認済み、Notion承認済み |
| Drive backup | Google Drive 管理フォルダ | 対象月から 7 年目安 | 業務保管方針の明示承認がある場合のみ |
| download logs | Firestore `download_logs` | 作成から 400 日 | incident / 問い合わせ対応が完了し、Notion承認済み |
| security events | Firestore `security_events` | 作成から 400 日 | incident / abuse調査が完了し、Notion承認済み |
| admin audit logs | Firestore `admin_audit_logs` | 作成から 400 日 | 管理操作監査の確認期間終了後、Notion承認済み |
| OTP challenges / download sessions | Firestore `otp_challenges`, `download_sessions` | 有効期限切れから 30 日 | incident調査中でないこと |
| Slack通知履歴 | Slack workspace | Slack workspace retention に従う | 削除判断の正本にしない |

削除承認の必須項目:

- 対象種別
- 対象ID、GCS URI、collection名などの検索キー
- 保持期限を満たしている根拠
- Drive backup URL、またはbackup不要と判断した理由
- active/current参照がないことの確認結果
- 削除理由
- 承認者
- 実施者
- 実施日時
- 削除後確認結果

自動削除は現時点では未実装です。削除を行う場合は `docs/operations.md` の runbook に従い、bulk delete や ad hoc script を本番に対して直接実行しません。

## Admin IAP user auth addendum

Admin専用serviceでは、`ADMIN_IAP_AUTH_ENABLED=1` と `ADMIN_IAP_ALLOWED_EMAILS` を設定した場合に限り、IAPが付与する `X-Goog-Authenticated-User-Email` を管理API認証に使えます。

安全条件:

- `ADMIN_IAP_SERVICE_NAME` のdefaultは `report-generator-admin`
- public service `report-generator` には `ADMIN_IAP_AUTH_ENABLED` を設定しない
- 許可メールは `ADMIN_IAP_ALLOWED_EMAILS` で明示する
- 監査ログには生メールを保存せず、`iap_email_hash` のみ保存する
- `ADMIN_API_KEY` はmachine/scriptとbreak-glass用として継続する
