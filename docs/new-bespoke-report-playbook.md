# 専用レポート追加プレイブック

既存の「レポート定義」機能(`docs/report-definitions-guide.md`)では対応できない、**別のBigQuery SQL・別のExcelテンプレート構造**を持つ新規レポートを追加するときの手順です。テルマエ・ロマエ月次販売報告書(`thermae_romae_report.py`)を参照実装とし、そこから抽出した共通部品の使い方をまとめます。

## これが必要になるケース

以下のいずれかに該当する場合、レポート定義機能では対応できません。このプレイブックに沿って専用実装を作ります。

- SQL本文が既存の `sql/paid.sql` / `sql/free.sql` と異なるBigQueryソースを使う
- Excelテンプレートのセル配置・シート構造が既存テンプレートと異なる
- 判断に迷う場合は `docs/report-definitions-guide.md` の「このガイドの対象外(非対応)の操作」を確認してください

**明示しておく非対応の設計判断**: ユーザーが管理画面から任意のSQL・任意のセルマッピングをアップロードできる汎用エンジンは作りません。本番の顧客向けデータ配信経路で任意クエリ実行を安全に許可するコストが見合わないためです(BigQuery MCPツール整備時の検討と同じ理由)。新しいレポートは、このプレイブックに沿って都度エンジニアが専用実装を書く前提とします。

## 着手前に決めること

1. **データソース**: 対象のBigQueryプロジェクト・データセット・テーブル、集計ロジック
2. **配布方式**(どちらかを選ぶ):
   - **Drive保存のみ**(OTP/ダウンロードURL不要): テルマエ・ロマエ方式。納品物をDriveへ置くだけで良い場合
   - **OTP付きダウンロードURLで配布**: 既存の `distribution.py`(`create_delivery_record` など)を使い、レポート定義の仕組みを流用しない専用の配布経路を作る場合
   - 判断基準: 顧客が自分でダウンロードURLを開く運用が必要か、Drive納品だけで完結するか
3. **テンプレート所有**: 専用Excelテンプレートをどこに置くか(Drive固定ファイル、GCS)、更新運用の担当者
4. **スケジュール実行の要否・頻度**: 手動実行のみか、Cloud Scheduler経由の自動実行が必要か

## 実装の型(テルマエ・ロマエを参照実装として)

### 専用モジュールを1つ作る

`<report_name>_report.py` のように、既存の `thermae_romae_report.py` と同じ構成にします。

- 日付ユーティリティ(対象月のパース・前月計算など)
- BigQuery実行関数(`bigquery.Client(project=...).query(sql).to_dataframe()` 相当)
- Excelセル書き込みロジック — **ここは汎用化しません**。テンプレートのシート構造・見出し位置・罫線・集計行の位置は新レポートごとに異なるため、都度実装します
- オーケストレーション関数(query実行 → workbook組み立て → 保存/アップロード)

### app.py に追加するエンドポイント

- `POST /admin/reports/<report-name>/generate` — 手動実行用。`X-Admin-Key` または IAPで保護(`_check_admin()`)
- `POST /admin/reports/<report-name>/scheduled-generate` — Cloud Scheduler専用。OIDCで保護

### 再利用できる既存の共通部品

新しいレポートを作る際は、以下は書き直さず再利用してください。

| 部品 | 場所 | 用途 |
| --- | --- | --- |
| `download_drive_file` / `upload_xlsx_to_drive` | `drive_io.py` | Driveからのテンプレート取得・生成物アップロード。既に汎用実装済み |
| `_check_scheduler_oidc_auth(*, env_prefix, log_tag)` | `app.py` | Cloud Scheduler経由のOIDC bearer token検証。新レポート用のenv prefix(例 `NEWREPORT_SCHEDULER`)を渡すだけで使える |
| `_claim_scheduled_run(*, collection_name, run_id, initial_fields)` | `app.py` | Firestoreで重複実行を防止する「claim」処理。新レポート用のFirestore collection名とrun_idを渡すだけで使える。claim成功時に返る`run_ref`へ、処理完了後に`.update(...)`で最終ステータスを書き込むのは呼び出し側の責務 |
| `_log_admin_audit_event` | `app.py` | 管理操作の監査ログ記録 |

`_check_scheduler_oidc_auth` と `_claim_scheduled_run` は、テルマエ・ロマエと本体 `report_definitions` スケジューラの実装が実質同一だったため共通化したものです。3つ目のレポートを追加する際は、この2つを呼び出すだけで認証・重複防止を実装できます。

### Cloud Scheduler設定(自動実行が必要な場合)

`thermae-romae-monthly-report` ジョブを参考にします。

1. 専用service account作成(例: `<report-name>-scheduler@ice-sh.iam.gserviceaccount.com`)
2. Cloud Runへ `<PREFIX>_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS` / `<PREFIX>_SCHEDULER_AUDIENCE` を設定
3. Cloud Scheduler jobを作成し、OIDC token(上記service account、audienceは`scheduled-generate`のURL)で呼び出す

## チェックリスト

- [ ] データソーステーブル・集計ロジックを確定した
- [ ] Excelテンプレートのシート構造・見出し位置・固定行列レイアウトを確認し、実装前にメモした
- [ ] 配布方式(Drive限定 / OTP付き)を決定した
- [ ] Drive/GCSの権限を確認した(Shared Drive制約の有無は `docs/drive-domain-wide-delegation.md` 参照)
- [ ] 手動生成エンドポイントのsmokeを実施した
- [ ] スケジュール実行が必要な場合: 専用SA作成・env設定・Cloud Scheduler job作成・OIDC smoke・重複実行(409)smokeを実施した
- [ ] `docs/<report-name>-report.md` を作成した(`docs/thermae-romae-report.md` を参考に)
- [ ] 監査ログ・Slack通知に secret、PIN、生メール、token断片、Admin key fingerprint、IP、user agent、Signed URL、SQL本文、Excelセル値が出ていないことを確認した

## 関連ドキュメント

- `docs/report-definitions-guide.md` — レポート定義機能で対応できる範囲との切り分け
- `docs/thermae-romae-report.md` — 参照実装の詳細仕様
- `docs/schedule-automation-guarded-executor.md` — 本体`report_definitions`スケジューラの安全設計(考え方は共通)
- `docs/drive-domain-wide-delegation.md` — Drive連携の権限設計
