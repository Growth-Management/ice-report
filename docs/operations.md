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
alert発火履歴とthreshold変更要否を確認する場合は、`docs/monitoring.md` の
Noise review に従います。

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

#### Admin audit log検索view

定期確認や月次作業後の監査確認では、Cloud Logging の概要件数を一次確認に
使います。Firestore `admin_audit_logs` は、対象 delivery_id、target_id、
失敗理由、時系列を追加調査するときに参照します。

操作別の件数確認:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-audit-logs.ps1
```

JSONで保存する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-audit-logs.ps1 -AsJson |
  Set-Content -Encoding UTF8 artifacts\admin-audit-log-review.json
```

この script は action別件数に加えて、直近の audit failure と
`admin_auth_failed` security event を安全な項目だけに抽出します。raw log
payloadはNotionへ転記せず、`action`、`result`、`targetType`、`targetId`、
`statusCode`、`reason`、件数、対象期間だけを使います。直近失敗の確認数を
変える場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-audit-logs.ps1 `
  -RecentFailureLimit 20
```

定期 read-only operational check では、`scripts\run-operations-readonly-scheduled.ps1`
がこの audit review も同時に実行し、同じ artifact folder に保存します。
一時的に除外する場合だけ `-SkipAdminAuditReview` を指定します。

確認単位:

| 観点 | 主な確認先 | Notion転記 |
| --- | --- | --- |
| action別件数 | Cloud Logging `ICE_REPORT_ADMIN_AUDIT` | 件数と対象期間 |
| failure件数 | Cloud Logging `result=failure` | action、件数、reason概要 |
| admin認証失敗 | Cloud Logging / `security_events` | 件数、想定内外、対応結果 |
| 対象delivery | Firestore `admin_audit_logs.target_id` | delivery_id、操作、結果 |
| 詳細調査 | Firestore `admin_audit_logs.detail` | 必要な業務キーのみ |

Notionへ転記する項目:

- 確認日時、確認者、対象期間
- Cloud Run service、latest ready revision、対象action
- action別 total / success / failure 件数
- failure がある場合の reason 概要と一次対応
- 対象 delivery_id / target_id
- 関連PR、deploy、月次作業、incident との対応関係

Notion、Slack、GitHub、スクリーンショットへ転記しない項目:

- Admin key fingerprint
- credential、secret値、access key
- PIN、token、signed URL の token 断片
- 生メールアドレス
- message body、provider event JSON
- IP address、user agent

監査確認の目安:

- 月次作業後: `delivery_create`、`delivery_version_add`、`cleanup_expired_deliveries`
- 緊急停止後: `delivery_disable`、`delivery_enable`
- break-glass利用後: `admin_auth` failure、対象操作、runtime ERROR
- 定期read-only check後: audit件数が想定外に増えていないこと

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

通常は helper script を使います。default は dry-run で、Secret Manager へ
書き込みません。

dry-run / 現行key確認:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\rotate-admin-key.ps1
```

新しい Secret version を追加する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\rotate-admin-key.ps1 `
  -Execute `
  -PromptForNewKey `
  -SkipHttpVerification
```

`-Execute` では新しい Admin key を対話入力します。key はファイル、artifact、
Notion、ログ、チャットへ残しません。Secret version 追加後は、通常の deploy
手順で新しい Cloud Run revision を作成し、`ADMIN_API_KEY` が新versionで
読み込まれる状態にします。

deploy後の確認:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\rotate-admin-key.ps1 `
  -VerifyOnly `
  -PromptForOldKey
```

`-PromptForOldKey` は旧keyが `401` になることを確認するための任意確認です。
旧keyも値を出力・保存しません。

手動で Secret version を追加する場合:

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

deploy後は次を確認します。

- 無認証の管理 API が `401`
- 新key付きの `GET /deliveries?limit=1` が `200`
- 古いkeyが `401`
- `admin_auth_failed` が想定外に増えていない
- runtime ERROR が増えていない

### Admin UI 中期移行方針

人間向け Admin UI は、中期的に Admin 専用 Cloud Run service + IAP へ分離します。

通常運用:

- 人間の管理画面利用は Admin 専用service + IAP を主経路にする
- script/API利用、障害時break-glass、migration中の緊急操作は `X-Admin-Key` を継続する
- `ADMIN_API_KEY` はSecret Managerで管理し、利用後はrotationを原則とする

IAP移行時の確認:

1. public service の `/api-health`、`/d/*`、OTP request / verify がIAP影響を受けない
2. Admin専用serviceの `/admin` はIAP許可user/groupだけが到達できる
3. IAP service agent に `roles/run.invoker` が付与されている
4. 管理者user/group に `roles/iap.httpsResourceAccessor` が付与されている
5. `X-Admin-Key` 付きのscript/API経路が必要範囲で継続する

IAP移行rollback:

- Admin専用serviceのtrafficを直前の正常revisionへ戻す
- IAP設定に問題がある場合はAdmin専用service側だけで切り戻し、public serviceへ影響を広げない
- 緊急操作が必要な場合はbreak-glassとして `X-Admin-Key` を使い、操作後にAdmin key rotationを行う

### 配布一覧の視覚確認

headless Chrome または Edge が使える環境では、補助スクリプトで管理画面のスクリーンショットを取得します。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\capture-admin-deliveries.ps1
```

生成物は `artifacts/` 配下に出ます。`artifacts/` は git 管理しません。確認観点は次です。

- desktop と mobile の両方で配布一覧 table が表示される
- 横スクロールが必要な項目は `.table-wrap` 内に収まり、body 全体を押し広げない
- 配布URLとGCS URIは長い文字列でも表示が破綻しない
- screenshot には Admin key、PIN、生メールアドレス、token を転記しない

### Read-only operational check

日常確認、deploy後smoke、Notionへの結果記録補助には read-only script を使います。管理 API は `GET` のみ、Cloud Logging / Cloud Run / Cloud Monitoring は読み取りのみです。Admin key は Secret Manager から取得しますが、出力には含めません。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-operations-readonly.ps1
```

JSONで保存する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-operations-readonly.ps1 -AsJson |
  Set-Content -Encoding UTF8 artifacts\operations-readonly-check.json
```

管理画面スクリーンショットも同時に取得する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-operations-readonly.ps1 -CaptureScreenshots
```

確認対象:

- `/api-health`
- `/admin`
- `/deliveries?limit=<n>` の無認証、誤Admin key、正Admin key
- `/download-logs?limit=<n>` の正Admin key
- Cloud Run latest ready revision、image、traffic
- latest revision の ERROR logs
- admin audit、admin auth failure、mail attempt、OTP sent の Cloud Logging count
- `/api-health` uptime check と alert policy の存在

Notion へ貼る場合は、script の `notionSummary` を使います。API応答本文、Admin key、PIN、生メールアドレス、token は貼りません。

### Read-only operational check 定期実行

定期実行は週1回を目安にします。GitHub Actions の
`Operations Read-Only Check` workflow は毎週月曜 10:00 JST に実行し、
deployを伴わずに read-only check artifact を保存します。Cloud Run deploy
workflow では、deploy後に同じ wrapper を自動実行し、artifactを保存します。

標準頻度:

- 定期: 毎週月曜 10:00 JST 目安
- deploy後: Cloud Run traffic切替後に1回
- incident後: 復旧確認後に1回

GitHub Actions:

- `.github/workflows/operations-readonly-check.yml`: 週次または手動のread-only確認
- `.github/workflows/deploy-cloud-run.yml`: deploy後のread-only確認
- workflow timeout: 20分。複数回連続して timeout する場合のみ見直す

定期実行向け wrapper:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1
```

この wrapper は次を `artifacts/operations-readonly/` へ保存します。
`artifacts/` は git 管理しません。

- `operations-readonly-check-<timestamp>.json`
- `operations-readonly-check-<timestamp>-summary.txt`
- `operations-readonly-run-metadata-<timestamp>.json`
- `admin-audit-log-review-<timestamp>.json`
- `admin-audit-log-review-<timestamp>-summary.txt`
- `admin-iap-readonly-check-<timestamp>.json`
- `admin-iap-readonly-check-<timestamp>-summary.txt`
- `docs-legacy-reference-check-<timestamp>.json`
- `docs-legacy-reference-check-<timestamp>-summary.txt`
- `monitoring-noise-review-<timestamp>.json`
- `monitoring-noise-review-<timestamp>-summary.txt`
- `secret-exposure-metadata-<timestamp>.json`
- `secret-exposure-metadata-<timestamp>-summary.txt`

GitHub Actions では同じ内容を `operations-readonly-check-<run_id>` artifact
として保存します。workflow上でcheckが失敗した場合も、失敗内容を確認できる
ようartifact upload stepは `always()` で実行します。

#### 週次 read-only check 3回レビュー

Phase 8 では、`Operations Read-Only Check` workflow の週次実行結果を3回分
確認してから、timeout、権限、monitoring threshold の変更要否を判断します。
単発の失敗や遅延だけでは設定変更しません。

確認コマンド:

```powershell
gh run list --workflow "Operations Read-Only Check" --limit 10 `
  --json databaseId,status,conclusion,createdAt,updatedAt,event,url
```

記録テンプレート:

```text
ICE Report Generator 週次 read-only check 3回レビュー

review date:
reviewer:
workflow: Operations Read-Only Check
対象期間:

run 1:
- run URL:
- createdAt:
- conclusion:
- duration:
- artifact:
- operationsExitCode:
- auditExitCode:
- adminIapExitCode:
- docLegacyExitCode:
- monitoringExitCode:
- repoHygieneExitCode:
- failedChecks:
- monitoringThresholdChangeRecommended:
- monitoringChannelSplitRecommended:

run 2:
- run URL:
- createdAt:
- conclusion:
- duration:
- artifact:
- failedChecks:

run 3:
- run URL:
- createdAt:
- conclusion:
- duration:
- artifact:
- failedChecks:

判断:
- timeout変更要否:
- GCP / GitHub Actions権限変更要否:
- monitoring threshold変更要否:
- warning channel新設要否:
- Notion / roadmap更新要否:

次アクション:
```

3回レビューの判断条件:

- 3回すべて `conclusion=success` かつ wrapper metadata `passed=true` なら、
  workflow timeout、権限、threshold は変更しない
- 同じ exit code または `failedChecks` が2回以上続く場合は、失敗箇所を
  切り分けて別PRで修正する
- `durationSeconds` が2回以上 900秒を超える場合は、timeoutではなく
  時間を使っているcheckを先に分離する
- `monitoringThresholdChangeRecommended=true` または
  `monitoringChannelSplitRecommended=true` が出た場合も、incident実績と
  alert発火履歴を確認してから判断する
- artifact がない run は判定不能として扱い、workflow設定またはartifact uploadを
  先に修正する

`operations-readonly-run-metadata-<timestamp>.json` には、wrapper全体の
`durationSeconds`、`exitCode`、`operationsExitCode`、`operationsFailureReason`、
`auditExitCode`、`adminIapExitCode`、`adminIapFailureReason`、`failedChecks`、
Admin audit review の成否、Admin IAP drift check の成否、docs legacy reference
check の成否、monitoring noise review の成否と warning / critical signal 件数、
repo hygiene metadata review の成否と現在metadata上の要対応有無を記録します。
Notion直接記録を使う場合は、`notionRunKey`、`notionPreviewPath`、
`notionSafetyViolations`、`notionRecorded`、`notionRecordError`、
`notionWrite.skipped` も確認します。
pipeline上で失敗した場合は、まずこの metadata と summary text を見て、
失敗箇所と所要時間を確認します。

read-only operational check または Admin IAP drift check が権限不足などで
JSONを出せずに終了した場合も、wrapper は失敗用 JSON と summary text を
artifactへ保存します。`permission_denied`、`resource_not_found`、
`unauthenticated` などの failure reason を metadata と summary で確認します。

Admin IAP drift check では、`report-generator-admin` のIAP policy、
Cloud Run invoker、`ADMIN_IAP_ALLOWED_EMAILS` が期待userと一致していることを
確認します。複数管理者を確認する場合は、wrapperへ期待userを渡します。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -ExpectedIapUsers sinohara@impress.co.jp,admin2@example.com
```

一時的にAdmin IAP確認だけを外す場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -SkipAdminIapReview
```

docs legacy reference check は、非推奨の環境変数・認証方式・復旧記述の
想定外混入を確認します。一時的にこの確認だけを外す場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -SkipDocLegacyReview
```

monitoring noise review は、warning / critical alert の件数と threshold 変更要否を
確認します。一時的にこの確認だけを外す場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -SkipMonitoringReview
```

repo hygiene metadata review は、secret値やローカルファイル内容を読まずに、
tracked sensitive path、Cloud Run env、Secret Manager version metadata を確認します。
一時的にこの確認だけを外す場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -SkipRepoHygieneReview
```

Notion へ転記する場合は summary text のみを使います。JSON artifact はローカル
確認用とし、API応答本文、Admin key、PIN、生メールアドレス、token を
チケットやチャットへ貼り付けません。

Notion API へ直接追記する場合:

```powershell
$env:NOTION_READONLY_CHECK_PAGE_ID = '<NOTION_PAGE_ID>'
$env:NOTION_API_TOKEN_SECRET_NAME = '<SECRET_NAME_CONTAINING_NOTION_API_TOKEN>'
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -RecordToNotion
```

Notionへ書き込まずに送信予定内容だけを確認する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -PreviewNotion
```

この場合、`notion-readonly-record-preview-<timestamp>.txt` を
`artifacts/operations-readonly/` に保存し、Notion API token は要求しません。

重複記録を避ける場合は、実行単位で安定した run key を指定します。
GitHub Actions では `GITHUB_RUN_ID` を既定値として使います。ローカル実行では
`local-<timestamp>` を使います。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1 `
  -RecordToNotion `
  -NotionRunKey github-run-<RUN_ID>
```

同じ run key が追記先ページに既に存在する場合、既定では重複appendせず
`notionWrite.skipped=true` として metadata に記録します。明示的に重複を許す場合だけ
`-AllowDuplicateNotionRecord` を指定します。

または、実行環境だけに `NOTION_API_TOKEN` を設定して実行します。token値は
ローカルファイル、artifact、Notion本文、ログ、チャットへ残しません。

Notion直接記録の前提:

- Notion integration が対象ページへ共有されている
- integration に insert content 権限がある
- `NOTION_READONLY_CHECK_PAGE_ID` は追記先ページのIDを指定する
- Notion API token は `NOTION_API_TOKEN` または Secret Manager secret から読む
- Notion API version は wrapper 既定の `2026-03-11` を使う
- 追記する内容は `notionSummary` とローカルartifact pathだけに限定する
- Notionへ送る直前にメールアドレス、Slack webhook URL、AWS credential-like形式、
  bearer token、`X-Admin-Key`、signed URL 形式をredactし、残存していれば書き込みを止める
- Notion書き込みに失敗しても read-only check 本体の成否とは分離し、
  `notionRecordError` にredact済み理由を残す

失敗時対応:

1. wrapper の `passed=false` または exit code 1 を確認する
2. `operations-readonly-run-metadata-<timestamp>.json` の `durationSeconds`、
   `operationsExitCode`、`auditExitCode`、`adminIapExitCode`、
   `docLegacyExitCode`、`monitoringExitCode`、`repoHygieneExitCode`、
   `failedChecks` を確認する
3. summary text の `Failed checks` を確認する
4. `/api-health`、Cloud Run latest ready revision、runtime ERROR log を優先確認する
5. Admin認証失敗が増えているだけの場合は、誤key check由来かを確認する
6. Admin IAP drift check が失敗した場合は、IAP accessor、Cloud Run invoker、
   `ADMIN_IAP_ALLOWED_EMAILS` の差分を確認し、public serviceへIAP設定が
   広がっていないことを確認する
7. docs legacy reference check が失敗した場合は、`unexpectedMatches` のpath/lineを
   確認し、非推奨構成に戻す実行手順なら修正する
8. monitoring noise review が失敗した場合は、Cloud Logging read 権限と
   対象 filter を確認する。件数が増えているだけなら、incident化有無と
   deploy / 手動smokeとの対応を確認してから threshold 変更要否を判断する
9. repo hygiene metadata review が失敗した場合は、
   `repoHygieneRewriteRequiredByCurrentMetadata` と count項目を確認し、
   現在HEADのtracking、Cloud Run env、非推奨secret残存のどれが原因かを切り分ける
10. 利用者影響がある場合は本 playbook の一次対応または rollback 手順へ進む
11. Notion には summary、failedChecks、durationSeconds、確認者、確認日時、一次対応結果を記録する

2026-06-29 初回手動 run 記録:

- workflow run: `28340664457`
- event: `workflow_dispatch`
- conclusion: `failure`
- duration: 約1分40秒
- artifact: `operations-readonly-check-28340664457`
- 成功した確認:
  - Admin audit review
  - docs legacy reference check
  - monitoring noise review
  - repo hygiene metadata review
- 失敗した確認:
  - read-only operational check
  - Admin IAP drift check
- 原因:
  - GitHub Actions 実行主体に `monitoring.uptimeCheckConfigs.get` が不足
  - GitHub Actions 実行主体に `iap.web.getIamPolicy` が不足
- 判断:
  - timeout変更は不要
  - monitoring threshold変更は不要
  - GitHub Actions 実行主体のread権限追加後に再実行する
  - この失敗runは「3回成功レビュー」には数えず、権限不足切り分け実績として扱う

2026-06-29 read-only 権限追加:

- GitHub Actions 実行主体: `ice-deployer@ice-sh.iam.gserviceaccount.com`
- 追加済み role:
  - `roles/monitoring.viewer`
  - `projects/ice-sh/roles/iceReportIapPolicyViewer`
- custom role `iceReportIapPolicyViewer` は IAP IAM policy の read-only 確認だけに使う
- 含める permission:
  - `iap.web.getIamPolicy`
  - `iap.webTypes.getIamPolicy`
  - `iap.webServices.getIamPolicy`
  - `iap.webServiceVersions.getIamPolicy`
  - `iap.tunnel.getIamPolicy`
  - `iap.tunnelZones.getIamPolicy`
  - `iap.tunnelInstances.getIamPolicy`
  - `iap.tunnelLocations.getIamPolicy`
  - `iap.tunnelDestGroups.getIamPolicy`
- 含めない permission:
  - `*.setIamPolicy`

2026-06-29 2回目手動 run 記録:

- workflow run: `28351790564`
- event: `workflow_dispatch`
- conclusion: `failure`
- artifact: `operations-readonly-check-28351790564`
- 成功した確認:
  - read-only operational check
  - Admin audit review
  - docs legacy reference check
  - monitoring noise review
  - repo hygiene metadata review
- 失敗した確認:
  - Admin IAP drift check
- 原因:
  - Ubuntu runner 上で `scripts/check-admin-iap-readonly.ps1` が Windows 固定の `curl.exe` を呼んでいた
- 判断:
  - IAM 追加は期待通り有効
  - script を `curl.exe` / `curl` 両対応に修正して再実行する
  - この失敗runは「3回成功レビュー」には数えない

2026-06-29 3回目手動 run 記録:

- workflow run: `28352173345`
- event: `workflow_dispatch`
- conclusion: `failure`
- artifact: `operations-readonly-check-28352173345`
- 成功した確認:
  - read-only operational check
  - Admin audit review
  - docs legacy reference check
  - monitoring noise review
  - repo hygiene metadata review
- 失敗した確認:
  - Admin IAP drift check
- 原因:
  - Ubuntu runner 上で `Get-Command curl` が複数候補を返し、実行パスが結合された
- 判断:
  - `curl.exe` / `curl` 解決時に最初の application だけを使う
  - この失敗runは「3回成功レビュー」には数えない

2026-06-29 4回目手動 run 記録:

- workflow run: `28352409745`
- event: `workflow_dispatch`
- conclusion: `success`
- duration: 約1分33秒
- artifact: `operations-readonly-check-28352409745`
- 成功した確認:
  - read-only operational check
  - Admin IAP drift check
  - Admin audit review
  - docs legacy reference check
  - monitoring noise review
  - repo hygiene metadata review
- metadata:
  - `operationsExitCode=0`
  - `adminIapExitCode=0`
  - `docLegacyExitCode=0`
  - `monitoringExitCode=0`
  - `repoHygieneExitCode=0`
  - `failedChecks` なし
- Admin IAP drift:
  - 未ログイン `/admin` は IAP 生成の `302` で Google login へ遷移
  - public `/api-health` は `200`
  - public `/admin` は `200`
  - Admin runtime ERROR は `0`
- 判断:
  - GitHub Actions 実行主体の read 権限と Linux runner 互換性は解消済み
  - この run を「3回成功レビュー」の 1/3 として数える

2026-06-30 5回目手動 run 記録:

- workflow run: `28408454687`
- event: `workflow_dispatch`
- conclusion: `success`
- duration: 約1分59秒
- artifact: `operations-readonly-check-28408454687`
- 成功した確認:
  - read-only operational check
  - Admin IAP drift check
  - Admin audit review
  - docs legacy reference check
  - monitoring noise review
  - repo hygiene metadata review
- metadata:
  - `operationsExitCode=0`
  - `adminIapExitCode=0`
  - `docLegacyExitCode=0`
  - `monitoringExitCode=0`
  - `repoHygieneExitCode=0`
  - `failedChecks` なし
- Admin IAP drift:
  - 未ログイン `/admin` は IAP 生成の `302` で Google login へ遷移
  - public `/api-health` は `200`
  - public `/admin` は `200`
  - Admin runtime ERROR は `0`
- 判断:
  - この run を「3回成功レビュー」の 2/3 として数える
  - `notionWrite=null` のため、今回も Notion へは手動で補完記録する

2026-06-30 6回目手動 run 記録:

- workflow run: `28409091044`
- event: `workflow_dispatch`
- conclusion: `success`
- duration: 約1分51秒
- artifact: `operations-readonly-check-28409091044`
- 成功した確認:
  - read-only operational check
  - Admin IAP drift check
  - Admin audit review
  - docs legacy reference check
  - monitoring noise review
  - repo hygiene metadata review
- metadata:
  - `operationsExitCode=0`
  - `adminIapExitCode=0`
  - `docLegacyExitCode=0`
  - `monitoringExitCode=0`
  - `repoHygieneExitCode=0`
  - `failedChecks` なし
- Admin IAP drift:
  - 未ログイン `/admin` は IAP 生成の `302` で Google login へ遷移
  - public `/api-health` は `200`
  - public `/admin` は `200`
  - Admin runtime ERROR は `0`
- 判断:
  - この run を「3回成功レビュー」の 3/3 として数える
  - 3回成功レビューの結果、workflow timeout / 権限 / monitoring threshold の追加変更は不要
  - `notionWrite=null` のため、今回も Notion へは手動で補完記録する

所要時間の扱い:

- 単発の遅延だけではthresholdやworkflowを変更しない
- 複数回連続して `durationSeconds` が大きく伸びる場合は、Cloud Logging read、
  Cloud Monitoring read、Admin API read、Admin audit review のどこで時間を
  使っているかを切り分ける
- GitHub Actionsのtimeoutや権限を変更する場合は、失敗artifactと変更理由を
  Notionへ記録してから別PRで扱う

定期実行の前提:

- `gcloud` が対象project `ice-sh` を読める
- `report-generator-admin-api-key` の Secret Manager read 権限がある
- Cloud Run / Cloud Logging / Cloud Monitoring の read 権限がある
- Admin audit log review のため Cloud Logging read 権限がある
- GitHub Actionsで実行する場合は、deploy用workload identityのservice accountに
  上記read権限と `report-generator-admin-api-key` のSecret Manager read権限がある
- `Operations Read-Only Check` の GitHub Actions 実行主体には、少なくとも次の
  確認が通る権限が必要です
  - Cloud Run service describe と IAM policy read
  - Cloud Logging read
  - Secret Manager latest version access for `report-generator-admin-api-key`
  - Cloud Monitoring uptime check / alert policy read
  - IAP web IAM policy read for `report-generator-admin`
- Cloud Monitoring は `roles/monitoring.viewer` で
  `monitoring.uptimeCheckConfigs.get` と `monitoring.alertPolicies.get` を満たします
- IAP policy read は `projects/ice-sh/roles/iceReportIapPolicyViewer` を使います。
  この custom role は `getIamPolicy` 系 permission のみに限定し、`setIamPolicy`
  は含めません
- 実行環境に保存される artifact はローカル運用記録として扱い、git commitしない

## 月次運用

月次レポート作成、配布URL発行、期限切れ整理、version追加を行うときの基準です。管理画面で操作する場合も、APIで操作する場合もこの順序を正とします。

### 月次作成前確認

1. 対象月を `YYYY-MM` で確定する。未指定でレポート生成すると、実行日の前月が対象月になります。
2. 顧客名、許可メール、許可ドメインを確認する。許可宛先が空欄の場合は既定ドメインが使われます。
3. 既存GCSファイルを使うか、BigQueryを再実行して新規生成するかを決める。
4. 生成時の保存先は `gs://ice-report-files/reports/plus/<yymm>/...xlsx` を基本にする。
5. 送付前に配布URL、対象月、顧客名、current version、期限、GCS URI を確認する。

GCS URI を空欄にして配布作成すると、backend が BigQuery を再実行して Excel を生成し、GCS へ保存してから delivery を作成します。既に検品済みの Excel を配布する場合は、GCS URI を明示します。

### 複数レポート運用 baseline

複数レポートを扱う場合は、delivery 作成前にレポート単位の baseline を確認します。
同じ月次作業でも、保存先、許可宛先、backup 先、共有文面、保持判断は
レポートごとに変わる前提です。

レポート別に固定して記録する項目:

| 項目 | 記録内容 |
| --- | --- |
| report name | 運用上のレポート名 |
| customer / recipient group | 顧客名、配布先グループ、許可domain |
| target month | `YYYY-MM` |
| GCS prefix | 生成・保管する GCS path prefix |
| Drive folder | レポート別の保存先 folder URL |
| operational owner | 所管部署 |
| primary operator | 主担当者 |
| backup requirement | overwrite / cleanup 前の backup 必須条件 |
| delivery policy | 有効期限、再送、停止判断 |
| verification | 作成後に確認する Admin audit、Slack通知、DLログ |

現在の baseline:

| report | GCS prefix | Drive folder | owner | primary operator | note |
| --- | --- | --- | --- | --- | --- |
| OMFダウンロード数報告 | `gs://ice-report-files/reports/plus/<yymm>/` | `https://drive.google.com/drive/folders/126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt` | システム管理室 | 篠原邦昭 | レポートごとに保存先は変更される前提。全レポート共通の固定保存先として扱わない |

新しいレポートを追加する場合は、最初の delivery 作成前に上記項目を
Notionまたは運用記録へ登録します。Drive folder が未確定のまま overwrite、
GCS cleanup、Firestore record削除を進めません。

月次作業時の最低確認:

1. 対象レポートの baseline が存在する
2. 対象月、顧客名、許可宛先、許可domain が対象レポートの運用条件と一致する
3. GCS prefix と出力ファイル名が対象レポートの命名規則に合っている
4. Drive folder URL、operational owner、primary operator が記録済み
5. delivery 作成後に `delivery_create` の Admin audit、Slack通知、配布一覧を確認する
6. version追加または停止/再有効化を行った場合は、対象操作の Admin audit と
   DLログ表示確認を運用記録へ残す

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
3. 上書き前の Excel を対象レポートの Drive 保存先へ backup 済み
4. 同じファイル名を維持する理由がある
5. version note に `overwrite`、実施理由、確認者を残す

実行後は、current version が増えていること、GCS URI、ファイル更新時刻、Slack通知、`ICE_REPORT_ADMIN_AUDIT action=delivery_version_add result=success` を確認します。利用者影響がある差し替えでは、必要に応じて許可済みメールアドレスで OTP からダウンロードまで確認します。

### 月次 Admin UI human smoke 記録テンプレート

許可userが Admin専用service + IAP で月次操作を行った場合は、操作結果を
次の粒度で運用記録へ残します。メールアドレス実値、Admin key、PIN、token、
signed URL の token 断片、message body は記録しません。

```text
ICE Report Generator 月次 Admin UI smoke

実施日時:
実施者:
Admin経路: report-generator-admin + IAP / break-glass X-Admin-Key
対象report:
target month:
customer / recipient group:
delivery_id:

baseline確認:
- report baseline exists:
- Drive folder URL recorded:
- operational owner:
- primary operator:

delivery create:
- 実施有無:
- result:
- current_version:
- GCS URI:
- public_download_url共有可否:
- Slack通知確認:
- Admin audit `delivery_create`:

version add:
- 実施有無:
- result:
- overwrite: yes / no
- old version:
- new version:
- new GCS URI:
- Drive backup確認:
- Admin audit `delivery_version_add`:

disable / enable:
- 実施有無:
- disable result:
- enable result:
- active final state:
- Admin audit `delivery_disable`:
- Admin audit `delivery_enable`:

DLログ確認:
- `/download-logs` または管理画面DLログ表示:
- 対象deliveryのログ件数:
- 問い合わせ / incident 対応中の有無:

post-check:
- read-only operational check 実施有無:
- Admin audit summary確認:
- runtime ERROR有無:
- Notion / 運用記録URL:

記録しない項目:
- Admin key、PIN、token、signed URL token断片
- raw recipient email
- message body、provider event JSON
```

人間向け smoke で確認する最小セット:

1. Admin UI が IAP 許可userで表示できる
2. 対象 delivery が配布一覧で確認できる
3. 必要な場合のみ delivery create を行い、`delivery_create` audit を確認する
4. 必要な場合のみ version add を行い、`delivery_version_add` audit と Drive backup を確認する
5. 停止/再有効化を行った場合は、最終状態が意図どおりであることを確認する
6. DLログ表示で対象 delivery のログ件数を確認する
7. 操作後に read-only operational check または Admin audit log review を確認する

### Phase 9 Admin UI expansion smoke

Phase 9 の管理画面拡張は、表示、read-only、versioning、preview/dry-run、
publish、rollback、automation の順で段階的に進めます。初回実装では
利用者向けOTP画面の選択中レポート表示と、Admin UI の report definitions
read-only一覧だけを対象にします。

初回実装のdeploy要否:

- `app.py` / `distribution.py` の変更を含むため Cloud Run deploy が必要
- 人間向け確認は `report-generator-admin` + IAP を主経路にする
- public service 全体へ IAP は適用しない
- `X-Admin-Key` は script/API/break-glass 用に継続する

smoke:

1. `report-generator-admin` に IAP 許可userでログインできる
2. Admin UI のレポート定義一覧が表示される
3. レポート定義一覧に SQL、template mapping、allowed email、token、Signed URL が表示されない
4. 定義がある場合は version履歴を展開し、SQL本文、template mapping、作成者メール、Signed URL が表示されない
5. レポート定義の追加、編集、archive をテスト用定義で確認する。確認後は archive 状態にして残す
6. 追加・編集・archive の操作ログに secret、PIN、生メール、token断片、Admin key fingerprint、IP、user agent、Signed URL が含まれていない
7. Excelテンプレート `.xlsx` のpreviewを実行し、保存やpublishなしでシート名、行数、列数、テーブル数、サイズ、sha256だけが返ることを確認する
8. template preview の操作ログに secret、PIN、生メール、token断片、Admin key fingerprint、IP、user agent、Signed URL、セル値が含まれていない
9. Excelテンプレート `.xlsx` のpublishをテスト用定義で実行し、管理用GCS prefixへの保存、version追加、current_version更新を確認する
10. template rollbackをテスト用定義で実行し、既存versionへcurrent_versionだけが戻ることを確認する
11. template publish / rollback の操作ログに secret、PIN、生メール、token断片、Admin key fingerprint、IP、user agent、Signed URL、セル値、GCS URI が含まれていない
12. 既存の配布一覧、最新GCSファイル一覧、DLログが従来どおり表示される
13. 有効な配布URLでOTP画面を開き、選択中レポートの顧客、対象月、current version、file、期限、状態が表示される
14. PIN発行、PIN検証、download redirect の既存フローが変わっていない

rollback:

- 表示のみの問題であれば直前 Cloud Run revision へ戻す
- SQL変更、template mapping変更、生成処理へのtemplate適用、schedule変更、Drive/GCS保存先変更はこの段階では行わない
- PR単位で戻す場合は該当PRをrevertする

記録しない項目:

- secret、PIN、生メール、token断片、Admin key fingerprint、IP、user agent、Signed URL、template GCS URI
- SQL本文、template mapping詳細、Excelセル値、message body、provider event JSON

### Google Drive backup 後の GCS cleanup 方針

GCS object の削除は破壊的操作のため、通常の月次手順では実行しません。保持期間と承認条件は `docs/security.md` の Archive Lifecycle Management を正とします。現時点の方針は次です。

- active delivery の current version が参照する GCS object は削除しない
- 期限切れ delivery でも、問い合わせ対応や再送に備えて `expires_at` から 180 日、かつ対象月から 13 か月の遅い方まで保持する
- GCS削除候補にする前に、対象レポートの Drive 保存先へ Excel backup を保存する
- backup ファイル名には顧客名、対象月、delivery_id、version、元GCS URIが分かる情報を含める
- 削除候補は Notion に記録し、明示承認後にだけ削除する

#### Retention review

候補抽出は read-only dry-run の補助 script を使います。この script は
Firestore record や GCS object を削除しません。

```powershell
python scripts\check-retention-candidates.py
```

GCS object の存在確認まで含める場合:

```powershell
python scripts\check-retention-candidates.py --check-gcs-exists
```

GCS report object だけを確認する場合:

```powershell
python scripts\check-retention-candidates.py --scope gcs --check-gcs-exists
```

出力には secret 値、token、PIN、生メールアドレス、message body、
provider event JSON を含めません。GCS / Firestore の実削除は、この候補
抽出結果を Notion に記録し、明示承認を得てから別手順で実施します。

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

#### Drive backup retention運用確認

GCS report object を削除候補にする前に、Google Drive backup を個別ファイル
単位で確認します。対象 backup が対象レポートの Drive 保存先配下で確認できない、または
対象ファイルのURLを確認できない場合は GCS object を削除しません。

現在のレポート保存先:

- report: `OMFダウンロード数報告`
- path: `99_共有用 / OMFダウンロード数報告`
- folder URL: `https://drive.google.com/drive/folders/126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt`
- operational owner: システム管理室
- primary operator: 篠原邦昭

Drive metadata から owner email を取得できない場合でも、運用上の削除承認では
対象レポートの保存先フォルダURL、operational owner、primary operator を
記録します。レポートごとに格納箇所は変更する前提のため、上記URLを全レポート
共通の固定保存先として扱いません。対象 backup が対象レポートの保存先配下に
ない場合は例外扱いとし、個別 backup URL、閲覧確認者、確認日時、例外理由を
削除承認記録へ残します。

確認する項目:

- backup URL が Google Drive 上の対象 Excel file を指している
- backup file name に顧客名、対象月、delivery_id、version、元GCS URIを追跡
  できる情報がある
- backup file の閲覧権限がシステム管理室の運用担当者で確認できる
- Drive backup の保持目安が対象月から7年であることを削除承認記録に残す
- 対象レポートの保存先 folder URL、operational owner、primary operator を
  削除承認記録へ転記する
- 対象レポートの保存先配下ではない backup は、個別backup URLを必須項目として扱う

Drive検索で対象 backup を対象レポートの保存先配下に確認できない場合:

1. GCS削除は実施しない
2. Notion削除承認に「Drive backup レポート別保存先配下未確認」と記録する
3. 個別 backup URL、file name、閲覧確認者、確認日時を記録する
4. レポートの保存先を変更する場合は、対象レポート単位で folder URL、
   operational owner、共有範囲、保持方針を更新する

#### Drive backup 記録テンプレート

月次 delivery 作成、version追加、overwrite、削除承認前確認で Drive backup を
確認した場合は、削除予定がなくても次を運用記録へ残します。

```text
ICE Report Generator Drive backup確認

report name:
target month:
customer / recipient group:
delivery_id:
version:
GCS URI:
file name:

Drive folder URL:
Drive folder path:
backup file URL:
backup file name:
backup location status: report-folder / individual-url / not-found

operational owner:
primary operator:
backup確認者:
確認日時:

確認結果:
例外理由:
次アクション:
```

`backup location status` の扱い:

- `report-folder`: 対象レポートの Drive 保存先配下で確認済み
- `individual-url`: 対象レポートの Drive 保存先配下ではないが、個別 backup URL を確認済み
- `not-found`: backup未確認。overwrite、GCS cleanup、Firestore record削除は進めない

現在の `OMFダウンロード数報告` は次を既定値として記録します。

- Drive folder URL: `https://drive.google.com/drive/folders/126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt`
- Drive folder path: `99_共有用 / OMFダウンロード数報告`
- operational owner: システム管理室
- primary operator: 篠原邦昭

#### GCS削除候補の確認

dry-run では `deliveries` metadata だけを読み、候補 GCS object の存在確認を
含めます。この手順は GCS object を削除しません。

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
python scripts\check-retention-candidates.py --scope gcs --check-gcs-exists |
  Tee-Object -FilePath artifacts\gcs-retention-candidates.json
```

確認する JSON 項目:

- `dryRun=true`
- `scope=gcs`
- `candidateCounts.gcs_report_objects`
- `candidates.gcs_report_objects[].gcs_uri`
- `candidates.gcs_report_objects[].delivery_id`
- `candidates.gcs_report_objects[].retain_until`
- `candidates.gcs_report_objects[].exists`
- `candidates.gcs_report_objects[].protected_by_active_delivery=false`

候補が出た場合の読み取り確認例:

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

Firestore record の削除は、GCS object 削除よりも調査影響が大きいため、
dry-run と Notion 承認を先に完了させます。実削除は承認済み manifest を
入力にした管理scriptだけで実行し、Firestore console での手動削除や ad hoc
script は使いません。

dry-run で候補だけを確認する場合:

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
python scripts\check-retention-candidates.py --scope firestore |
  Tee-Object -FilePath artifacts\firestore-retention-candidates.json
```

確認する JSON 項目:

- `dryRun=true`
- `scope=firestore`
- `candidateCounts.deliveries`
- `candidateCounts.download_logs`
- `candidateCounts.security_events`
- `candidateCounts.admin_audit_logs`
- `candidateCounts.otp_challenges`
- `candidateCounts.download_sessions`
- `candidates.*[].id`
- `candidates.*[].date_field`
- `candidates.*[].date`
- `candidates.*[].delivery_id`

Firestore record 候補に secret 値、token、PIN、生メールアドレス、message
body、provider event JSON が含まれていないことを確認します。incident対応中、
abuse調査中、顧客問い合わせ中、再送依頼中の delivery_id に紐づく record は
削除候補から外します。

削除候補にできる最短条件:

- `deliveries`: `active=false` かつ `expires_at` から 400 日経過
- `download_logs`: `created_at` から 400 日経過
- `security_events`: `created_at` から 400 日経過
- `admin_audit_logs`: `created_at` から 400 日経過
- `otp_challenges` / `download_sessions`: 期限切れから 30 日経過

承認済み manifest の例は
`docs/retention-firestore-deletion-manifest.example.json` を参照します。manifest
には collection、document id、削除理由だけを記録し、record本文、token、PIN、
生メールアドレス、message body、provider event JSON は含めません。

dry-run:

```powershell
python scripts\delete-firestore-retention-records.py `
  --manifest artifacts\firestore-retention-deletion-approved.json
```

実削除:

```powershell
python scripts\delete-firestore-retention-records.py `
  --manifest artifacts\firestore-retention-deletion-approved.json `
  --execute `
  --confirm DELETE_FIRESTORE_RETENTION_RECORDS
```

`deliveries` collection は高リスク扱いです。承認済みであっても、削除する場合は
追加で `--allow-deliveries` を指定し、active/current参照、問い合わせ、incident、
再送依頼がないことを再確認します。

script の安全条件:

- default は dry-run
- 実削除には `--execute` と確認文字列が必要
- 対象 collection は allowlist に限定
- document id に path separator を含めない
- 同一 target の重複を拒否
- default 最大件数は 50 件
- 出力には Firestore record本文や機微値を含めない

#### Notion削除承認テンプレート

詳細テンプレートは `docs/retention-deletion-approval-template.md` を正とします。
Notion へ貼り付ける際も、credential、token、PIN、生メールアドレス、message
body、provider event JSON は記録しません。

2026-06-22 時点では、削除承認テンプレートを専用 Notion DB へ移行せず、
本テンプレートによる手動記録を継続します。削除承認が継続的に発生する、
複数承認を横断検索する必要がある、または監査上の集計要件が出た場合に
DB化を再起票します。

```text
ICE Report Generator 削除承認

対象種別:
対象ID / GCS URI / collection:
対象月:
顧客名:
現在の active/current 参照確認:
保持期限を満たしている根拠:
Drive backup URL:
Drive backup report folder URL / owner: https://drive.google.com/drive/folders/126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt / システム管理室
Drive backup report folder path: 99_共有用 / OMFダウンロード数報告
Drive backup primary operator: 篠原邦昭
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

deploy後の読み取り確認は、必要に応じて read-only operational check に置き換えます。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-operations-readonly.ps1
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

## SES bounce / complaint warning response

SES bounce / complaint is an AWS-side warning signal. Do not add a Cloud Run
callback endpoint for SES notifications.

### Monitoring resources

| Item | Value |
| --- | --- |
| SNS topic | `arn:aws:sns:ap-northeast-1:855532282119:ice-report-ses-reputation-alerts` |
| Notification owner | ICE GM department mailing list |
| Notification endpoint | `info-ice-gm@impress.co.jp` |
| Bounce alarm | `ice-report-ses-bounce-rate-warning` |
| Complaint alarm | `ice-report-ses-complaint-rate-warning` |
| Missing data handling | `notBreaching` |

### 1. Initial triage

Confirm these items before changing runtime configuration:

- whether the event is bounce, complaint, or sender reputation warning
- affected time window
- affected SES identity: `ice-sv.jp`
- affected custom MAIL FROM: `bounce.ice-sv.jp`
- whether OTP delivery failures or `mail_provider_auth_failed` increased at the
  same time
- whether SES account status remains healthy

Cloud Logging check:

```powershell
gcloud.cmd logging read `
  'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND (textPayload:"ICE_REPORT_MAIL_DELIVERY_ATTEMPT" OR textPayload:"ICE_REPORT_SECURITY_EVENT type=otp_delivery_failed" OR textPayload:"mail_provider_auth_failed")' `
  --project=ice-sh `
  --freshness=120m `
  --limit=100 `
  --format='value(timestamp,textPayload)'
```

SES account / identity check:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-ses-web-identity.ps1
```

If a direct AWS profile is explicitly prepared for read-only confirmation:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-ses-direct.ps1 -Profile ice-report-ops
```

Do not recreate persistent AWS credentials only for routine investigation.

CloudWatch alarm / SNS subscription check:

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

### 2. Severity decision

Treat as warning when:

- bounce / complaint count increased, but OTP delivery and `/api-health` remain
  normal
- SES account remains `HEALTHY`
- sending is still enabled

Escalate to critical OTP delivery incident when:

- OTP delivery failures increase
- SES account status is not healthy
- SES sending is paused or enforcement indicates account-level risk
- multiple recipients report no OTP arrival

### 3. Containment

For warning-level cases:

1. Identify the affected delivery or recipient group using hashes or aggregate
   counts only.
2. Disable or narrow the affected delivery if a specific list is causing
   repeated bounces.
3. Stop any repeated manual resend that is increasing complaint risk.
4. Keep the public download and OTP security controls unchanged.

For critical cases, follow the OTP delivery incident response in this playbook.

### 4. Recording rules

Record these fields in Notion:

- event type: bounce / complaint / reputation warning
- detected source: SES notification / CloudWatch alarm / manual AWS console
  check
- time window
- aggregate count
- affected delivery_id, if known
- Cloud Run revision
- SES account / identity status
- containment action
- whether rollback or provider change was required

Do not record raw recipient email, MIME payload, message body, provider event
JSON, PIN, token, or AWS credential values.

## /api-health uptime alert 一次対応

Cloud Monitoring の `ICE Report Generator - api-health uptime failure` が発火した場合は、外形監視で `/api-health` が失敗しています。利用者影響の有無を最初に切り分けます。

確認順:

1. `/api-health` を直接確認する
2. Cloud Run の latest ready revision と traffic 100% revision を確認する
3. 直近 deploy、Secret 更新、Cloud Run 環境変数変更の有無を確認する
4. 対象 revision の ERROR log と起動失敗 log を確認する
5. 直前の正常 revision へ rollback するか判断する

確認コマンド:

```powershell
$base = 'https://report-generator-635067190197.asia-northeast1.run.app'
Invoke-WebRequest -Uri "$base/api-health" -UseBasicParsing |
  Select-Object StatusCode,Content

gcloud.cmd run services describe report-generator `
  --project=ice-sh `
  --region=asia-northeast1 `
  --format='json(status.latestReadyRevisionName,status.traffic,status.url)'
```

Cloud Logging 確認:

```powershell
$revision = '<latest-ready-revision>'
$filter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND resource.labels.revision_name="' + $revision + '" AND severity>=ERROR'
gcloud.cmd logging read $filter `
  --project=ice-sh `
  --freshness=30m `
  --limit=50 `
  --format='value(timestamp,severity,textPayload)'
```

rollback が必要な場合は `docs/deploy.md` の rollback 手順に従います。

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

## Slack Webhook Rotation

`webhook.txt`、画面共有、ログ、PR差分などにSlack webhook URLが含まれていた可能性がある場合の手順です。URL値はこの手順中でも記録しません。

### 1. 事前確認

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-secret-exposure-metadata.ps1 -AsJson
```

確認観点:

- `SLACK_WEBHOOK_SECRET_NAME` はSecret名だけで、URL実値ではない
- `slack-download-webhook-url` が対象projectのSecret Managerに存在するか
- `webhook.txt` はgit管理対象から外れているか

### 2. Slack側の旧webhook無効化

Slack app / Incoming Webhooks の管理画面で、露出疑いのある旧webhookを revoke または delete します。

記録する内容:

- 実施日時
- 実施者
- 対象channel名
- 旧webhookの状態: revoked / deleted / already inactive

記録しない内容:

- webhook URL実値
- token断片
- secret payload

### 3. 新webhookが必要な場合

Secret Manager に新しい値を登録します。値は標準入力または一時ファイルから渡し、コマンド履歴に残しません。

```powershell
# URL実値は貼り付け後に表示・保存しない
$secure = Read-Host 'New Slack webhook URL' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  $plain | gcloud.cmd secrets versions add slack-download-webhook-url `
    --project=ice-sh `
    --data-file=-
} finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
  $plain = $null
}
```

Cloud Run では `SLACK_WEBHOOK_SECRET_NAME=slack-download-webhook-url` のようにSecret名だけを保持します。URL実値をenv literalにしません。

### 4. 確認

- Secret Manager のversion状態を確認する
- 必要に応じてCloud Runをredeployし、latest secretを読み込ませる
- delivery作成やversion追加など、最小の安全な操作で通知経路を確認する
- 確認後、NotionへURL実値なしで結果を記録する

## セキュリティ注意

- Access Key、Secret、Admin Key、PIN、token、生メールアドレスをログやチケットに残さない
- Cloud Logging の調査では `token_hash`、`email_hash`、`recipient_hash` を使う
- 画面共有や IDE 文脈に credential 断片が出た場合は、安全側でローテーション対象にする
- OTP / PIN の検証条件は暫定復旧でも緩めない
- 作業用 AWS credential は必要時だけ発行し、確認後に無効化する

## Admin dedicated service IAP auth addendum

Admin専用serviceで人間向け管理操作をIAPログインに寄せる場合は、次のenvをAdmin専用serviceだけに設定します。

- `ADMIN_IAP_AUTH_ENABLED=1`
- `ADMIN_IAP_ALLOWED_EMAILS=sinohara@impress.co.jp`
- `ADMIN_IAP_SERVICE_NAME=report-generator-admin`

この経路ではIAPが付与する `X-Goog-Authenticated-User-Email` を許可メールと照合します。public service `report-generator` にはこれらのenvを設定しません。

監査ログではIAP経由の操作を `actor_type=iap_user` とし、メールアドレスそのものではなく `iap_email_hash` のみを保存します。許可外IAPユーザーは `actor_type=iap_user_denied` として扱います。

本番運用前の最小確認:

1. `report-generator-admin` にIAPでログインできる
2. `sinohara@impress.co.jp` で `GET /deliveries?limit=1` が `200`
3. 未ログインまたは未許可ユーザーはAdmin UI/APIに到達できない
4. public serviceの `/api-health`、`/d/*`、OTP flowに影響がない
5. `X-Admin-Key` 経路はscript/APIまたはbreak-glass用として必要範囲で継続する

### Production operation decision

2026-06-19 時点の判断:

- 人間向け Admin UI は `report-generator-admin` + Cloud Run direct IAP を主経路にする
- 初期許可userは `sinohara@impress.co.jp` のみとし、Google Group 管理は現時点では採用しない
- public service `report-generator` はdownload、OTP、health、既存互換用Admin経路を維持し、service全体へIAPを適用しない
- `X-Admin-Key` はmachine/scriptとbreak-glass用に継続する
- public serviceの `/admin` を閉じる、またはAdmin UIをAdmin専用serviceだけに限定する変更は、別タスクとしてruntime影響とrollback条件を確認してから実施する
- break-glassで `X-Admin-Key` を使った場合は、操作後にAdmin audit log、Admin auth failure、runtime ERRORを確認し、原則としてAdmin keyをrotateする

2026-06-26 Phase 7 final decision:

- `scripts\check-admin-iap-readonly.ps1 -AsJson` は PASS
- current good revision: `report-generator-admin-00002-59c`
- initial allowed user: `sinohara@impress.co.jp`
- IAP accessor policy と `ADMIN_IAP_ALLOWED_EMAILS` は明示user 1件で一致
- Cloud Run invoker は IAP service agent のみに維持
- public service `/api-health` と `/admin` は `200` を維持
- Admin no-auth `/admin` は IAP 生成の Google login redirect
- Admin専用service latest revision の runtime ERROR はなし
- Phase 7 時点では、人間向け Admin UI の主経路を
  `report-generator-admin` + Cloud Run direct IAP と判断する
- `X-Admin-Key` は machine/script と break-glass 用に継続し、通常の人間操作では
  IAP経路を優先する
- public service の `/admin` 廃止またはAdmin専用service限定は、別Phaseで
  runtime影響、既存script/API利用、rollback条件を確認してから判断する

2026-06-29 Phase 8 public `/admin` reassessment:

- `scripts\check-admin-iap-readonly.ps1 -AsJson` は PASS
- current good revision: `report-generator-admin-00002-59c`
- public service `/api-health` は `200`
- public service `/admin` は `200`
- Admin専用serviceの未ログイン `/admin` は IAP 生成の Google login redirect (`302`)
- Phase 8 時点では、public service `/admin` は現状維持とする
- public service全体へIAPは適用しない
- public service `/admin` の閉鎖またはAdmin専用service限定は、read-only check、
  deploy smoke、既存script/API、download、OTPへの影響を分離して確認してから再判断する

再評価条件:

- 人間向け Admin UI 操作が `report-generator-admin` + Cloud Run direct IAP で継続運用できている
- machine/scriptのAdmin API経路がpublic service `/admin` UIに依存していないことを確認済み
- `scripts\check-admin-iap-readonly.ps1` とdeploy smokeの期待値を変更できる状態になっている
- rollback対象revision、rollback後確認、break-glass条件が明示されている

### Multiple admin users

複数管理者が必要になった場合も、現時点では Google Group 管理へ切り替えず、明示 user allowlist で追加します。

追加時の変更点:

1. `report-generator-admin` の IAP policy に対象userへ `roles/iap.httpsResourceAccessor` を付与する
2. `report-generator-admin` の `ADMIN_IAP_ALLOWED_EMAILS` に同じメールアドレスをカンマ区切りで追加する
3. public service `report-generator` には `ADMIN_IAP_AUTH_ENABLED` を設定しない
4. Cloud Run `roles/run.invoker` はIAP service agentのみに維持し、user/groupへ直接付与しない
5. 退任・異動時は IAP policy と `ADMIN_IAP_ALLOWED_EMAILS` の両方から対象userを削除する

確認例:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-iap-readonly.ps1 `
  -ExpectedIapUsers sinohara@impress.co.jp,admin2@example.com
```

実運用手順:

1. 追加対象user、理由、期限、作業者、rollback対象revisionをタスクに記録する
2. `report-generator-admin` の current good revision と IAP policy を取得する
3. 対象userへ `roles/iap.httpsResourceAccessor` を追加する
4. `ADMIN_IAP_ALLOWED_EMAILS` を対象user込みの完全なカンマ区切り一覧で更新する
5. `scripts\check-admin-iap-readonly.ps1 -ExpectedIapUsers <EXPECTED_USERS>` を実行する
6. 追加userで Admin UI 表示と必要最小限の人間操作smokeを実施する
7. public serviceの `/api-health`、`/admin`、download、OTPに影響がないことを確認する
8. IAP policy、Cloud Run IAM policy、allowlist、smoke結果をタスクに記録する

IAP policy 追加例:

```powershell
gcloud.cmd iap web add-iam-policy-binding `
  --project=ice-sh `
  --region=asia-northeast1 `
  --resource-type=cloud-run `
  --service=report-generator-admin `
  --member=user:<ADMIN_EMAIL> `
  --role=roles/iap.httpsResourceAccessor
```

Allowlist更新例:

```powershell
gcloud.cmd run services update report-generator-admin `
  --project=ice-sh `
  --region=asia-northeast1 `
  --update-env-vars=ADMIN_IAP_ALLOWED_EMAILS=<COMMA_SEPARATED_ALLOWED_USERS>
```

削除時は、IAP policy と `ADMIN_IAP_ALLOWED_EMAILS` の両方から対象userを削除します。
片方だけを変更した状態はdriftとして扱い、次の確認で失敗させます。

IAP policy 削除例:

```powershell
gcloud.cmd iap web remove-iam-policy-binding `
  --project=ice-sh `
  --region=asia-northeast1 `
  --resource-type=cloud-run `
  --service=report-generator-admin `
  --member=user:<ADMIN_EMAIL> `
  --role=roles/iap.httpsResourceAccessor
```

drift確認:

- IAP `roles/iap.httpsResourceAccessor` のuser一覧と `ADMIN_IAP_ALLOWED_EMAILS` が一致している
- Cloud Run `roles/run.invoker` はIAP service agentのみに維持されている
- public service `report-generator` に `ADMIN_IAP_AUTH_ENABLED` が設定されていない
- 期待外user、group、domain単位の付与がない

記録テンプレート:

```text
対象user:
追加/削除理由:
作業者:
期限:
rollback対象revision:
IAP policy変更:
ADMIN_IAP_ALLOWED_EMAILS変更:
read-only check結果:
人間操作smoke結果:
public service影響確認:
drift確認:
rollback要否:
```

Google Group 管理は、管理者数が増えてライフサイクル管理を個別userで維持できなくなった時点で別タスクとして再検討します。その場合も、group採用前に rollback 手順、break-glass user、IAP policy差分、退任時の責任分界を明記します。

本番運用へ寄せる条件:

1. `scripts\check-admin-iap-readonly.ps1` がpassする
2. 許可userで Admin UI、delivery create、version add、disable/enable、download log views のbrowser smokeが完了している
3. public serviceの `/api-health`、`/d/*`、OTP flowに影響がない
4. 監査ログでIAP経由操作が `actor_type=iap_user` として記録され、生メールアドレスが残らない
5. `report-generator-admin` のcurrent good revision、previous ready revision、IAP policy、Cloud Run IAM policyがNotion taskに記録されている

## Admin dedicated service cutover / rollback

This procedure is for `report-generator-admin` only. The public `report-generator`
service remains the user-facing service for download, OTP, and public health
checks.

### Cutover baseline

Before changing traffic, IAP policy, or Admin allowlist settings, record these
values in the task log:

- source PR, commit SHA, image tag, and Cloud Run revision
- latest ready revision and previous ready revision for `report-generator-admin`
- IAP IAM policy for `report-generator-admin`
- Cloud Run IAM policy for `report-generator-admin`
- output from `scripts\check-admin-iap-readonly.ps1`
- allowed-user browser smoke result for Admin UI, delivery create, version add,
  disable/enable, and download log views
- public service `/api-health` and `/admin` status

Baseline commands:

```powershell
gcloud.cmd run revisions list `
  --service report-generator-admin `
  --project ice-sh `
  --region asia-northeast1 `
  --format='table(metadata.name,status.conditions[0].status,status.conditions[0].type,metadata.creationTimestamp)'

gcloud.cmd run services describe report-generator-admin `
  --project ice-sh `
  --region asia-northeast1 `
  --format='value(status.latestReadyRevisionName)'

gcloud.cmd iap web get-iam-policy `
  --project=ice-sh `
  --region=asia-northeast1 `
  --resource-type=cloud-run `
  --service=report-generator-admin

gcloud.cmd run services get-iam-policy report-generator-admin `
  --project ice-sh `
  --region asia-northeast1
```

Current known baseline:

- current good revision: `report-generator-admin-00002-59c`
- previous ready revision: `report-generator-admin-00001-jvk`

`report-generator-admin-00001-jvk` was created during early IAP validation. It
may not contain all production runtime env and secret settings. If traffic is
rolled back to this revision and Admin API operations fail closed, use the public
service `X-Admin-Key` break-glass path for urgent operations.

### Traffic rollback

Move all Admin service traffic to the last known good revision:

```powershell
gcloud.cmd run services update-traffic report-generator-admin `
  --project ice-sh `
  --region asia-northeast1 `
  --to-revisions=<PREVIOUS_GOOD_REVISION>=100
```

Restore traffic to the latest ready revision after verification:

```powershell
gcloud.cmd run services update-traffic report-generator-admin `
  --project ice-sh `
  --region asia-northeast1 `
  --to-latest
```

Do not update traffic for the public `report-generator` service as part of an
Admin-only rollback unless a separate public-service incident requires it.

### IAP policy rollback

If an IAP accessor policy change causes an outage, restore the personal
break-glass accessor first:

```powershell
gcloud.cmd iap web add-iam-policy-binding `
  --project=ice-sh `
  --region=asia-northeast1 `
  --resource-type=cloud-run `
  --service=report-generator-admin `
  --member=user:sinohara@impress.co.jp `
  --role=roles/iap.httpsResourceAccessor
```

After the personal accessor is confirmed, remove the faulty user or group
binding if needed:

```powershell
gcloud.cmd iap web remove-iam-policy-binding `
  --project=ice-sh `
  --region=asia-northeast1 `
  --resource-type=cloud-run `
  --service=report-generator-admin `
  --member=group:<GROUP_EMAIL_ADDRESS> `
  --role=roles/iap.httpsResourceAccessor
```

Do not grant `roles/run.invoker` directly to users or groups. The Admin service
Cloud Run invoker binding remains limited to the IAP service agent:
`service-635067190197@gcp-sa-iap.iam.gserviceaccount.com`.

### Admin allowlist rollback

If a valid IAP login reaches the service but receives Admin `401`, check
`ADMIN_IAP_ALLOWED_EMAILS` on `report-generator-admin`. Roll back by either
redeploying the Admin service with the correct actual user emails or moving
traffic back to the previous known good Admin revision.

Do not set `ADMIN_IAP_AUTH_ENABLED` on the public `report-generator` service. If
that setting is accidentally deployed to public service, roll back public service
traffic immediately and verify `/api-health`, `/admin`, and the OTP/download
flow.

### Break-glass operation

Use the public service `X-Admin-Key` path only when IAP, Google login, or the
Admin service is unavailable and urgent Admin operations cannot wait for normal
rollback.

Before use, record the target, reason, operator, and expected time window in the
Notion task. After use, check Admin audit logs, Admin auth failure logs, runtime
errors, and rotate the Admin key by default.

### Rollback verification

After traffic or policy rollback, run:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-iap-readonly.ps1
```

The expected result is:

- Admin no-auth `/admin` returns an IAP-generated `302` to Google login
- public `/api-health` returns `200`
- public `/admin` returns `200`
- Admin service latest revision has no new runtime `ERROR` logs
- IAP accessor policy contains the expected explicit user
- Cloud Run invoker policy remains limited to the IAP service agent

Record the rollback reason, target revision, policy changes, command result, and
human smoke result in the Notion task.

## Admin IAP Read-Only Smoke

Admin専用service + IAP の設定確認には、secret値を読まない read-only script を使います。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-iap-readonly.ps1
```

複数管理者を期待値として確認する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-iap-readonly.ps1 `
  -ExpectedIapUsers sinohara@impress.co.jp,admin2@example.com
```

JSONで保存する場合:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-iap-readonly.ps1 -AsJson |
  Set-Content -Encoding UTF8 artifacts\admin-iap-readonly-check.json
```

確認対象:

- `report-generator-admin` の `run.googleapis.com/iap-enabled`
- `ADMIN_IAP_AUTH_ENABLED`、`ADMIN_IAP_ALLOWED_EMAILS`、`ADMIN_IAP_SERVICE_NAME` の存在
- Cloud Run `roles/run.invoker` がIAP service agentのみに付与されていること
- IAP `roles/iap.httpsResourceAccessor` に期待userが付与されていること
- `ADMIN_IAP_ALLOWED_EMAILS` に期待userが含まれ、想定外userが含まれていないこと
- 未ログイン `/admin` がIAP生成レスポンスでGoogle loginへ `302` されること
- public serviceの `/api-health` と `/admin` が `200` のままであること
- Admin専用service latest revisionのERRORログがないこと

scriptでは許可ユーザーのブラウザログイン完了までは確認しません。許可ユーザーでのAdmin UI表示と、delivery作成、version追加、disable/enable、DLログ表示などの人間操作smokeは別途実施してNotionへ結果を記録します。
