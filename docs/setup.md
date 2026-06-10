# Setup

ICE Report Generator の開発・運用準備メモです。詳細な本番deploy手順は `docs/deploy.md`、日常運用は `docs/operations.md`、セキュリティ方針は `docs/security.md` を正とします。

## 前提

- OS: Windows PowerShell を主な操作環境とする
- Python: `3.12` 系を推奨。Docker image も `python:3.12-slim` を使う
- GCP project: `ice-sh`
- Cloud Run service: `report-generator`
- Region: `asia-northeast1`
- Runtime service account: `ice-report-runner@ice-sh.iam.gserviceaccount.com`
- Deploy impersonation service account: `ice-deployer@ice-sh.iam.gserviceaccount.com`
- Artifact Registry: `asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator`
- BigQuery project: `jumpplus-4a5f4`

## 必要ツール

- Git
- Python 3.12
- Google Cloud CLI
- Docker Desktop または Cloud Build を実行できるGCP権限
- GitHub CLI `gh`
- PowerShell 5 以降
- headless Chrome または Playwright。管理画面スクリーンショット確認を行う場合のみ

## ローカル準備

```powershell
git clone https://github.com/Growth-Management/ice-report.git
cd ice-report

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

ローカル設定ファイル:

- `env.example.yaml` は安全なサンプルです。必要に応じて `env.yaml` へコピーして、ローカル専用の値を入れます。
- `env.yaml`、`webhook.txt`、`.env*`、`tools.yaml`、`*_accessKeys.csv` はローカル/private設定として扱い、gitに追加しません。
- 既存の `env.yaml` / `webhook.txt` がローカルに残っていても、そのまま作業者専用ファイルとして扱います。
- 実値入りの `ADMIN_API_KEY`、`OTP_HASH_SECRET`、Slack webhook URL、AWS credential は、サンプルやdocsへ転記しません。

基本的な構文確認:

```powershell
python -m py_compile app.py create_report.py distribution.py mail_runtime.py
```

Flaskをローカルで起動する場合:

```powershell
$env:FLASK_APP = 'app'
$env:MAIL_PROVIDER = 'logging'
$env:ADMIN_API_KEY = 'local-admin-key'
$env:ADMIN_AUTH_FAIL_CLOSED = '1'
$env:OTP_HASH_SECRET = 'local-otp-secret'
python -m flask run --host 127.0.0.1 --port 8080
```

`ADMIN_AUTH_FAIL_CLOSED=1` を設定すると、local/dev でも `ADMIN_API_KEY` 未設定時に管理 API が `401` で閉じます。本番に近い確認では必ず `ADMIN_API_KEY` を明示してください。UI表示だけを確認する一時的なlocal作業では `ADMIN_AUTH_FAIL_CLOSED` を外せますが、その状態を本番手順や運用確認の前提にしないでください。

ローカル実行で BigQuery、GCS、Firestore、Secret Manager を使う場合は、`gcloud auth application-default login` と対象GCP権限が必要です。開発中の疎通だけなら、実送信を避けるため `MAIL_PROVIDER=logging` を使います。

## 機密情報の扱い

- credential、access key、admin key、OTP secret の値をリポジトリ、ログ、チャット、Notion に残さない
- 実値入りのローカル設定は `env.yaml` などのignore対象ファイルに置き、共有用には `env.example.yaml` のplaceholderだけを使う
- 本番 secret は Secret Manager からCloud Runへ注入する
- `AWS_SES_ACCESS_KEY_ID` / `AWS_SES_SECRET_ACCESS_KEY` は本番経路で使わない
- SES本番送信は Cloud Run service account から AWS STS `AssumeRoleWithWebIdentity` を使う
- 作業用AWS credentialが必要な場合は、必要時だけ発行し、確認後に無効化する

## 本番設定の入口

本番必須設定は `docs/security.md` を参照します。主要な設定は次です。

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
- `BUCKET_NAME`

任意設定または既定値のある主要項目:

- `ADMIN_AUDIT_LOGS_COLLECTION`: default `admin_audit_logs`
- `SECURITY_EVENTS_COLLECTION`: default `security_events`
- `DELIVERIES_COLLECTION`: default `deliveries`
- `DOWNLOAD_LOGS_COLLECTION`: default `download_logs`

旧env名とlegacy access key secretの扱いは `docs/env-compatibility.md` を正とします。

## Build / Deploy

本番deployは `docs/deploy.md` の手順に従います。要点は次です。

```powershell
$sha = (git rev-parse HEAD).Trim()
$image = "asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:$sha"

docker build --no-cache -t $image .
docker push $image
```

Cloud Run deploy:

```powershell
gcloud.cmd run deploy report-generator `
  --image $image `
  --region asia-northeast1 `
  --project ice-sh `
  --memory 2Gi `
  --service-account ice-report-runner@ice-sh.iam.gserviceaccount.com `
  --allow-unauthenticated `
  --impersonate-service-account=ice-deployer@ice-sh.iam.gserviceaccount.com `
  --quiet
```

変更がdocsだけの場合はCloud Run deploy不要です。`app.py`、runtime設定、Dockerfile、requirements、テンプレート、SQLを変更した場合はdeploy対象として扱います。

## Smoke Test

deploy後の最小確認:

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

実メールを伴うOTP smokeは、対象deliveryと宛先を事前に確認したうえで `docs/operations.md` に従って最小回数で実行します。

## 関連ドキュメント

- `README.md`: repo全体の概要
- `docs/deploy.md`: Cloud Run deploy手順
- `docs/operations.md`: 日常運用、月次運用、smoke、障害時初動、rollback
- `docs/monitoring.md`: Cloud Monitoring / alerting
- `docs/security.md`: 保護制御と残論点
- `docs/env-compatibility.md`: 旧env名とlegacy access keyの棚卸し
- `docs/ses-cutover-checklist.md`: SES切替・確認手順
- `docs/roadmap.md`: 完了済み整備と今後の優先課題
