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