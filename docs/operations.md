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

初期運用では、定期実行は週1回とし、deploy後は都度実行します。自動化
基盤を追加するまでは、システム管理室の運用端末または明示的に権限を
付与した実行環境から実行します。

標準頻度:

- 定期: 毎週月曜 10:00 JST 目安
- deploy後: Cloud Run traffic切替後に1回
- incident後: 復旧確認後に1回

定期実行向け wrapper:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\run-operations-readonly-scheduled.ps1
```

この wrapper は次を `artifacts/operations-readonly/` へ保存します。
`artifacts/` は git 管理しません。

- `operations-readonly-check-<timestamp>.json`
- `operations-readonly-check-<timestamp>-summary.txt`

Notion へ転記する場合は summary text のみを使います。JSON artifact はローカル
確認用とし、API応答本文、Admin key、PIN、生メールアドレス、token を
チケットやチャットへ貼り付けません。

失敗時対応:

1. wrapper の `passed=false` または exit code 1 を確認する
2. `failedChecks` と summary text の `Failed checks` を確認する
3. `/api-health`、Cloud Run latest ready revision、runtime ERROR log を優先確認する
4. Admin認証失敗が増えているだけの場合は、誤key check由来かを確認する
5. 利用者影響がある場合は本 playbook の一次対応または rollback 手順へ進む
6. Notion には summary、failedChecks、確認者、確認日時、一次対応結果を記録する

定期実行の前提:

- `gcloud` が対象project `ice-sh` を読める
- `report-generator-admin-api-key` の Secret Manager read 権限がある
- Cloud Run / Cloud Logging / Cloud Monitoring の read 権限がある
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
単位で確認します。専用 backup フォルダが未確定、または対象ファイルのURLを
確認できない場合は GCS object を削除しません。

確認する項目:

- backup URL が Google Drive 上の対象 Excel file を指している
- backup file name に顧客名、対象月、delivery_id、version、元GCS URIを追跡
  できる情報がある
- backup file の閲覧権限がシステム管理室の運用担当者で確認できる
- Drive backup の保持目安が対象月から7年であることを削除承認記録に残す
- folder URL、owner、運用担当者が分かる場合は削除承認記録へ転記する
- folder URL が未確定の場合は、個別backup URLを必須項目として扱う

Drive検索で専用 backup フォルダを断定できない場合:

1. GCS削除は実施しない
2. Notion削除承認に「Drive backup folder未確定」と記録する
3. 個別 backup URL、file name、閲覧確認者、確認日時を記録する
4. 専用フォルダを新設または既存フォルダを採用する場合は、別タスクで
   folder URL、owner、共有範囲、保持方針を確定する

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

```text
ICE Report Generator 削除承認

対象種別:
対象ID / GCS URI / collection:
対象月:
顧客名:
現在の active/current 参照確認:
保持期限を満たしている根拠:
Drive backup URL:
Drive backup folder URL / owner:
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

If group-based IAP access causes an outage, restore the personal break-glass
accessor first:

```powershell
gcloud.cmd iap web add-iam-policy-binding `
  --project=ice-sh `
  --region=asia-northeast1 `
  --resource-type=cloud-run `
  --service=report-generator-admin `
  --member=user:sinohara@impress.co.jp `
  --role=roles/iap.httpsResourceAccessor
```

After the personal accessor is confirmed, remove the faulty group binding if
needed:

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
- IAP accessor policy contains the intended user or group
- Cloud Run invoker policy remains limited to the IAP service agent

Record the rollback reason, target revision, policy changes, command result, and
human smoke result in the Notion task.

## Admin IAP Read-Only Smoke

Admin専用service + IAP の設定確認には、secret値を読まない read-only script を使います。

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\check-admin-iap-readonly.ps1
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
- IAP `roles/iap.httpsResourceAccessor` の付与対象が存在すること
- 未ログイン `/admin` がIAP生成レスポンスでGoogle loginへ `302` されること
- public serviceの `/api-health` と `/admin` が `200` のままであること
- Admin専用service latest revisionのERRORログがないこと

scriptでは許可ユーザーのブラウザログイン完了までは確認しません。許可ユーザーでのAdmin UI表示と、delivery作成、version追加、disable/enable、DLログ表示などの人間操作smokeは別途実施してNotionへ結果を記録します。
