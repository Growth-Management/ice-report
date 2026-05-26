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

### 配布一覧の視覚確認

headless Chrome が使える環境では、補助スクリプトで管理画面のスクリーンショットを取得します。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\capture-admin-deliveries.ps1
```

生成物は `artifacts/` 配下に出ます。`artifacts/` は git 管理しません。

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
