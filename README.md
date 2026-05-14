# report_excel_cloudrun

Cloud Runでジャンプ＋月次Excelを生成し、非公開GCSへアップロードする版です。

## ローカル実行

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

gcloud auth application-default login
python create_report.py --project jumpplus-4a5f4 --bucket <bucket-name>
```

## Cloud Run実行

### 環境変数

- `PROJECT_ID`: BigQueryジョブを実行するGCPプロジェクトID
- `BUCKET_NAME`: 生成Excel保存先のGCS bucket名
- `OBJECT_PREFIX`: GCS object prefix。未指定時は `reports/plus`

### エンドポイント

```http
POST /generate
Content-Type: application/json

{}
```

任意で以下を指定できます。

```json
{
  "project_id": "jumpplus-4a5f4",
  "bucket_name": "your-private-bucket",
  "object_prefix": "reports/plus",
  "today": "2026-05-01"
}
```

### 出力先

```text
GCS: gs://<bucket>/<object_prefix>/<yymm>/ダウンロード数入力シート_<yymmdd>_ICE入力済み_plus.xlsx
```

## Excel仕様

- 有料シート: 10列テーブル、集計行あり
- 無料シート: 6列テーブル、集計行なし
- 有料集計行はExcelのSUBTOTAL再計算に依存せず、PythonでE:J列の合計値を書き込みます
- Cloud RunではExcel本体を使わないため、xlwingsは使用しません

## 必要権限

Cloud Run実行サービスアカウントに以下を付与してください。

- BigQueryジョブ実行プロジェクト: `roles/bigquery.jobUser`
- 参照するBigQueryデータセット: `roles/bigquery.dataViewer`
- 保存先GCS bucket: `roles/storage.objectAdmin` または書き込みに必要な最小権限

## デプロイ例

```bash
gcloud run deploy report-excel-generator ^
  --source . ^
  --region asia-northeast1 ^
  --allow-unauthenticated ^
  --set-env-vars PROJECT_ID=jumpplus-4a5f4,BUCKET_NAME=<bucket-name>,OBJECT_PREFIX=reports/plus
```

実運用では `--allow-unauthenticated` の代わりに、Cloud Schedulerから認証付きで呼び出す構成を推奨します。

## 配布機能（Firestore + Slack通知）

追加されたAPI:

- `POST /deliveries`: GCS上のExcelを配布レコード化し、ダウンロードURLを発行
- `GET /d/<token>`: メール入力フォーム
- `POST /d/<token>`: メールドメイン/メールアドレスを検証し、短時間Signed URLへリダイレクト

### 必要な環境変数

```text
BUCKET_NAME=ice-report-files
PROJECT_ID=jumpplus-4a5f4
PUBLIC_BASE_URL=https://<Cloud Run URL>
SLACK_WEBHOOK_SECRET=slack-download-webhook-url
DEFAULT_EXPIRES_DAYS=7
DOWNLOAD_SIGNED_URL_SECONDS=300
```

Slack Webhook URLはSecret Managerに `slack-download-webhook-url` として保存する想定です。
テスト用途では `SLACK_WEBHOOK_URL` を直接環境変数に設定することもできます。

### 配布レコード作成例

```bash
curl.exe -X POST -H "Content-Type: application/json" -d "{\"customer_name\":\"テスト顧客\",\"report_month\":\"2026-04\",\"gcs_uri\":\"gs://ice-report-files/reports/plus/2604/ダウンロード数入力シート_260507_ICE入力済み_plus.xlsx\",\"allowed_domains\":[\"example.com\"],\"expires_days\":7}" https://<Cloud Run URL>/deliveries
```

返却された `download_url` を顧客に送付します。

### 注意

現版は「token + 入力メールの許可ドメイン/許可メールチェック」です。
メールアドレスの所有確認（OTP/Identity Platform）は次段階で追加してください。


## 配布運用API

### 配布一覧

```cmd
curl.exe "https://report-generator-635067190197.asia-northeast1.run.app/deliveries?limit=20"
```

### ファイル差し替え（既存URLのまま最新版へ更新）

```cmd
curl.exe -X POST -H "Content-Type: application/json" -d "{\"gcs_uri\":\"gs://ice-report-files/reports/plus/2604/修正版.xlsx\",\"note\":\"修正版\"}" https://report-generator-635067190197.asia-northeast1.run.app/deliveries/<delivery_id>/versions
```

### 配布停止

```cmd
curl.exe -X POST https://report-generator-635067190197.asia-northeast1.run.app/deliveries/<delivery_id>/disable
```

### 配布再有効化

```cmd
curl.exe -X POST https://report-generator-635067190197.asia-northeast1.run.app/deliveries/<delivery_id>/enable
```

### ダウンロードログ一覧

```cmd
curl.exe "https://report-generator-635067190197.asia-northeast1.run.app/download-logs?limit=20"
```

### 任意の管理APIキー

`ADMIN_API_KEY` を環境変数に設定すると、以下の管理APIは `X-Admin-Key` ヘッダーが必要になります。

- `POST /deliveries`
- `GET /deliveries`
- `POST /deliveries/<delivery_id>/versions`
- `POST /deliveries/<delivery_id>/disable`
- `POST /deliveries/<delivery_id>/enable`
- `GET /download-logs`

例:

```cmd
curl.exe -H "X-Admin-Key: <ADMIN_API_KEY>" "https://.../deliveries"
```

## Admin UI

運用APIをブラウザから操作する簡易管理UIを追加しています。

- `/admin`: 配布一覧、配布作成、停止/再開、version追加、download logs確認

`ADMIN_API_KEY` を Cloud Run の環境変数に設定している場合、画面上部の「管理キー」に同じ値を入力してください。設定していない場合はキーなしで利用できます。
