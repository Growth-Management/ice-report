# Roadmap

ICE Report Generator の整備状況と今後の優先課題です。実装・運用の詳細は `docs/setup.md`、`docs/deploy.md`、`docs/operations.md`、`docs/security.md` を参照します。

## 現在の到達点

2026-06-03 時点で、次の基盤整備は完了済みです。

- Cloud Run `report-generator` のdeploy手順を `docs/deploy.md` に整理
- 本番運用、月次運用、smoke test、OTP送信停止時の一次対応、rollbackを `docs/operations.md` に整理
- Amazon SES本番送信を Web Identity 経路に切替済み
- SES production access、DKIM、custom MAIL FROM、送信可能状態を確認済み
- 長期AWS access key方式を本番経路から除外
- legacy Secret Manager secret `aws-ses-access-key-id` / `aws-ses-secret-access-key` を削除済み
- Cloud Monitoring の log-based metrics と alert policies を作成済み
- セキュリティ方針と残論点を `docs/security.md` に整理
- setup / roadmap の入口を `docs/setup.md` と本ファイルに整理

## 次の優先課題

### 1. Admin認証の強化

現状の管理APIは `ADMIN_API_KEY` と `X-Admin-Key` header で保護しています。短期運用には十分ですが、利用者単位の追跡や権限分離はできません。

実装済み:

- production fail-closed
- Admin auth failure logging
- break-glass / rotation runbook
- Admin認証docs更新

中期方針:

- 人間向け Admin UI は Admin専用 Cloud Run service + direct IAP を第一候補にする
- public service全体へIAPを直接適用する案は、download / OTP 経路への影響があるため非採用
- script/API向け `X-Admin-Key` は machine / break-glass 用に継続

継続課題:

- Admin専用service + IAP の最小検証
- 許可user/group、IAP service agent、rollback手順の実環境確認

### 2. Admin audit log

現状は download log と security event が中心で、管理操作の監査ログは十分ではありません。

追加候補:

- delivery 作成
- version 追加
- delivery disable / enable
- cleanup 実行
- admin key 認証失敗

### 3. Retention / archive lifecycle

月次運用のbaselineは `docs/operations.md` に整理済みです。保持期間、削除承認、Drive backup確認、Notion記録テンプレートは `docs/security.md` と `docs/operations.md` に反映済みです。

継続課題:

- Retention review 用の一覧抽出script: `scripts/check-retention-candidates.py`
- GCS report object dry-run: `python scripts\check-retention-candidates.py --scope gcs --check-gcs-exists`
- Firestore record dry-run: `python scripts\check-retention-candidates.py --scope firestore`
- Drive backup retention確認: 専用folder未確定時は個別backup URL必須、GCS削除不可
- Firestore record削除用の管理コマンドまたはscript
- Drive backupフォルダの実パス/運用担当者の明記
- 削除承認NotionテンプレートのDB化

### 4. Monitoring改善

critical系 alert と `/api-health` 外形監視は稼働しています。運用負荷を見ながらwarning系の通知経路とSES bounce / complaint連携を追加検討します。

継続課題:

- warning と critical の通知先分離
- alert threshold の実運用ノイズ調整
- SES bounce / complaint のCloudWatch / SES側監視連携

### 5. 運用確認の自動化

管理画面・配布一覧・月次操作の確認は手順化済みです。管理画面 screenshot と read-only operational check は `scripts/` に初期実装済みで、Notion転記用 summary も出力できます。

実装済み:

- `scripts/capture-admin-deliveries.ps1`
- `scripts/check-operations-readonly.ps1`

継続課題:

- read-only check の定期実行
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

継続課題:

- 既存履歴に含まれる可能性がある設定ファイルの確認と必要時の履歴対応判断
- Slack webhook URL の旧値無効化/再発行済み確認
- docs内の旧env名・旧access key前提手順の定期棚卸し実行と、想定外ヒット時の修正

## 判断済み事項

- 本番メール送信は SES + Web Identity を正とする
- 長期AWS access key方式へ戻さない
- `MAIL_PROVIDER=logging` は本番送信経路ではなく、切り分け・一時rollback用として扱う
- docsのみの変更はCloud Run deploy不要
- `app.py`、runtime設定、Dockerfile、requirements、テンプレート、SQL変更はdeploy対象として扱う
