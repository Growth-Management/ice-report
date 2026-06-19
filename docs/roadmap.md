# Roadmap

ICE Report Generator の整備状況と今後の優先課題です。実装・運用の詳細は `docs/setup.md`、`docs/deploy.md`、`docs/operations.md`、`docs/security.md` を参照します。

## 現在の到達点

2026-06-19 時点で、次の基盤整備は完了済みです。

- Cloud Run `report-generator` のdeploy手順を `docs/deploy.md` に整理
- 本番運用、月次運用、smoke test、OTP送信停止時の一次対応、rollbackを `docs/operations.md` に整理
- Amazon SES本番送信を Web Identity 経路に切替済み
- SES production access、DKIM、custom MAIL FROM、送信可能状態を確認済み
- 長期AWS access key方式を本番経路から除外
- legacy Secret Manager secret `aws-ses-access-key-id` / `aws-ses-secret-access-key` を削除済み
- Cloud Monitoring の log-based metrics と alert policies を作成済み
- セキュリティ方針と残論点を `docs/security.md` に整理
- setup / roadmap の入口を `docs/setup.md` と本ファイルに整理
- Admin専用 Cloud Run service + direct IAP の検証とrollback手順を整理済み
- SES bounce / complaint 監視用 SNS topic と CloudWatch alarm を作成済み
- Retention / archive lifecycle の dry-run script、runbook、削除承認テンプレートを整備済み

## 次の優先課題

### 1. Admin認証の強化

現状の管理APIは `ADMIN_API_KEY` と `X-Admin-Key` header で保護しています。短期運用には十分ですが、利用者単位の追跡や権限分離はできません。

実装済み:

- production fail-closed
- Admin auth failure logging
- break-glass / rotation runbook
- Admin認証docs更新
- Admin専用 Cloud Run service `report-generator-admin`
- direct IAP login smoke
- Admin専用service rollback / cutover手順
- IAP read-only smoke script
- 初期IAP許可user: `sinohara@impress.co.jp`
- Admin専用service本番運用移行判断

中期方針:

- 人間向け Admin UI は Admin専用 Cloud Run service + direct IAP を主経路にする
- public service全体へIAPを直接適用する案は、download / OTP 経路への影響があるため非採用
- script/API向け `X-Admin-Key` は machine / break-glass 用に継続
- Google Group 管理は現時点では採用しない。複数管理者が必要になった場合に再検討する
- public serviceの `/admin` を閉じる、またはAdmin UIをAdmin専用serviceだけに限定する変更は、runtime影響とrollback条件を別タスクで確認してから実施する

継続課題:

- 複数管理者が必要になった場合のIAP許可user / group設計
- `X-Admin-Key` break-glass利用後のrotation運用徹底

### 2. Admin audit log

管理操作の監査ログは、Firestore `admin_audit_logs` と Cloud Logging
`ICE_REPORT_ADMIN_AUDIT` を使う方針に整理済みです。

実装済み:

- delivery 作成
- version 追加
- delivery disable / enable
- cleanup 実行
- admin key 認証失敗
- Admin audit log検索view: `scripts/check-admin-audit-logs.ps1`
- Notion転記粒度と転記禁止項目のrunbook

継続課題:

- 実運用後の検索view / 転記粒度の微調整
- 監査ログ定期確認を read-only operational check と同じスケジュールに寄せるかの判断

### 3. Retention / archive lifecycle

月次運用のbaselineは `docs/operations.md` に整理済みです。保持期間、削除承認、Drive backup確認、Notion記録テンプレートは `docs/security.md` と `docs/operations.md` に反映済みです。

実装済み:

- Retention review 用の一覧抽出script: `scripts/check-retention-candidates.py`
- GCS report object dry-run: `python scripts\check-retention-candidates.py --scope gcs --check-gcs-exists`
- Firestore record dry-run: `python scripts\check-retention-candidates.py --scope firestore`
- Drive backup retention確認: 専用folder未確定時は個別backup URL必須、GCS削除不可
- Retention削除承認テンプレート: `docs/retention-deletion-approval-template.md`

継続課題:

- Firestore record削除用の管理コマンドまたはscript
- Drive backupフォルダの実パス/運用担当者の明記
- 削除承認NotionテンプレートのDB化

### 4. Monitoring改善

critical系 alert、`/api-health` 外形監視、SES bounce / complaint warning
監視は稼働しています。

実装済み:

- `/api-health` uptime check / alert
- critical系 log-based metrics / alert policies
- SES reputation SNS topic: `ice-report-ses-reputation-alerts`
- SES bounce warning alarm: `ice-report-ses-bounce-rate-warning`
- SES complaint warning alarm: `ice-report-ses-complaint-rate-warning`
- SES reputation warning一次対応runbook
- Monitoring noise review: `scripts/check-monitoring-noise.ps1`
- warning / critical 通知分離とthreshold変更条件の判断記録

継続課題:

- 実運用後の通知先・threshold微調整

### 5. 運用確認の自動化

管理画面・配布一覧・月次操作の確認は手順化済みです。管理画面 screenshot と read-only operational check は `scripts/` に初期実装済みで、Notion転記用 summary も出力できます。

実装済み:

- `scripts/capture-admin-deliveries.ps1`
- `scripts/check-operations-readonly.ps1`
- `scripts/run-operations-readonly-scheduled.ps1`
- read-only check 定期実行方針: 週1回、deploy後、incident後

継続課題:

- deploy pipeline からの自動実行
- Notion API への直接記録

### 6. repo hygiene

setupとroadmapは現状に合わせて整理済みです。機密情報を含み得る設定ファイルとサンプル設定の分離を開始しています。

実装済み:

- `env.example.yaml` の追加
- `env.yaml`、`webhook.txt`、`.env*`、`tools.yaml`、`*_accessKeys.csv` のignore対象化
- trackedだった `env.yaml` / `webhook.txt` をgit管理対象から除外
- secret値を読まないメタデータ監査script `scripts/check-secret-exposure-metadata.ps1` の追加
- `report-generator-admin-api-key` 旧version 1 のdisableとread-only smoke確認
- `docs/setup.md` にローカル/private設定ファイルの扱いを追記
- `scripts/check-secret-exposure-metadata.ps1` の対象を `.env*`、`tools.yaml`、`*_accessKeys.csv` まで拡張
- repo hygiene残件確認runbookを `docs/security.md` に追加

継続課題:

- Slack側で旧webhookがrevoke / delete済みであることの最終確認
- 必要時のgit履歴rewrite判断
- docs内の旧env名・旧access key前提手順の定期棚卸し継続

## 次Phase: Phase 7 Operational hardening / automation

Phase 7 は、主要基盤の実装後に残る継続課題を日常運用へ定着させる
フェーズです。新機能追加よりも、確認作業の自動化、監査ログの扱い、
通知ノイズ調整、運用上の判断記録を優先します。

実行順:

1. Phase 7継続課題整理とroadmap反映
2. read-only operational check定期実行設計
3. Admin audit log検索view / 転記粒度整理
4. Monitoring warning/critical通知分離・threshold見直し
5. repo hygiene残件確認
6. Admin専用service本番運用移行判断

Phase 7 の完了条件:

- read-only operational check の実行頻度、実行主体、保存先、失敗時対応が決まっている
- Admin audit log の確認観点とNotion転記粒度が整理されている
- warning / critical alert の通知分離要否とthreshold調整方針が記録されている
- repo hygiene の残件が確認され、追加対応の要否が切り分けられている
- Admin専用serviceを主経路にする範囲と `X-Admin-Key` break-glass条件が判断記録されている

## 判断済み事項

- 本番メール送信は SES + Web Identity を正とする
- 長期AWS access key方式へ戻さない
- `MAIL_PROVIDER=logging` は本番送信経路ではなく、切り分け・一時rollback用として扱う
- docsのみの変更はCloud Run deploy不要
- `app.py`、runtime設定、Dockerfile、requirements、テンプレート、SQL変更はdeploy対象として扱う
