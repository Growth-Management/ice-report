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
- Admin key rotation helper: `scripts/rotate-admin-key.ps1`
- 複数管理者向けの明示user allowlist設計とread-only check期待user照合
- Admin IAP drift check を定期 read-only wrapper へ統合
- Phase 7 final decision: 人間向け Admin UI は `report-generator-admin` + Cloud Run direct IAP を主経路にする

中期方針:

- 人間向け Admin UI は Admin専用 Cloud Run service + direct IAP を主経路にする
- public service全体へIAPを直接適用する案は、download / OTP 経路への影響があるため非採用
- script/API向け `X-Admin-Key` は machine / break-glass 用に継続
- Google Group 管理は現時点では採用しない。複数管理者が必要になった場合も、当面はIAP policyと `ADMIN_IAP_ALLOWED_EMAILS` への明示user追加で対応する
- Google Group 管理は、管理者数や退任・異動時の運用負荷が明示user管理で維持できなくなった時点で再検討する
- public serviceの `/admin` を閉じる、またはAdmin UIをAdmin専用serviceだけに限定する変更は、runtime影響とrollback条件を別タスクで確認してから実施する

継続課題:

- 複数管理者を実際に追加した後の期待user更新とdrift実績確認

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
- Admin audit log定期確認を read-only operational check と同じwrapperへ統合
- Admin audit recent failure の安全な抜粋と検索filter出力

継続課題:

- 実運用後の検索view / 転記粒度の微調整

### 3. Retention / archive lifecycle

月次運用のbaselineは `docs/operations.md` に整理済みです。保持期間、削除承認、Drive backup確認、Notion記録テンプレートは `docs/security.md` と `docs/operations.md` に反映済みです。

実装済み:

- Retention review 用の一覧抽出script: `scripts/check-retention-candidates.py`
- GCS report object dry-run: `python scripts\check-retention-candidates.py --scope gcs --check-gcs-exists`
- Firestore record dry-run: `python scripts\check-retention-candidates.py --scope firestore`
- Drive backup retention確認: 対象レポートの保存先配下未確認時は個別backup URL必須、GCS削除不可
- Retention削除承認テンプレート: `docs/retention-deletion-approval-template.md`
- 承認manifest必須のFirestore record削除管理script: `scripts/delete-firestore-retention-records.py`
- 現在のレポート保存先、運用担当、レポート別格納先前提、例外時の個別backup URL必須条件を明記
- Retention削除承認テンプレートのNotion DB化は現時点では見送り、再開条件をdocsへ明記

継続課題:

- 削除承認が継続的に発生した場合のNotion DB化再起票

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
- Monitoring noise review を定期 read-only wrapper へ統合

継続課題:

- 実運用後の通知先・threshold微調整

### 5. 運用確認の自動化

管理画面・配布一覧・月次操作の確認は手順化済みです。管理画面 screenshot と read-only operational check は `scripts/` に初期実装済みで、Notion転記用 summary も出力できます。

実装済み:

- `scripts/capture-admin-deliveries.ps1`
- `scripts/check-operations-readonly.ps1`
- `scripts/run-operations-readonly-scheduled.ps1`
- read-only check 定期実行方針: 週1回、deploy後、incident後
- Notion API への直接記録オプション
- Notion直接記録の preview、run key重複防止、送信前redaction、禁止項目テスト
- deploy pipeline からのread-only check自動実行
- GitHub Actions `Operations Read-Only Check` による週次 read-only check
- pipeline check run metadata artifact: duration、exit code、failed checks、audit review成否
- pipeline check失敗時の確認順と所要時間見直し条件
- Admin IAP drift check artifactを定期read-only checkに統合
- docs legacy reference check artifactを定期read-only checkに統合

継続課題:

- 実運用後の複数回実績に基づくpipeline timeout / 権限 / threshold微調整

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
- Slack側の旧webhook無効化確認: 現行webhookは1件のみ
- 現時点のメタデータではgit履歴rewriteを必須にしない判断を記録
- `scripts/check-secret-exposure-metadata.ps1` にrepo hygiene判断用summaryを追加
- docs legacy reference check を定期 read-only wrapper へ統合
- secret exposure metadata review を定期 read-only wrapper へ統合

継続課題:

- docs legacy reference check の実績に基づく許容リスト / 修正対象の微調整

## Phase 7 Operational hardening / automation

Phase 7 は、主要基盤の実装後に残る継続課題を日常運用へ定着させる
フェーズです。新機能追加よりも、確認作業の自動化、監査ログの扱い、
通知ノイズ調整、運用上の判断記録を優先します。

実行順:

1. Phase 7継続課題整理とroadmap反映
2. read-only operational check定期実行設計: 実装済み
3. Admin audit log検索view / 転記粒度整理: 実装済み
4. Monitoring warning/critical通知分離・threshold見直し: 実装済み
5. repo hygiene残件確認: 実装済み
6. Admin専用service本番運用移行判断: 実装済み

Phase 7 の完了条件:

- read-only operational check の実行頻度、実行主体、保存先、失敗時対応が決まっている
- Admin audit log の確認観点とNotion転記粒度が整理されている
- warning / critical alert の通知分離要否とthreshold調整方針が記録されている
- repo hygiene の残件が確認され、追加対応の要否が切り分けられている
- Admin専用serviceを主経路にする範囲と `X-Admin-Key` break-glass条件が判断記録されている

Phase 7 final status:

- 2026-06-26 時点で実行順1-6は実装済み
- 週次 read-only check、Admin audit、monitoring noise、repo hygiene、Admin IAP drift は
  scheduled wrapper / GitHub Actions / docs に反映済み
- 残る継続課題は、実運用後の複数回実績に基づく微調整と、複数管理者追加時の
  allowlist / IAP policy 更新確認

## 次Phase: Phase 8 Production rollout / multi-report operations

Phase 8 は、Phase 7 で定着させた監視・監査・read-only確認を前提に、
実運用で複数レポートを継続的に扱うための運用品質を上げるフェーズです。
大きな認証方式変更や破壊的cleanupよりも、月次作業の再現性、保存先管理、
本番操作後の確認実績、問い合わせ時の調査導線を優先します。

実行順:

1. 複数レポート運用baseline整理: 実装済み
2. レポート別 Drive 保存先 / backup URL 記録ルールの実運用確認: 実装済み
3. 月次 delivery create / version add / disable-enable の人間向け smoke 実績記録: 実装済み
4. read-only wrapper の週次実行結果を3回分確認し、timeout / 権限 / threshold の変更要否を判断: 成功run 3/3確認済み、追加変更不要
5. public service `/admin` の扱いを再評価し、閉鎖またはAdmin専用service限定の要否を判断: 現状維持判断済み
6. 複数管理者を追加する場合の IAP policy / `ADMIN_IAP_ALLOWED_EMAILS` 更新手順を実運用で確認: 手順化済み、実追加は発生時

実装済み:

- 複数レポート運用 baseline を `docs/operations.md` に追加
- 現在の baseline として `OMFダウンロード数報告` の GCS prefix、Drive folder、
  owner、primary operator、保存先の扱いを整理
- Drive backup 記録テンプレートを `docs/operations.md` に追加
- retention削除承認テンプレートへ backup location status と確認日時を追加
- 月次 Admin UI human smoke 記録テンプレートを `docs/operations.md` に追加
- 週次 read-only check 3回レビューの記録テンプレートと判断条件を
  `docs/operations.md` に追加
- public service `/admin` は Phase 8 時点では現状維持とし、
  Admin専用service限定はruntime影響、既存script/API、deploy smoke、rollback条件を
  分離確認してから再判断する方針を `docs/operations.md` に追加
- 複数管理者を明示userで追加・削除する場合の IAP policy、
  `ADMIN_IAP_ALLOWED_EMAILS`、drift確認、記録テンプレートを
  `docs/operations.md` に追加
- `Operations Read-Only Check` workflow の成功run 3回分を確認済み
  - 2026-06-29 手動run `28352409745` は success。1/3
  - 2026-06-30 手動run `28408454687` は success。2/3
  - 2026-06-30 手動run `28409091044` は success。3/3
  - workflow timeout / 権限 / monitoring threshold の追加変更は不要
- GitHub Actions実行主体への Cloud Monitoring read と IAP policy read 権限追加は完了
  - `roles/monitoring.viewer`
  - `projects/ice-sh/roles/iceReportIapPolicyViewer`
  - `setIamPolicy` は付与しない
- Phase 8 の完了条件は満たした。追加の実行可能な Phase 8 残タスクはなし

後続Phaseまたは発生時対応:

- public service `/admin` 閉鎖またはAdmin専用service限定の再判断は、
  IAP経路での人間操作実績とscript/API移行影響確認が必要になった時点で別Phase化する
- 複数管理者の実追加smokeは、追加対象userが決まった時点で実施する

Phase 8 の完了条件:

- sinoharaアカウントで複数レポートの delivery 作成、version追加、停止/再有効化、DLログ確認を安全に実施できる
- レポートごとの Drive 保存先、backup URL、operational owner、primary operator が運用記録に残る
- 週次 read-only check の複数回実績から、workflow timeout / 権限 / monitoring threshold の変更要否が判断されている
- public `/admin` を維持するか、Admin専用serviceへ寄せるかの次判断が記録されている
- 複数管理者が必要になった場合の明示user追加・削除・drift確認が手順化されている

Phase 8 で急がないもの:

- Google Group 管理への切替
- public service全体へのIAP適用
- GCS / Firestore の実削除自動化
- 履歴rewrite

## Phase 9 Admin UI expansion / report definitions

Phase 9 は、複数レポート運用を前提に Admin UI を段階拡張するフェーズです。
人間向け Admin UI は `report-generator-admin` + Cloud Run direct IAP を主経路にし、
public service 全体へ IAP は適用しません。script/API/break-glass 用の
`X-Admin-Key` は継続します。

実装順:

1. 管理画面拡張ロードマップと安全ルール: 実装済み
2. 利用者UIで選択中レポートを明確に表示: 実装済み
3. Admin read-only のレポート定義一覧: 実装済み
4. Firestore report definitions と version 管理: read-only foundation 実装済み
5. レポート定義の追加・編集・archive: 実装済み
6. Excelテンプレート upload / preview / publish / rollback: publish / rollback foundation 実装済み
7. query config と template mapping の dry-run / preview / publish: dry-run preview / publish foundation 実装済み
8. schedule 設定と ON/OFF: foundation 実装済み
9. Google Drive / GCS 保存先 allowlist 管理: foundation 実装済み

安全ルール:

- 最初から SQL編集、template publish、schedule自動化、Drive任意保存先変更へ進まない
- 表示、read-only、versioning、preview/dry-run、publish、rollback、automation の順で進める
- Admin UI は Admin専用service `report-generator-admin` + IAP を主経路とし、public service全体へIAPを適用しない
- 管理者は Google Group ではなく、当面は明示user allowlistで管理する
- 利用者ログイン制限を追加する場合は、OTP/PINフロー内で `impress.co.jp` ドメイン判定を行う
- secret、PIN、生メール、token断片、Admin key fingerprint、IP、user agent、Signed URL は新規表示・記録に含めない

今回実装した範囲:

- 利用者向けOTP画面に、選択中レポートの顧客、対象月、current version、file、期限、状態を表示
- Admin UI に Firestore `report_definitions` の read-only 一覧を追加
- `/report-definitions` は管理認証必須の read-only API とし、SQL、mapping、メールallowlist、token、Signed URL は返さない
- `/report-definitions/<report_id>` は個別定義とversion履歴をread-onlyで返す。version履歴は version、status、note、template名、query config ID、mapping version ID、日時だけに限定する
- Admin UI と管理APIでレポート定義の作成、編集、archiveを追加。扱う項目は report_id、name、owner、primary_operator、customer_name、default_report_month、GCS prefix、Drive folder name、initial version note に限定する
- Admin UI と管理APIでExcelテンプレートのpreviewを追加。アップロードされた `.xlsx` は保存せず、シート名、行数、列数、テーブル数、サイズ、sha256だけを返す。セル値は返さない
- Admin UI と管理APIでExcelテンプレートのpublish / rollback foundationを追加。publishは管理用GCS prefixへ保存し、report definition のversion metadataとcurrent_versionだけを更新する。rollbackは既存versionへcurrent_versionを戻す
- レポート生成時に published template を利用する切替は `report_id` gated foundation として実装済み。SQL編集、template mapping編集、schedule自動化、保存先変更は未実装

Published template runtime switch design:

- Runtime design is tracked in `docs/published-template-runtime-switch.md`.
- The first implementation uses a published template only when an admin explicitly passes `report_id` to `/generate` or `/deliveries` generation.
- Existing generation without `report_id` must continue using `TEMPLATE_PATH` or bundled `templates/template.xlsx`.
- If `report_id` is present and the current report definition version has no `template_gcs_uri`, generation must fail closed before BigQuery execution.
- API responses and logs must not include `template_gcs_uri`, Signed URL, Excel cell values, SQL text, template mapping details, raw email, token fragments, IP, user agent, or Admin key fingerprint.

Query config / template mapping dry-run preview foundation:

- Admin API/UI can run a dry-run preview for the existing default query config and table mapping.
- This step does not edit SQL, edit mapping, publish query config, publish mapping, schedule jobs, or change storage destinations.
- Preview responses include query IDs, SQL file names, dry-run status, bytes processed, mapping source IDs, expected column counts, and total-row flags.
- Preview responses and logs must not include SQL text, template mapping cell details, Excel cell values, raw email, token fragments, Signed URL, IP, user agent, or Admin key fingerprint.

Query config / template mapping publish foundation:

- Admin API/UI can publish the approved default query config ID and template mapping version ID as report definition version metadata.
- Publish appends a new report definition version and updates `current_version`.
- When the previous current version has published template metadata, the new version carries forward the internal template reference so runtime generation keeps using the published template.
- This step does not edit SQL, edit mapping cells, upload templates, create schedule jobs, or change Drive/GCS destinations.
- Publish responses and logs must not include SQL text, template mapping cell details, Excel cell values, template GCS URI, raw email, token fragments, Signed URL, IP, user agent, or Admin key fingerprint.
- Rollback remains a version-level `current_version` rollback to a known previous version.

Report definition version rollback foundation:

- Admin API/UI can move `current_version` back to any existing report definition version.
- The generic endpoint is `/report-definitions/<report_id>/version-rollback`.
- The existing `/report-definitions/<report_id>/template-rollback` endpoint remains for compatibility.
- Rollback changes only `current_version` and `updated_at`; it does not edit template, query config, mapping, schedule, or storage fields.
- Rollback responses and logs must not include SQL text, template mapping cell details, Excel cell values, template GCS URI, raw email, token fragments, Signed URL, IP, user agent, or Admin key fingerprint.

Schedule ON/OFF foundation:

- Admin API/UI can save monthly schedule metadata per report definition.
- The first implementation stores only `enabled`, `frequency=monthly`, `day_of_month`, `time_of_day`, and `timezone=Asia/Tokyo`.
- This step does not create Cloud Scheduler jobs, trigger automatic generation, edit SQL, edit template mapping, or change Drive/GCS destinations.
- Schedule responses and logs must not include secret, PIN, raw email, token fragments, Signed URL, IP, user agent, Admin key fingerprint, SQL text, or template mapping details.

Schedule automation dry-run preview foundation:

- Admin API/UI can run a read-only schedule preview at `/report-definitions/schedule-preview`.
- Preview evaluates enabled monthly schedules against the current evaluation time and returns due candidates with safe metadata only.
- This step does not create Cloud Scheduler jobs, trigger report generation, create deliveries, send mail, edit SQL, edit template mapping, or change Drive/GCS destinations.
- Preview responses and logs must not include secret, PIN, raw email, token fragments, Signed URL, IP, user agent, Admin key fingerprint, SQL text, template mapping details, template GCS URI, or Excel cell values.

Storage destination allowlist foundation:

- Admin API/UI can show the configured report definition storage allowlist.
- Report definition create/update validates `gcs_prefix` and `drive_folder_name` when those fields are set.
- Allowed GCS prefixes come from `REPORT_ALLOWED_GCS_PREFIXES`; allowed Drive folder names or IDs come from `REPORT_ALLOWED_DRIVE_FOLDERS`.
- Defaults are limited to the current baseline: `gs://ice-report-files/reports/plus/`, `reports/plus/`, `OMFダウンロード数報告`, and `126n9wGJ9DMU3hR-4yPgsd-atLhaeRdVt`.
- This step does not create Drive folders, move GCS objects, register arbitrary Drive URLs, or change runtime delivery destinations automatically.
- Allowlist responses and logs must not include secret, PIN, raw email, token fragments, Signed URL, IP, user agent, Admin key fingerprint, SQL text, or template mapping details.

Cloud Run / smoke / rollback:

- `app.py` と `distribution.py` を変更するため、反映には `report-generator-admin` と必要に応じて `report-generator` のdeployが必要
- smoke は Admin IAP ログイン後にレポート定義一覧が表示されること、既存配布一覧・DLログが表示されること、利用者OTP画面に選択中レポートが表示されることを確認する
- rollback は直前 Cloud Run revision へ戻すか、このPRをrevertする

## 判断済み事項

- 本番メール送信は SES + Web Identity を正とする
- 長期AWS access key方式へ戻さない
- `MAIL_PROVIDER=logging` は本番送信経路ではなく、切り分け・一時rollback用として扱う
- docsのみの変更はCloud Run deploy不要
- `app.py`、runtime設定、Dockerfile、requirements、テンプレート、SQL変更はdeploy対象として扱う
