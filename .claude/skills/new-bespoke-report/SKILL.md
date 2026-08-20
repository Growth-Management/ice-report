---
name: new-bespoke-report
description: Use when the user asks to add a new report to the ICE Report Generator that needs a different BigQuery SQL query and/or a different Excel template layout than the existing reports — e.g. "新しいレポートを追加したい", "別のSQL・別のExcelテンプレートで新規レポートを作りたい", "add a new report with its own query and template", "テルマエ・ロマエみたいな専用レポートを作りたい". Also use to help decide whether a request actually needs this (vs. the existing report_definitions Admin UI feature). Do NOT use when the new "report" only needs a different destination/customer/schedule/allowlist on the SAME Jump+ paid/free-downloads SQL and template — that's the existing report_definitions feature (docs/report-definitions-guide.md), no code required.
version: 1.0.0
---

# 専用レポートの追加(new-bespoke-report)

ICE Report Generator(Flask on Cloud Run。BigQueryからExcelを生成し、DriveまたはOTP付きダウンロードURLで配布する)へ、**既存の「レポート定義」機能では対応できない**新しいレポートを追加する作業をガイドします。レポート定義機能は `.xlsx` テンプレートファイルとメタデータ(宛先・schedule・allowlist)だけを差し替える仕組みで、BigQuery SQLとExcelセルマッピングは既存のJump+有料/無料DL集計に固定されています。SQL・テンプレート構造そのものが異なる新規レポートには使えません。

着手前に `docs/new-bespoke-report-playbook.md` と、参照実装 `thermae_romae_report.py` を読んでください。この2つが正であり、このSKILL.mdは作業の進め方をまとめた入口です。

## Step 0 — このskillが本当に必要か確認する

まだ確認していなければ聞く: 新しいレポートは、**既存と別のSQL、または別のExcelレイアウト**が必要か?

- 宛先・顧客・schedule・allowlistだけが違う(SQL・テンプレート構造は既存と同じ)場合 → `docs/report-definitions-guide.md` の「レポート定義の追加」で対応可能。Admin UIの操作のみで済み、コード変更は不要。ここで案内を止める
- SQLまたはExcelレイアウトが実質的に別物 → Step 1へ進む

## Step 1 — 着手前に聞くこと(まとめて質問する)

1. **データソース**: 対象のBigQueryプロジェクト・データセット・テーブル、集計ロジックは何か
2. **配布方式**:
   - Drive保存のみ(OTP/ダウンロードURL不要) → テルマエ・ロマエ方式
   - OTP付きダウンロードURLで配布 → `distribution.py` の `create_delivery_record` 等を再利用する専用経路
3. **テンプレート所有**: `.xlsx` テンプレートをどこに置くか(Drive固定ファイル、GCSなど)、更新運用の担当者
4. **スケジュール実行の要否・頻度**: 手動実行のみか、Cloud Scheduler自動実行が必要か

これらが決まるまで実装に着手しない。

## Step 2 — 専用モジュールを作る

`<report_name>_report.py` を `thermae_romae_report.py` と同じ構成で新規作成する。

- 日付ユーティリティ(対象月パース、前月計算など)
- BigQuery実行関数
- Excelセル書き込みロジック — **このレポートのテンプレート構造専用**。汎用化しようとしない(テンプレートごとに構造が異なるため)
- オーケストレーション関数(query実行 → workbook組み立て → 保存/アップロード)

## Step 3 — app.py にエンドポイントを追加する

- `POST /admin/reports/<report-name>/generate` — 手動実行。`_check_admin()` で保護
- `POST /admin/reports/<report-name>/scheduled-generate` — 自動実行が必要な場合のみ。OIDCで保護

## Step 4 — 既存の共通部品を再利用する(書き直さない)

| 必要なもの | 使うもの |
| --- | --- |
| Driveのテンプレート取得・生成物アップロード | `drive_io.py` の `download_drive_file` / `upload_xlsx_to_drive` |
| Cloud Scheduler OIDC認証 | `app.py` の `_check_scheduler_oidc_auth(env_prefix=..., log_tag=...)` |
| スケジュール重複実行防止 | `app.py` の `_claim_scheduled_run(collection_name=..., run_id=..., initial_fields=...)` |
| 管理操作の監査ログ | `app.py` の `_log_admin_audit_event(...)` |

`_check_scheduler_oidc_auth` と `_claim_scheduled_run` は、テルマエ・ロマエと本体schedulerの重複実装を統合した共通関数です。新レポートのScheduler認証・重複防止は、これらを呼ぶだけで実装できます。

## Step 5 — 明示的に対応しないこと

ユーザーが管理画面から任意のSQL・任意のセルマッピングをアップロードできる汎用エンジンは作らない。これは意図的なセキュリティ判断であり(`docs/report-definitions-guide.md`、`docs/new-bespoke-report-playbook.md` 参照)、本番の顧客向けデータ配信経路で任意クエリ実行を安全に許可するコストが見合わないためです。新しいレポートは都度、エンジニアが専用実装を書く前提を維持してください。

## Step 6 — Cloud Scheduler設定(自動実行が必要な場合のみ)

`thermae-romae-monthly-report` ジョブを参考にする。

1. 専用service account作成(例: `<report-name>-scheduler@ice-sh.iam.gserviceaccount.com`)
2. Cloud Runへ `<PREFIX>_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS` / `<PREFIX>_SCHEDULER_AUDIENCE` を設定
3. Cloud Scheduler jobを作成し、上記service accountのOIDC tokenで呼び出す

## Step 7 — 完了前のチェックリスト

- [ ] データソーステーブル・集計ロジックを確定した
- [ ] Excelテンプレートの構造(シート名、見出し位置、固定行列レイアウト)を実装前に確認・メモした
- [ ] 配布方式(Drive限定 / OTP付き)を決定した
- [ ] Drive/GCSの権限を確認した(Shared Drive制約は `docs/drive-domain-wide-delegation.md` 参照)
- [ ] 手動生成エンドポイントのsmokeを実施した
- [ ] スケジュール実行が必要な場合: 専用SA作成・env設定・Cloud Scheduler job作成・OIDC smoke・重複実行(409)smokeを実施した
- [ ] `docs/<report-name>-report.md` を作成した(`docs/thermae-romae-report.md` を参考に)
- [ ] 監査ログ・Slack通知に secret、PIN、生メール、token断片、Admin key fingerprint、IP、user agent、Signed URL、SQL本文、Excelセル値が出ていないことを確認した
- [ ] 本番反映は `docs/deploy.md` の手順(build --no-cache → push → deploy → smoke)に従い、`app.py` 変更のため `report-generator` と `report-generator-admin` の両方をdeployした

## 参照ファイル

- `docs/new-bespoke-report-playbook.md` — 設計判断の詳細
- `thermae_romae_report.py` — 参照実装
- `docs/thermae-romae-report.md` — 参照実装の個別仕様
- `docs/report-definitions-guide.md` — このskillが対象外とする境界線(レポート定義機能で足りるケース)
