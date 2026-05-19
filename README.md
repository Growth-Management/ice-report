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
docker build --no-cache -t asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:latest .
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

推奨環境変数:

- `MAIL_PROVIDER`
- `MAIL_FROM_EMAIL`
- `MAIL_REPLY_TO_EMAILS`
- `MAIL_PROVIDER_SES_CONFIGURATION_SET`
- `MAIL_PROVIDER_SES_REGION`
- `MAIL_PROVIDER_TIMEOUT_SECONDS`
