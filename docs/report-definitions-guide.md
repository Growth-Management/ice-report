# レポート定義の追加・使い方ガイド

Admin UI(`report-generator-admin`)の「レポート定義管理」タブを使って、新しいレポート定義を追加・設定するための手順書です。設計の詳細は `docs/published-template-runtime-switch.md`(published templateの仕組み)と `docs/schedule-automation-guarded-executor.md`(schedule実行の仕組み)を正とし、ここでは実際の操作手順だけをまとめます。

## 対象読者

Admin UIでレポート定義を追加・編集する担当者。IAP許可ユーザーであることが前提です。

## レポート定義とは

`report_id` をキーにFirestoreへ保存される設定のまとまりです。1つのレポート定義は以下を持ちます。

- 基本情報(name、owner、primary operator、customer、default month、GCS prefix、Drive folder)
- 公開テンプレート(`.xlsx`)の version 履歴と `current_version`
- query config / template mapping の version 履歴
- 月次 schedule 設定(enabled/day_of_month/time_of_day/timezone)
- delivery allowlist(許可ドメイン・許可メールのハッシュ)

**このガイドの対象外(非対応)の操作**:

- SQL本文の編集・差し替え(`query_config_id` は既定の1種類 `plus-monthly-default-v1` のみ選択可能)
- Excelテンプレートのセルマッピング編集(`mapping_version_id` も既定の1種類 `plus-monthly-table-mapping-v1` のみ)
- 許可されていない任意のGCS prefix / Driveフォルダの指定
- Cloud Scheduler jobの作成(schedule保存はメタデータ保存のみ。実際の自動実行には別途Cloud Scheduler attachmentが必要で、現状は個別のPhase10対応が必要です)

これらが必要な場合は、既存のレポート定義を流用するか、テルマエ・ロマエのような専用実装を検討してください。専用実装の進め方は `docs/new-bespoke-report-playbook.md` を参照してください。

## 前提条件

- `report_id` の命名規則: 英数字で始まり、英数字・`_`・`.`・`-` のみ、2〜128文字、`/` を含まない、`__xxx__` のような前後アンダースコア2つ囲みは不可
- GCS prefix / Drive folder は allowlist に含まれる値のみ登録可能(下記「Storage allowlistの確認」参照)
- テンプレート公開・query/mapping公開・archiveは、対象レポート定義が archive 済みでないことが前提

## 手順1: 基本情報を登録する

「レポート定義管理」タブの上部フォームに入力します。

| フィールド | 内容 | 必須 |
| --- | --- | --- |
| report_id | 一意のID。作成後は変更不可 | ○ |
| name | レポート名(表示用) | ○ |
| owner | 所管部署 | - |
| primary operator | 主担当者 | - |
| customer | 顧客 / recipient group | - |
| default month | 既定の対象月(`YYYY-MM`) | - |
| GCS prefix | 生成物の保存先prefix。allowlist必須 | - |
| Drive folder name | Drive保存先フォルダ名。allowlist必須 | - |

入力後「定義を追加」を押すと、`current_version=1`(status: draft)で作成されます。この時点ではテンプレート・query/mapping・scheduleは未設定です。

既存定義を修正する場合は、一覧から対象行をクリックしてフォームへ読み込み(`fillDefinitionForm`)、値を変更してから「定義を更新」を押します。

## 手順2: Excelテンプレートを公開する(任意)

未公開のままだと、生成時は既定テンプレート(`templates/template.xlsx` または `TEMPLATE_PATH`)にフォールバックします。専用テンプレートを使いたい場合のみ実施します。

1. 「Excel template preview」で `.xlsx` を選択し「template preview」を押す。保存はされず、シート名・行数・列数・テーブル数・サイズ・sha256だけを確認できる
2. 内容に問題なければ、同じファイルを再度選択して「template publish」を押す。GCSへアップロードされ、新しいversionが追加され `current_version` が更新される
3. 誤って公開した場合は「rollback version」に戻したいversion番号を入力して「version rollback」を押す。`current_version` だけが戻り、テンプレートファイル自体は削除されない

## 手順3: query / mapping を設定する(任意)

現状は BigQuery SQL・テーブルマッピングそのものは編集できず、既定の組み合わせ(`plus-monthly-default-v1` / `plus-monthly-table-mapping-v1`)を version として記録するだけです。

1. 「query / mapping dry-run」でBigQuery dry-runを実行し、SQLが構文的に有効かを確認する(SQL本文・結果行は返らない)
2. 問題なければ「query / mapping publish」を押して新しいversionを記録する

未対応の `query_config_id` / `mapping_version_id` を指定すると `400` で拒否されます。

## 手順4: 月次スケジュールを設定する(任意)

- 「schedule enabled」チェック、`monthly day`(1〜28)、`time of day`(`HH:MM`)、`timezone`(`Asia/Tokyo` のみ許可)を設定し「schedule save」を押す
- 「schedule dry-run」で、現在時刻を基準に対象レポートが実行対象かどうかを読み取り専用で確認できる

**重要**: ここでの保存はメタデータの記録のみです。Cloud Scheduler jobは自動作成されず、実際に毎月自動配信するには別途Cloud Scheduler jobの作成(専用service account、OIDC audience設定)が必要です。詳細は `docs/schedule-automation-guarded-executor.md` を参照してください。

## 手順5: delivery allowlist を設定する(任意)

- 「delivery allowed domains」「delivery allowed emails」をカンマ区切りで入力し「delivery allowlist save」を押す
- 保存されるのは正規化したドメインとメールのハッシュのみで、生メールアドレスは保存されません
- 配布作成時にリクエスト側で許可ドメイン/メールを省略すると、このpersisted allowlistが使われます(scheduled deliveryでは必ずこちらが使われ、request-time指定は無視されます)

## 手順6: このレポート定義で配布を作成する

「配布運用」タブの「配布作成」で「対象レポート定義」から作成したreport_idを選択します。

- 未選択(空欄)のままだと、既定テンプレート(report_id指定なし)の従来経路で生成されます
- report_idを選択すると、そのレポート定義の`current_version`の公開テンプレートを使って生成されます(未公開の場合は既定テンプレートにフォールバック)

## Storage allowlistの確認

「storage allowlist」ボタンを押すと、現在許可されているGCS prefixとDriveフォルダ名/IDの一覧が表示されます。ここに無い値をGCS prefix / Drive folder nameへ入力すると、定義の追加・更新時に `400` で拒否されます。allowlist自体は環境変数(`REPORT_ALLOWED_GCS_PREFIXES` / `REPORT_ALLOWED_DRIVE_FOLDERS`)で管理されており、Admin UIからは追加できません。

## Archiveする

「archive」ボタンで対象レポート定義を `archived` 状態にします。archive後は、テンプレート公開・query/mapping公開・schedule保存・delivery allowlist保存ができなくなります。配布URLの発行を止めたいだけの場合は、レポート定義のarchiveではなく個々の配布(delivery)の停止操作(`active=false`)を使ってください。

## API経由で操作する場合

Admin UIと同じ操作は `X-Admin-Key` を使ってAPI経由でも実行できます(script/自動化向け)。

| 操作 | Method | Path |
| --- | --- | --- |
| 一覧取得 | GET | `/report-definitions` |
| 詳細取得 | GET | `/report-definitions/<report_id>` |
| 新規作成 | POST | `/report-definitions` |
| 更新 | PATCH | `/report-definitions/<report_id>` |
| archive | POST | `/report-definitions/<report_id>/archive` |
| template preview | POST | `/report-definitions/<report_id>/template-preview` |
| template publish | POST | `/report-definitions/<report_id>/template-publish` |
| template/version rollback | POST | `/report-definitions/<report_id>/template-rollback`, `/report-definitions/<report_id>/version-rollback` |
| query/mapping dry-run | POST | `/report-definitions/<report_id>/query-mapping-preview` |
| query/mapping publish | POST | `/report-definitions/<report_id>/query-mapping-publish` |
| schedule保存 | POST | `/report-definitions/<report_id>/schedule` |
| schedule dry-run(全体) | GET | `/report-definitions/schedule-preview` |
| delivery allowlist保存 | POST | `/report-definitions/<report_id>/delivery-allowlist` |
| storage allowlist確認 | GET | `/report-definitions/storage-allowlist` |

新規作成の例:

```powershell
$base = 'https://report-generator-admin-635067190197.asia-northeast1.run.app'
$adminKey = & gcloud.cmd secrets versions access latest --secret=report-generator-admin-api-key --project=ice-sh

Invoke-RestMethod `
  -Uri "$base/report-definitions" `
  -Headers @{ 'X-Admin-Key' = $adminKey } `
  -Method Post `
  -ContentType 'application/json' `
  -Body (@{
    report_id = 'example-monthly'
    name = 'サンプル月次レポート'
    owner = 'システム管理室'
    primary_operator = '担当者名'
    customer_name = '顧客名'
    gcs_prefix = 'reports/plus/'
    version_note = 'initial definition'
  } | ConvertTo-Json)
```

## よくあるエラー

| エラー | 原因 | 対処 |
| --- | --- | --- |
| `report_id not found` | 存在しないreport_id、または命名規則違反 | report_idの綴り・形式を確認する |
| `report_id already exists` | 既に同じreport_idが存在する | 別のreport_idを使うか、既存定義を更新する |
| `name is required` | name未入力 | nameを入力する |
| `gcs_prefix is not allowed` | allowlist外のGCS prefix | 「storage allowlist」で許可値を確認する |
| `drive_folder_name is not allowed` | allowlist外のDriveフォルダ名 | 「storage allowlist」で許可値を確認する |
| `query_config_id is not allowed` / `mapping_version_id is not allowed` | allowlist外の値を指定 | 既定値(`plus-monthly-default-v1` / `plus-monthly-table-mapping-v1`)を使う |
| `report definition is archived` | archive済みの定義にtemplate/query-mapping publishを実行 | archive解除の運用がないため、新しいreport_idで作り直す |
| `schedule timezone is not allowed` | `Asia/Tokyo` 以外を指定 | `Asia/Tokyo` を指定する |

## 関連ドキュメント

- `docs/published-template-runtime-switch.md` — published templateが生成処理へ適用される仕組み
- `docs/schedule-automation-guarded-executor.md` — schedule保存後、実際に自動実行させる際の安全設計
- `docs/operations.md`(月次運用、複数レポート運用baseline) — 運用記録・確認手順
- `docs/thermae-romae-report.md` — SQL・セルマッピングが固有で、このレポート定義の仕組みに乗らない専用実装の例
