# ICE Report Generator

Cloud Run 上で動作する、ICE向けレポート生成・配布管理システムです。

## 概要

BigQuery の集計結果を Excel レポートとして生成し、GCS に保存したうえで、外部向けの配布URLを発行します。

## 主な機能

- BigQuery からのデータ取得
- Excel レポート生成
- GCS へのアップロード
- 配布URL作成
- メールアドレス / ドメインによるダウンロード制御
- Signed URL による短時間ダウンロード
- ダウンロードログ記録
- Slack通知
- 管理画面
- cleanup scheduler 対応

## 技術構成

- Python
- Flask
- Docker
- Cloud Run
- BigQuery
- Cloud Storage
- Firestore
- Secret Manager
- Cloud Scheduler
- Artifact Registry

## 運用ブランチ

- `main`: 本番安定版
- `develop`: 通常作業用

## デプロイ方針

Docker build は必ず `--no-cache` を使用します。

```bash
docker build --no-cache -t asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:${GITHUB_SHA} .
```

## メール送信 provider 方針

OTP / PIN 送信は provider interface 経由で切り替える前提です。

- 共通 interface: `mail_provider.py`
- OTP 用テンプレート: `otp_mail.py`
- runtime resolver: `mail_runtime.py`

想定 provider は次の 3 種です。

- `logging`: Phase1 の暫定送信経路。実送信せず Cloud Logging で確認
- `noop`: 開発用の無送信モード
- `ses`: Amazon SES 本実装

### SES 本番設定

`MAIL_PROVIDER=ses` の場合、起動時に必須設定を検証し、不足があれば fail closed で起動失敗します。

認証経路は次です。

1. Cloud Run が metadata server から Google-signed ID token を取得
2. AWS STS `AssumeRoleWithWebIdentity` で一時認証情報へ交換
3. 取得した一時認証情報で SES `SendEmail` を実行

推奨環境変数:

- `MAIL_PROVIDER=ses`
- `AWS_SES_ROLE_ARN`
- `AWS_SES_WEB_IDENTITY_AUDIENCE`
- `AWS_SES_REGION`
- `AWS_SES_FROM_ADDRESS`
- `AWS_SES_FROM_NAME`
- `AWS_SES_CONFIGURATION_SET` (optional)
- `AWS_SES_ROLE_SESSION_NAME` (optional)
- `AWS_SES_ROLE_SESSION_DURATION_SECONDS` (optional)
- `AWS_SES_TIMEOUT_SECONDS` (optional)
- `MAIL_REPLY_TO_EMAILS` (optional)
- `MAIL_SERVICE_NAME` (optional)

移行互換として次の既存名も当面は fallback で読み取ります。

- `MAIL_FROM_EMAIL`
- `MAIL_FROM_NAME`
- `MAIL_PROVIDER_SES_REGION`
- `MAIL_PROVIDER_SES_CONFIGURATION_SET`
- `MAIL_PROVIDER_TIMEOUT_SECONDS`

長期AWS access key (`AWS_SES_ACCESS_KEY_ID` / `AWS_SES_SECRET_ACCESS_KEY`) は本番経路の前提にしません。残っている場合は削除候補として扱い、切替確認後に整理してください。

Cloud Run では認証情報をイメージへ埋め込まず、Secret Manager から設定注入する前提です。具体的な切替手順は `docs/ses-cutover-checklist.md` を参照してください。

### 削除候補の扱い

不要になった設定名や運用手順は即削除せず、確認付きのやることとして管理します。候補は `docs/ses-cutover-checklist.md` に残し、合意後に削除します。
