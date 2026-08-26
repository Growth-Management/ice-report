# CLAUDE.md

このファイルは Claude Code がこのリポジトリで作業する際に必ず読み込む設定です。
詳細手順は各 `docs/*.md` を正とし、ここには要点とルールだけを書きます。

## プロジェクト概要

**ICE Report Generator** — BigQuery の集計結果を Excel レポートとして生成し、GCS に保存、
OTP/PIN認証付きの配布URLで社外に届けるCloud Runアプリ。

- BigQueryからのデータ取得 → Excel生成 → GCSアップロード → 配布URL発行
- メールアドレス/ドメインによるダウンロード制御、Signed URLでの短時間ダウンロード
- ダウンロードログ記録、Slack通知、管理画面、cleanupスケジューラ

## 環境・インフラ

| 項目 | 値 |
|---|---|
| GCPプロジェクト | `ice-sh` |
| BigQueryプロジェクト | `jumpplus-4a5f4` |
| Cloud Runサービス | `report-generator` |
| リージョン | `asia-northeast1` |
| Runtime SA | `ice-report-runner@ice-sh.iam.gserviceaccount.com` |
| Deploy impersonation SA | `ice-deployer@ice-sh.iam.gserviceaccount.com` |
| Artifact Registry | `asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator` |
| メール送信元 | `report-noreply@ice-sv.jp`(SES, リージョン `ap-northeast-1`) |

技術構成: Python 3.12 / Flask / Docker / Cloud Run / BigQuery / GCS / Firestore /
Secret Manager / Cloud Scheduler / Artifact Registry。操作環境はWindows PowerShellが主。

## リポジトリ構成(主要ファイル)

- `app.py` — Flaskエントリポイント。管理API、配布/OTP認証フロー、ダウンロードルーティング
- `create_report.py` — BigQuery→Excel生成のコアロジック。デフォルトテンプレート/クエリマッピング
- `distribution.py` — Firestore上の配布レコード・レポート定義・スケジュール実行・tokenを管理
- `drive_io.py` — Google Drive連携(アップロード/ダウンロード)
- `mail_provider.py` — メール送信の抽象interface(`MailDeliveryRequest`/`Result`)
- `mail_runtime.py` — providerのruntime resolver。SES送信はAWS STS `AssumeRoleWithWebIdentity`経由
- `otp_mail.py` — OTP PINメール本文のテンプレート生成
- `thermae_romae_report.py` — 個別帳票「テルマエ・ロマエ月次販売報告書」専用ロジック
- `sql/free.sql` / `sql/paid.sql` — 無料/有料セグメント向けBigQueryクエリ
- `templates/template.xlsx` — Excel出力テンプレート
- `tools.yaml`, `env.yaml`, `webhook.txt`, `.env*` — ローカル専用設定。**gitに追加しない・実値を転記しない**

## 開発コマンド

```powershell
# セットアップ
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 構文チェック
python -m py_compile app.py create_report.py distribution.py mail_runtime.py

# ローカル起動(実送信を避けるため MAIL_PROVIDER=logging)
$env:FLASK_APP = 'app'
$env:MAIL_PROVIDER = 'logging'
$env:ADMIN_API_KEY = 'local-admin-key'
$env:ADMIN_AUTH_FAIL_CLOSED = '1'
$env:OTP_HASH_SECRET = 'local-otp-secret'
python -m flask run --host 127.0.0.1 --port 8080
```

BigQuery/GCS/Firestore/Secret Managerをローカルから使う場合は
`gcloud auth application-default login` が必要。

## デプロイ

Docker buildは必ず `--no-cache`。詳細手順は `docs/deploy.md`。

```powershell
$sha = (git rev-parse HEAD).Trim()
$image = "asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:$sha"
docker build --no-cache -t $image .
docker push $image

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

docsのみの変更ならCloud Run再deployは不要。`app.py`/runtime設定/Dockerfile/requirements/
テンプレート/SQLを変更した場合はdeploy対象。

## ブランチ運用

- `main`: 本番安定版
- `develop`: 通常作業用

## 絶対に守るルール(セキュリティ)

- **credential・access key・admin key・OTP secretの値をリポジトリ、ログ、チャット、Notionに残さない**
- 本番メール送信はAWS長期access keyを使わない(Cloud Run SA → AWS STS `AssumeRoleWithWebIdentity`が前提)。
  `AWS_SES_ACCESS_KEY_ID`/`AWS_SES_SECRET_ACCESS_KEY` は本番経路で使わない
- Cloud Loggingに生PIN・生token・生メールアドレス・secret・Authorization headerを出さない。
  照合には `token_hash`/`email_hash`/`recipient_hash` を使う
- OTP/PINのTTL・試行回数上限・download sessionのone-time条件は、障害対応中でも緩めない
- SES障害時に未検証のproviderへ無断で切り替えない
- 配布URLやSigned URLを公開チャンネルに貼らない
- `env.yaml`/`webhook.txt`/`.env*`/`*_accessKeys.csv` に実値が入っていた疑いがある場合は、
  中身を読んで共有せず `docs/security.md` の Secret Exposure Response 手順に従う
- 本番Cloud Runで未設定を許容しない環境変数: `ADMIN_API_KEY`, `OTP_HASH_SECRET`,
  `MAIL_PROVIDER=ses`, `AWS_SES_ROLE_ARN`, `AWS_SES_WEB_IDENTITY_AUDIENCE`,
  `AWS_SES_REGION`, `AWS_SES_FROM_ADDRESS`, `PROJECT_ID=ice-sh`,
  `BIGQUERY_PROJECT_ID=jumpplus-4a5f4`, `PUBLIC_BASE_URL`

## 詳細ドキュメントへのポインタ

| ファイル | 内容 |
|---|---|
| `docs/setup.md` | 開発・運用準備の入口 |
| `docs/deploy.md` | Cloud Run deploy手順(build→push→deploy→smoke→rollback) |
| `docs/operations.md` | 日常/月次運用、smoke test、障害時初動、rollback(大容量、目次から検索推奨) |
| `docs/monitoring.md` | Cloud Monitoring / alerting設定 |
| `docs/security.md` | 保護対象・本番制御・権限方針・Secret Exposure Response |
| `docs/env-compatibility.md` | 旧env名・legacy access keyの棚卸し記録 |
| `docs/ses-cutover-checklist.md` | SES切替・確認手順 |
| `docs/roadmap.md` | 完了済み整備事項と今後の優先課題 |
| `docs/drive-domain-wide-delegation.md` | Google Drive連携の権限設定 |
| `docs/thermae-romae-report.md` | テルマエ・ロマエ帳票の個別仕様 |
| `docs/report-definitions-guide.md` | Admin UIでのレポート定義の追加・使い方ガイド |
| `docs/new-bespoke-report-playbook.md` | 別SQL・別テンプレートが必要な専用レポートの追加手順 |

作業内容が上記docsのどれかに関わる場合は、着手前にそのdocsを読んでから進めてください。

## トークン節約ルール

- 大きいファイルは全文読まず、view_range/grepで必要箇所のみ読むこと
- 別タスクに切り替える際は /clear を促す、または要点のみ引き継ぐこと
- ログ・SQL結果などはファイルに書き出し、パス参照で報告する。本文に全文貼らない
- 編集は可能な限り複数変更をまとめて1回のツール呼び出しで行う
- 必要なMCPツールのみ使用し、無関係なツール呼び出しはしない
- Notionにアクセスする際は、まず docs/notion-map.md を確認し、記載のIDがあれば notion-search を使わず直接 fetch すること
