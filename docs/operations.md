# Operations Playbook

ICE Report Generator の本番運用手順です。日常確認、リリース、smoke test、OTP 送信停止時の初動、rollback をここに集約します。

## 本番環境

- GCP project: `ice-sh`
- Cloud Run service: `report-generator`
- Region: `asia-northeast1`
- Service URL: `https://report-generator-635067190197.asia-northeast1.run.app`
- Runtime service account: `ice-report-runner@ice-sh.iam.gserviceaccount.com`
- Deploy impersonation service account: `ice-deployer@ice-sh.iam.gserviceaccount.com`
- Artifact Registry image: `asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:<git-sha>`
- BigQuery project: `jumpplus-4a5f4`
- SES AWS account: `855532282119`
- SES region: `ap-northeast-1`
- Sender: `report-noreply@ice-sv.jp`
- Custom MAIL FROM: `bounce.ice-sv.jp`

## 日常確認

Cloud Monitoring の alert policy と log-based metrics は `docs/monitoring.md` に記録します。

### Cloud Run

```powershell
Invoke-WebRequest -Uri 'https://report-generator-635067190197.asia-northeast1.run.app/api-health' -UseBasicParsing |
  Select-Object StatusCode,Content
```

確認ポイント:

- `StatusCode` が `200`
- response が `{"status":"ok"}`
- Cloud Run revision の起動失敗や traffic split の異常がない

```powershell
gcloud.cmd run services describe report-generator `
  --project=ice-sh `
  --region=asia-northeast1 `
  --format='value(status.latestReadyRevisionName,status.traffic[0].percent,spec.template.spec.containers[0].image)'
```

### 管理画面

```powershell
Invoke-WebRequest -Uri 'https://report-generator-635067190197.asia-northeast1.run.app/admin' -UseBasicParsing |
  Select-Object StatusCode,RawContentLength
```

管理 API を確認する場合は `ADMIN_API_KEY` を Secret Manager から取得し、`X-Admin-Key` で送ります。key の値はログやチャットへ貼らないでください。

```powershell
$adminKey = & gcloud.cmd secrets versions access latest --secret=report-generator-admin-api-key --project=ice-sh
Invoke-RestMethod `
  -Uri 'https://report-generator-635067190197.asia-northeast1.run.app/deliveries?limit=100' `
  -Headers @{ 'X-Admin-Key' = $adminKey } `
  -Method Get
```

Admin 認証の確認ポイント:

- key の値は画面共有、ログ、チャット、Notion、スクリーンショットに残さない
- 無認証または誤った `X-Admin-Key` は `401` になる
- 認証失敗は `admin_auth_failed` security event として記録される
- 認証失敗は `ICE_REPORT_ADMIN_AUDIT action=admin_auth result=failure` としても記録される
- Cloud Run runtime では `ADMIN_API_KEY` 未設定も `401` で fail closed する

認証失敗イベント確認例:

```powershell
gcloud.cmd logging read `
  'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND textPayload:"ICE_REPORT_SECURITY_EVENT type=admin_auth_failed"' `
  --project=ice-sh `
  --freshness=30m `
  --limit=20 `
  --format='value(timestamp,textPayload)'
```

### Admin audit log

管理操作は Firestore の `admin_audit_logs` と Cloud Logging の `ICE_REPORT_ADMIN_AUDIT` に記録されます。対象は `admin_auth`、`generate_report`、`delivery_create`、`delivery_version_add`、`delivery_disable`、`delivery_enable`、`cleanup_expired_deliveries` です。

Cloud Logging での確認例:

```powershell
gcloud.cmd logging read `
  'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND textPayload:"ICE_REPORT_ADMIN_AUDIT"' `
  --project=ice-sh `
  --freshness=30m `
  --limit=20 `
  --format='value(timestamp,textPayload)'
```

特定操作だけを見る場合:

```powershell
gcloud.cmd logging read `
  'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND textPayload:"ICE_REPORT_ADMIN_AUDIT action=delivery_create"' `
  --project=ice-sh `
  --freshness=30m `
  --limit=20 `
  --format='value(timestamp,textPayload)'
```

Firestore record は `action`、`result`、`target_id`、`status_code`、`reason`、`created_at` を中心に確認します。Notion や Slack へ転記する場合は、Admin key fingerprint、credential、PIN、token、生メールアドレスを含めません。

### Admin key break-glass / rotation

`ADMIN_API_KEY` は通常の人間向け主認証ではなく、machine/script と break-glass 用の共有鍵として扱います。

break-glass 利用条件:

- IAP / Google identity / Admin UI 移行中の障害で通常経路が使えない
- 緊急停止、cleanup、配布状態確認など、管理 API 操作が必要
- 利用理由、実行者、実行時刻、対象 delivery_id を運用記録へ残せる

利用後の必須対応:

1. 実行した管理 API、対象、結果を運用記録へ残す
2. `admin_auth_failed`、`ICE_REPORT_ADMIN_AUDIT`、runtime error、想定外のmutationが増えていないか確認する
3. break-glass で使った Admin key は原則 rotation する

rotation 手順:

```powershell
$newAdminKey = Read-Host 'New ADMIN_API_KEY' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($newAdminKey)
try {
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  $plain | gcloud.cmd secrets versions add report-generator-admin-api-key `
    --project=ice-sh `
    --data-file=-
} finally {
  if ($ptr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
  }
  $plain = $null
}
```

Secret version 追加後は、通常の deploy 手順で新しい Cloud Run revision を作成し、`ADMIN_API_KEY` が新versionで読み込まれる状態にします。deploy後は次を確認します。

- 無認証の管理 API が `401`
- 新key付きの `GET /deliveries?limit=1` が `200`
- 古いkeyが `401`
- `admin_auth_failed` が想定外に増えていない

### 配布一覧の視覚確認

headless Chrome が使える環境では、補助スクリプトで管理画面のスクリーンショットを取得します。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\capture-admin-deliveries.ps1
```

生成物は `artifacts/` 配下に出ます。`artifacts/` は git 管理しません。

## 月次運用

月次レポート作成、配布URL発行、期限切れ整理、version追加を行うときの基準です。管理画面で操作する場合も、APIで操作する場合もこの順序を正とします。

### 月次作成前確認

1. 対象月を `YYYY-MM` で確定する。未指定でレポート生成すると、実行日の前月が対象月になります。
2. 顧客名、許可メール、許可ドメインを確認する。許可宛先が空欄の場合は既定ドメインが使われます。
3. 既存GCSファイルを使うか、BigQueryを再実行して新規生成するかを決める。
4. 生成時の保存先は `gs://ice-report-files/reports/plus/<yymm>/...xlsx` を基本にする。
5. 送付前に配布URL、対象月、顧客名、current version、期限、GCS URI を確認する。

GCS URI を空欄にして配布作成すると、backend が BigQuery を再実行して Excel を生成し、GCS へ保存してから delivery を作成します。既に検品済みの Excel を配布する場合は、GCS URI を明示します。

### 月次作成の実施

管理画面では「配布作成」を使います。APIで実施する場合の最小payloadは次です。

```powershell
$base = 'https://report-generator-635067190197.asia-northeast1.run.app'
$adminKey = & gcloud.cmd secrets versions access latest --secret=report-generator-admin-api-key --project=ice-sh

Invoke-RestMethod `
  -Uri "$base/deliveries" `
  -Headers @{ 'X-Admin-Key' = $adminKey } `
  -Method Post `
  -ContentType 'application/json' `
  -Body (@{
    customer_name = '一ツ橋企画'
    report_month = '2026-05'
    gcs_uri = 'gs://ice-report-files/reports/plus/2605/example.xlsx'
    allowed_emails = @('user@example.com')
    allowed_domains = @('example.com')
    version_note = 'monthly initial'
  } | ConvertTo-Json)
```

作成後は次を確認します。

- `/deliveries?limit=100` に delivery が表示される
- `active=true`
- `expires_at` が想定どおり
- `current_version=1`
- Slack 通知の delivery_id、対象月、GCS URI が操作内容と一致する
- `ICE_REPORT_ADMIN_AUDIT action=delivery_create result=success` が記録される
- 配布先へ共有するURLは `public_download_url` だけにする

### 期限切れ配布の扱い

配布レコードの有効性は `active` と `expires_at` で判定します。期限切れ後はURLを再共有せず、必要な場合は新しい delivery を作成します。

- 期限前に止める場合: delivery の停止操作で `active=false` にする
- 期限後に整理する場合: `/internal/cleanup` で期限切れの active delivery を `active=false` にする
- cleanup は Firestore の delivery を無効化するだけで、GCS object は削除しない
- 誤配布や宛先ミスの場合は、cleanup を待たずに即時停止し、Notion または運用記録に理由を残す

cleanup 実行例:

```powershell
$base = 'https://report-generator-635067190197.asia-northeast1.run.app'
$adminKey = & gcloud.cmd secrets versions access latest --secret=report-generator-admin-api-key --project=ice-sh

Invoke-RestMethod `
  -Uri "$base/internal/cleanup" `
  -Headers @{ 'X-Admin-Key' = $adminKey } `
  -Method Post
```

cleanup 後は `updated_count`、`updated_delivery_ids`、`cleanup_at`、`ICE_REPORT_ADMIN_AUDIT action=cleanup_expired_deliveries result=success` を確認し、月次作業記録に残します。

### version追加 / overwrite の確認ルール

version追加は、配布URLを変えずに新しい Excel を current version にする操作です。通常は overwrite OFF を使い、既存GCS objectを残したまま新しいファイル名で保存します。

overwrite OFF を標準にする場面:

- 月次データの再集計や差し替え
- 送付済みファイルの履歴をGCS上でも残したい場合
- 変更前後を比較できる状態にしたい場合

overwrite ON は現在versionのファイル名を再利用し、同じGCS object名へ保存します。bucket versioning を前提にしないため、上書き前のファイルは Google Drive backup を正とします。

overwrite ON の実行前に必ず確認する項目:

1. delivery_id、顧客名、対象月、current version が対象と一致する
2. 現在versionの GCS URI と file name を確認済み
3. 上書き前の Excel を Google Drive の管理フォルダへ backup 済み
4. 同じファイル名を維持する理由がある
5. version note に `overwrite`、実施理由、確認者を残す

実行後は、current version が増えていること、GCS URI、ファイル更新時刻、Slack通知、`ICE_REPORT_ADMIN_AUDIT action=delivery_version_add result=success` を確認します。利用者影響がある差し替えでは、必要に応じて許可済みメールアドレスで OTP からダウンロードまで確認します。

### Google Drive backup 後の GCS cleanup 方針

GCS object の削除は破壊的操作のため、通常の月次手順では実行しません。保持期間と承認条件は `docs/security.md` の Archive Lifecycle Management を正とします。現時点の方針は次です。

- active delivery の current version が参照する GCS object は削除しない
- 期限切れ delivery でも、問い合わせ対応や再送に備えて `expires_at` から 180 日、かつ対象月から 13 か月の遅い方まで保持する
- GCS削除候補にする前に、Google Drive の管理フォルダへ Excel backup を保存する
- backup ファイル名には顧客名、対象月、delivery_id、version、元GCS URIが分かる情報を含める
- 削除候補は Notion に記録し、明示承認後にだけ削除する

#### Retention review

Retention review は月次運用とは分け、四半期に1回を目安に実施します。incident 対応、顧客問い合わせ、再送依頼が継続している対象は削除候補から外します。

確認対象:

| 対象 | 確認観点 |
| --- | --- |
| Firestore `deliveries` | `active=false`、`expires_at`、`current_version`、参照GCS URI |
| Firestore `download_logs` | 作成日時、delivery_id、問い合わせ対応の有無 |
| Firestore `security_events` | incident / abuse調査の有無、event_type |
| Firestore `admin_audit_logs` | 管理操作監査の確認期間、target_id |
| GCS report object | active/current参照、対象月、Drive backup有無 |
| Slack通知 | 必要な判断記録がNotionへ転記済みか |
| Google Drive backup | backup URL、ファイル名、対象月、delivery_id |

#### GCS削除候補の確認

読み取り確認例:

```powershell
gcloud.cmd storage ls "gs://ice-report-files/reports/plus/<yymm>/"
```

delivery の参照確認:

```powershell
$base = 'https://report-generator-635067190197.asia-northeast1.run.app'
$adminKey = & gcloud.cmd secrets versions access latest --secret=report-generator-admin-api-key --project=ice-sh

Invoke-RestMethod `
  -Uri "$base/deliveries?limit=500" `
  -Headers @{ 'X-Admin-Key' = $adminKey } `
  -Method Get
```

確認ルール:

1. active delivery の `current_version` が参照する GCS URI は削除しない
2. inactive delivery でも `expires_at` から 180 日未満、または対象月から 13 か月未満の場合は削除しない
3. Google Drive backup URL が確認できない場合は削除しない
4. Notion に削除候補を記録し、承認者の明示承認を得る
5. 削除前後の `gcloud.cmd storage ls` 結果を記録する

実削除は承認済みの単一 object に限定して実行します。wildcard や directory 相当の prefix 削除は使いません。

```powershell
gcloud.cmd storage rm "gs://ice-report-files/reports/plus/<yymm>/<file>.xlsx"
```

#### Firestore record削除候補の扱い

Firestore record の削除は、GCS object 削除よりも調査影響が大きいため、現時点では原則として手動削除しません。削除が必要な場合は、対象 collection、document id、削除理由、復旧要否を Notion に記録し、個別PRで削除用scriptまたは管理コマンドを用意してから実施します。

削除候補にできる最短条件:

- `deliveries`: `active=false` かつ `expires_at` から 400 日経過
- `download_logs`: `created_at` から 400 日経過
- `security_events`: `created_at` から 400 日経過
- `admin_audit_logs`: `created_at` から 400 日経過
- `otp_challenges` / `download_sessions`: 期限切れから 30 日経過

#### Notion削除承認テンプレート

```text
ICE Report Generator 削除承認

対象種別:
対象ID / GCS URI / collection:
対象月:
顧客名:
現在の active/current 参照確認:
保持期限を満たしている根拠:
Drive backup URL:
削除理由:
影響範囲:
復旧可能性:
承認者:
実施者:
実施予定日時:
削除後確認結果:
関連 audit log / admin audit log:
```

## リリース手順

詳細コマンドは `docs/deploy.md` を参照します。運用上の順序は次です。

1. `main` を最新化し、作業ツリーが clean であることを確認する
2. 対象 commit SHA で Docker image を build する
3. Artifact Registry へ push する
4. Cloud Run へ deploy する
5. `/api-health` と `/admin` を確認する
6. 必要に応じて OTP smoke test を 1 回だけ実行する
7. Cloud Logging で `ICE_REPORT_MAIL_DELIVERY_ATTEMPT` と `ICE_REPORT_OTP_DELIVERY_SENT` を確認する
8. 生 PIN、生メールアドレス、生 token がログに出ていないことを確認する
9. Notion の該当タスクへ revision、commit、smoke 結果を記録する

## Smoke Test

本番で実施する smoke test は、送信先と回数を最小にします。実メールを送るため、実施前に対象 delivery と宛先を確認してください。

### 基本 smoke

```powershell
$base = 'https://report-generator-635067190197.asia-northeast1.run.app'
$health = Invoke-WebRequest -Uri "$base/api-health" -UseBasicParsing
$admin = Invoke-WebRequest -Uri "$base/admin" -UseBasicParsing
[pscustomobject]@{
  healthStatus = $health.StatusCode
  healthContent = $health.Content
  adminStatus = $admin.StatusCode
  adminLength = $admin.RawContentLength
}
```

### OTP smoke

1. `/deliveries?limit=100` から active な配布レコードを 1 件選ぶ
2. 許可済みメールアドレスで `/d/<token>/request-pin` へ POST する
3. HTTP `200` を確認する
4. Cloud Logging で送信ログを確認する
5. 受信メール、PIN 認証、ダウンロードまで必要に応じて確認する

ログ確認例:

```powershell
$revision = '<latest-ready-revision>'
$mailFilter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND resource.labels.revision_name="' + $revision + '" AND textPayload:"ICE_REPORT_MAIL_DELIVERY_ATTEMPT"'
$sentFilter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND resource.labels.revision_name="' + $revision + '" AND textPayload:"ICE_REPORT_OTP_DELIVERY_SENT"'
$pinFilter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND resource.labels.revision_name="' + $revision + '" AND textPayload:"ICE_REPORT_OTP_PIN"'
$mailTimestamps = @(gcloud.cmd logging read $mailFilter --project=ice-sh --freshness=20m --limit=20 --format='value(timestamp)')
$sentTimestamps = @(gcloud.cmd logging read $sentFilter --project=ice-sh --freshness=20m --limit=20 --format='value(timestamp)')
$pinPayloads = @(gcloud.cmd logging read $pinFilter --project=ice-sh --freshness=20m --limit=20 --format='value(textPayload)')
[pscustomobject]@{
  revision = $revision
  mailAttemptCount = $mailTimestamps.Count
  otpDeliverySentCount = $sentTimestamps.Count
  otpPinLogCount = $pinPayloads.Count
  rawPinPatternCount = @($pinPayloads | Where-Object { $_ -match '\spin=' }).Count
}
```

期待値:

- `mailAttemptCount` が 1 以上
- `otpDeliverySentCount` が 1 以上
- `rawPinPatternCount` が `0`

## OTP 送信停止時の一次対応

### 1. 障害判定

- 複数ユーザーまたは複数 delivery で OTP 未達が起きているか確認する
- `/api-health` が正常か確認する
- 直近 deploy、Secret 更新、Cloud Run 設定変更の有無を確認する
- `ICE_REPORT_MAIL_DELIVERY_ATTEMPT result=failure`、`mail_provider_auth_failed`、`otp_delivery_failed` の増加を確認する

```powershell
gcloud.cmd logging read `
  'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND (textPayload:"ICE_REPORT_MAIL_DELIVERY_ATTEMPT" OR textPayload:"mail_provider_auth_failed" OR textPayload:"otp_delivery_failed")' `
  --project=ice-sh `
  --freshness=60m `
  --limit=50 `
  --format='value(timestamp,textPayload)'
```

### 2. 切り分け

- Cloud Run revision が Ready か
- `MAIL_PROVIDER=ses` で起動しているか
- `AWS_SES_ROLE_ARN`、`AWS_SES_WEB_IDENTITY_AUDIENCE`、`AWS_SES_REGION`、`AWS_SES_FROM_ADDRESS` が設定されているか
- Secret Manager の `ADMIN_API_KEY` と SES 関連 Secret が取得できるか
- AWS STS Web Identity の assume が成功しているか
- SES account / identity が送信可能か
- rate limit や PIN resend interval による利用者単位の拒否ではないか

### 3. SES 状態確認

Web Identity 経路:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-ses-web-identity.ps1
```

AWS profile 経路:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-ses-direct.ps1 -Profile ice-report-ops
```

注意:

- `ice-report-ops` の既存 Access Key は 2 本とも無効化済みです
- direct 確認が必要な場合は、必要時だけ新しい作業用 credential を発行します
- credential の値はリポジトリ、ログ、チャット、Notion に残しません

確認ポイント:

- `ProductionAccessEnabled=True`
- `SendingEnabled=True`
- `EnforcementStatus=HEALTHY`
- identity `ice-sv.jp` の `VerificationStatus=SUCCESS`
- DKIM `SUCCESS`
- `MailFromDomainStatus=SUCCESS`

### 4. 暫定復旧判断

優先順:

1. 直前の正常 revision へ traffic を戻す
2. 設定変更が原因なら Cloud Run 環境変数または Secret 参照を直前の状態へ戻す
3. SES 認証・権限が原因なら trust policy、audience、role policy を最小範囲で戻す
4. 原因切り分け中に OTP / PIN のセキュリティ条件を緩めない

利用者向けには詳細な内部理由を出さず、影響範囲と復旧見込みだけを伝えます。

### 5. 記録

必ず残す項目:

- 発生日時
- 検知経路
- 影響 delivery / 影響ユーザー範囲
- 直近変更
- Cloud Run revision
- Cloud Logging の該当イベント名
- SES account / identity 状態
- 暫定対応
- 復旧日時
- 恒久対応要否

## Critical 通知テンプレート

初報:

```text
[critical] ICE Report Generator OTP送信停止の疑い

発生時刻:
検知経路:
影響範囲:
現在の状態:
確認中の項目:
一次対応者:
次アクション:
```

追加報:

```text
[update] ICE Report Generator OTP送信停止 調査状況

更新時刻:
判明したこと:
除外できた原因:
残っている原因候補:
暫定対応:
次回更新予定:
```

復旧報:

```text
[resolved] ICE Report Generator OTP送信停止 復旧

復旧時刻:
原因:
実施した対応:
影響範囲:
再発防止:
恒久対応チケット:
```

## 恒久対応起票テンプレート

```text
件名:

## 事象

## 影響範囲

## 発生から復旧までの時系列

## 暫定対応

## 原因

## 再発防止策

## セキュリティ確認

## リリース前確認

## 完了条件
```

## Rollback

直前の正常 revision へ traffic を戻す場合:

```powershell
gcloud.cmd run services update-traffic report-generator `
  --project=ice-sh `
  --region=asia-northeast1 `
  --to-revisions=<stable-revision>=100 `
  --impersonate-service-account=ice-deployer@ice-sh.iam.gserviceaccount.com `
  --quiet
```

provider を一時的に `logging` へ戻す場合:

```powershell
gcloud.cmd run services update report-generator `
  --project=ice-sh `
  --region=asia-northeast1 `
  --update-env-vars=MAIL_PROVIDER=logging `
  --impersonate-service-account=ice-deployer@ice-sh.iam.gserviceaccount.com `
  --quiet
```

rollback 後は `/api-health`、`/admin`、必要最小限の OTP smoke、Cloud Logging を確認します。

## セキュリティ注意

- Access Key、Secret、Admin Key、PIN、token、生メールアドレスをログやチケットに残さない
- Cloud Logging の調査では `token_hash`、`email_hash`、`recipient_hash` を使う
- 画面共有や IDE 文脈に credential 断片が出た場合は、安全側でローテーション対象にする
- OTP / PIN の検証条件は暫定復旧でも緩めない
- 作業用 AWS credential は必要時だけ発行し、確認後に無効化する
