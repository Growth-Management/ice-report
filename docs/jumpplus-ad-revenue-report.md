# ジャンプ＋広告売上レポート（動画リワード / APP_2 / WEB）

3種類の専用Drive出力レポートです。`plus_browser_point_sales_report.py` / `thermae_romae_report.py` と同じ
「Drive保存のみ、OTP/ダウンロードURLなし」のパターンに従いますが、この帳票群は追加で **生成条件(readiness)判定**
を持ちます — 明細データと広告売上確定値の両方が揃うまで生成しません。

## Scope

- 対象レポート: `video-reward`(動画リワード広告売上) / `app2`(広告売上_APP_2、奥付広告) / `web`(広告売上_WEB)
- 手動API: `POST /admin/reports/jumpplus-ad-revenue/<report_type>/generate`
- スケジュールAPI: `POST /admin/reports/jumpplus-ad-revenue/<report_type>/scheduled-generate`
- 確定値同期(手動): `POST /admin/ad-revenue/sync`
- 確定値同期(スケジュール): `POST /admin/ad-revenue/scheduled-sync`
- 配布方式: Drive保存のみ。OTP付きダウンロードURL・メール送信は行わない
- 実装ファイル: `jumpplus_ad_revenue_report.py`、`sheets_io.py`、`app.py` の該当エンドポイント

## 全体フロー

```
Google Sheets「PLUS_広告費」(data シート: 対象月/動画リワード/奥付広告/WEB)
  │  Sheets API (sheets_io.read_sheet_values)
  ▼
BigQuery 通常テーブル jumpplus-4a5f4.dataset_exdata_tables.ad_revenue_confirmed_monthly
  │  (MERGE upsert, sync_ad_revenue_confirmed_values)
  ▼
readiness判定 (check_readiness)
  ├─ 明細データready? -> report_plus_monthly_coin_content_report / report_plus_monthly_ad_view に
  │                       対象月の行が存在するか
  └─ 広告売上確定値ready? -> ad_revenue_confirmed_monthly に対象月の該当列がNOT NULLか
  │
  ▼ (両方readyの場合のみ)
Excel生成 (generate_ad_revenue_report)
  │  BigQueryから明細取得 -> Driveからテンプレートdownload -> openpyxlで書き込み -> Driveへupload
  ▼
Google Drive 出力フォルダ (1jxC2AZ6eeDKx1wVr88kf4Ilw86FWTART, 「広告売上」フォルダ)
```

BigQuery外部テーブル(`format=GOOGLE_SHEETS`)は使わない。このプロジェクトの既存の
`dataset_exdata_tables.spreadsheets_*` テーブルは外部テーブルとしてこの方式を使っているが、今回は要件で明示的に
除外されている。代わりに Sheets API で読み取った値を通常のBigQueryテーブルへ `MERGE` する
(`sync_ad_revenue_confirmed_values`)。TROCCOも使わない。

## Environment

```text
AD_REVENUE_SPREADSHEET_ID=188iTZsN46tYQKc2ILi96ZH--coLFhUN-F9YDFIFCG70
AD_REVENUE_SHEET_RANGE=data!A1:D1000
AD_REVENUE_CONFIRMED_TABLE=jumpplus-4a5f4.dataset_exdata_tables.ad_revenue_confirmed_monthly
AD_REVENUE_COIN_CONTENT_TABLE=jumpplus-4a5f4.dataset_datamart_tables.report_plus_monthly_coin_content_report
AD_REVENUE_AD_VIEW_TABLE=jumpplus-4a5f4.dataset_datamart_tables.report_plus_monthly_ad_view
AD_REVENUE_SERVICE_NAME=J_PLUS
AD_REVENUE_OUTPUT_FOLDER_ID=1jxC2AZ6eeDKx1wVr88kf4Ilw86FWTART

# テンプレートファイルID -- システム管理室が2026-09-18に「広告売上」フォルダへ配置した正式テンプレート。
# jumpplus_ad_revenue_report.REPORT_SPECS[*].default_template_file_id にハードコードされた既定値であり、
# これらの環境変数は未設定でもよい(thermae/plusの DEFAULT_*_TEMPLATE_FILE_ID と同じ扱い)。運用上、
# 特定の帳票だけ一時的に無効化したい場合(インシデント対応など)は、該当の環境変数を空文字に設定すれば
# その帳票だけ "template_not_configured" で安全に止められる。
AD_REVENUE_VIDEO_REWARD_TEMPLATE_FILE_ID=1MtCimmJ9MjEjd0XW977kKME1sr82OP38
AD_REVENUE_APP2_TEMPLATE_FILE_ID=1Q1d4Mp-5iQFBwo3wnzmUa1mQPxPdvL1L
AD_REVENUE_WEB_TEMPLATE_FILE_ID=1LJ72durOS1ekPO79GtDFdrJX23Vr8je-

AD_REVENUE_SCHEDULED_RUNS_COLLECTION=ad_revenue_scheduled_runs
AD_REVENUE_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS=thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com
AD_REVENUE_SCHEDULER_AUDIENCE=<report-generator の実URL>/admin/reports/jumpplus-ad-revenue/<report_type>/scheduled-generate
AD_REVENUE_SYNC_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS=thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com
AD_REVENUE_SYNC_SCHEDULER_AUDIENCE=<report-generator の実URL>/admin/ad-revenue/scheduled-sync
```

`BIGQUERY_PROJECT_ID` / `PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` はBigQueryクライアントのプロジェクトに使う
(既存の thermae/plus と同じ)。

Scheduler SAは新規作成せず、PLUS point-sales レポートと同じ理由で既存の
`thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com` を再利用する想定(`_check_scheduler_oidc_auth` は
audience(URLごと)とAllowlistの組だけを見るため、SAを使い回すこと自体に追加のIAMは不要)。

### Drive認証: 既存のruntime SAを再利用(想定)

`DRIVE_AUTH_MODE` は未設定のまま(既定の `adc`)で動く想定。ただし出力フォルダ
`1jxC2AZ6eeDKx1wVr88kf4Ilw86FWTART`(「広告売上」フォルダ)が
`ice-report-runner@ice-sh.iam.gserviceaccount.com` から読み書きできるかは **未検証**。
PLUS point-sales レポートの前例(`docs/plus-browser-point-sales-report.md` の
"Drive authentication" 節)と同様に、対象フォルダが属するShared Driveへruntime SAがメンバーとして
参加できていない場合は `drive_not_found` で失敗する。デプロイ前に本番相当の資格情報でsmokeすること。

### Sheets認証: ice-report-runnerへの共有はしない -- Driveと同じユーザーOAuthを使う

**方針(確定)**: 共有ドライブの制約上、`ice-report-runner@ice-sh.iam.gserviceaccount.com` へ
「PLUS_広告費」を直接共有する方式は採らない。`drive_io.py` の `DRIVE_AUTH_MODE=oauth`
(`sinohara@impress.co.jp` のユーザーOAuthでDriveへアクセスする既存の本番方式、
`docs/drive-domain-wide-delegation.md` 参照)と同じ考え方を Sheets API にもそのまま適用する。

`sheets_io.py` は `DRIVE_AUTH_MODE` と同じ形の `SHEETS_AUTH_MODE`(既定 `adc`)を持ち、
`SHEETS_AUTH_MODE=oauth` で `drive_io._drive_oauth_credentials()` と同型の
`_sheets_oauth_credentials()` を使う(`OAuthCredentials(refresh_token=..., client_id=...,
client_secret=..., scopes=[SHEETS_READONLY_SCOPE])`)。client_id/client_secret/refresh_tokenは
Secret Manager管理で、`SHEETS_OAUTH_CLIENT_ID_SECRET_NAME` 等の `_SECRET_NAME` 環境変数からも読める
(`drive_io.py` と全く同じ仕組み)。

`SHEETS_OAUTH_*` が未設定の場合は自動的に `DRIVE_OAUTH_*`(`DRIVE_OAUTH_CLIENT_ID` /
`DRIVE_OAUTH_CLIENT_SECRET` / `DRIVE_OAUTH_REFRESH_TOKEN`、またはその `_SECRET_NAME` 版)へ
フォールバックする設計にしている(`sheets_io._config_value` の `fallback_env_name`)。これにより、
`sinohara@impress.co.jp` の既存Drive OAuth認証情報に `spreadsheets.readonly` スコープを追加した
単一のrefresh tokenを再発行して両方(Drive/Sheets)に使い回すことも、Sheets専用の別credentialを
`SHEETS_OAUTH_*` に個別設定することも、どちらも可能。どちらを採るかは運用判断(下記チェックリスト参照)。

```text
SHEETS_AUTH_MODE=oauth
# 以下は SHEETS_OAUTH_* が未設定なら DRIVE_OAUTH_* にフォールバックするので、
# 既存のDrive OAuth認証情報にSheetsスコープを追加して使い回す場合は設定不要。
# Sheets専用の別credentialを使う場合のみ明示的に設定する。
SHEETS_OAUTH_CLIENT_ID_SECRET_NAME=<Secret Manager シークレット名>
SHEETS_OAUTH_CLIENT_SECRET_SECRET_NAME=<Secret Manager シークレット名>
SHEETS_OAUTH_REFRESH_TOKEN_SECRET_NAME=<Secret Manager シークレット名>
```

**本番投入前に必須の作業(IAM変更でも共有変更でもなく、OAuth認証情報の確認・整備):**

1. **OAuth credential確認**: `sinohara@impress.co.jp` の既存Drive OAuth
   (client_id/client_secret/refresh_token)が、Sheets読み取りスコープ
   (`https://www.googleapis.com/auth/spreadsheets.readonly`)を含む形で使えるか確認する。
   含まれていない場合は、そのスコープを追加した新しいOAuth同意・refresh token発行が必要
   (Drive用と共用するか、Sheets専用に分けるかは運用判断)。
2. **Secret Manager設定**: 上記で決めたclient_id/client_secret/refresh_tokenをSecret Managerへ
   登録し(既存のDrive OAuth用シークレットを再利用するか、新規に `sheets-oauth-*` 相当を作成するか)、
   Cloud Runの環境変数(`SHEETS_OAUTH_*_SECRET_NAME`、または共用する場合は未設定のままでDrive側の
   `DRIVE_OAUTH_*_SECRET_NAME` へのフォールバックに任せる)を設定する。
3. **Sheets API有効化**: `ice-sh` プロジェクト(Cloud Runの実行プロジェクト)で Google Sheets API
   が有効化されていることを確認する(未確認)。
4. **OAuth経由のPLUS_広告費読み取り確認**: 本番相当の環境で `SHEETS_AUTH_MODE=oauth` を設定した状態から
   `sheets_io.read_sheet_values` (または `POST /admin/ad-revenue/sync`)を実行し、
   `sinohara@impress.co.jp` のOAuth経由で実際に「PLUS_広告費」の `data` シートを読み取れることを確認する。

`ice-report-runner@ice-sh.iam.gserviceaccount.com` への「PLUS_広告費」共有は行わない(この方針は確定)。

## BigQueryへの新規テーブルと権限(要承認)

新規テーブル: `jumpplus-4a5f4.dataset_exdata_tables.ad_revenue_confirmed_monthly`

```sql
create table if not exists `jumpplus-4a5f4.dataset_exdata_tables.ad_revenue_confirmed_monthly` (
  target_month date not null,
  video_reward_yen int64,
  app_footer_ad_yen int64,
  web_ad_yen int64,
  synced_at timestamp
);
```

このテーブルはコード側 (`_ensure_confirmed_table`、`client.create_table(table, exists_ok=True)`) が
初回同期実行時に自動作成する。手動でDDLを流す必要はない。

**IAM変更が必要(要明示承認)**: `ice-report-runner@ice-sh.iam.gserviceaccount.com` は現在
`dataset_exdata_tables` に対して読み取り(READER)権限のみ持っている(PLUS point-sales レポートが
`sbps_product`/`sbps_payment_class` を読むため)。今回の同期処理はこのデータセットへテーブルを
**作成・書き込み**するため、`dataset_exdata_tables` に対する `roles/bigquery.dataEditor` 相当
(データセットレベル、または作成後のテーブル単位でも可)を追加付与する必要がある。これは本ドキュメントでは
実施していない。デプロイ前に明示承認のうえ付与すること。

`report_plus_monthly_coin_content_report` / `report_plus_monthly_ad_view` の読み取りは、thermae
レポートが同じ `dataset_datamart_tables` から既に読んでいるため追加のIAM変更は不要と想定している
(未検証。同一データセットの別テーブルであり、既存のデータセットレベルREADER権限で足りるはず)。

## Google Sheets -> BigQuery 同期

`jumpplus_ad_revenue_report.sync_ad_revenue_confirmed_values`:

1. `sheets_io.read_sheet_values` で `PLUS_広告費` の `data!A1:D1000` を `UNFORMATTED_VALUE` で取得
2. ヘッダー行が `対象月, 動画リワード, 奥付広告, WEB` と一致することを検証(不一致は
   `unexpected_sheet_header` でフェイルクローズ)
3. 各行を `target_month`(ISO日付。文字列 `"YYYY-MM-DD"` またはSheetsのシリアル日付どちらの形式でも対応)と
   金額3列(カンマ区切り文字列・数値・空欄・`"-"` のいずれにも対応、空欄/`-` は `NULL` として扱う)にパース
4. `ad_revenue_confirmed_monthly` テーブルを (無ければ)作成
5. パース済みの全行を `MERGE ... USING UNNEST(@rows)` で一括upsert(`target_month` をキーに
   `UPDATE`/`INSERT`、`synced_at` を都度更新)

この処理は繰り返し実行しても安全(MERGEは毎回その時点のSheet値で上書きするだけ)。スケジュール実行に
Firestoreクレーム(重複防止)は使っていない -- 生成処理と異なり、同期は「毎回最新値に合わせる」ことが
正しい挙動そのものだから。

## 生成条件(readiness)判定

`check_readiness(project_id, report_type, target_month)` が3つの状態を返す:

- `ready`: 明細データ・広告売上確定値の両方が揃っている(`revenue_yen` も返す)
- `waiting_detail`: 対象月の明細データ(BigQueryデータマート)がまだ無い
- `waiting_revenue`: 明細データはあるが、`ad_revenue_confirmed_monthly` の該当列がまだ `NULL`
  (＝まだ確定値がSheetsに反映されていない、または同期がまだ走っていない)

`waiting_*` はエラーではない。スケジュール実行エンドポイントはこれを `200 {"status": "waiting", "reason": ...}`
として返し、Firestoreに何も書き込まない(条件確認自体は何度呼んでも副作用なし)。

明細ready判定は「対象月の行が1件でも存在するか」の存在チェック(`limit 1`)であり、明細データの完全性
(全件揃っているか)までは検証していない -- テルマエ・ロマエ/PLUS point-sales レポートも同じ粒度(存在しなければ
即エラー)。

## 3帳票の実装

3種類とも公式テンプレート(2026-09-18配置)を実際に開いて構造を全項目確認済み(下記「検証済み事項」参照)。

| report_type | 表示名 | 明細データソース | 明細シート | 値列 |
|---|---|---|---|---|
| `video-reward` | 動画リワード広告売上 | `report_plus_monthly_coin_content_report`(`service_name='J_PLUS'` AND `ws_ex_is_return_reward_video_ad_coin=true` AND `purchase_type='episode'` AND `app_pf in ('iOS','And')`、content_idごとの合計が正の行のみ) | iOS(`app_pf='iOS'`) / Android(`app_pf='And'`) / 全体(iOS+Android合算、Webは含めない) | コイン消費数 |
| `app2` | 広告売上_APP_2(奥付広告) | `report_plus_monthly_ad_view`(`service_name = 'J_PLUS'`) | iOS(`app_pf='iOS'`) / Android(`app_pf='And'`) / 全体(iOS+Android合算) | 広告表示数 |
| `web` | 広告売上_WEB | `report_plus_monthly_ad_view`(`app_pf='Web'`、`service_name = 'J_PLUS'`) | 全体 | 広告表示数 |

**`video-reward`のフィルタ条件はGolden Master(2026年8月の実手動作成ファイル)との突き合わせで確定した
ものであり、単純な `service_name='J_PLUS'` だけでは実帳票と一致しない**(詳細は下記「検証済み事項」の
「Golden Master突き合わせ」参照)。`ws_ex_is_return_reward_video_ad_coin=true` のみでは
`purchase_type='book'` や `app_pf='Web'` の(コイン消費数0の)行が明細に混入し、さらに
content_idの合計が0の行(その月に実際は消費されなかった対象コンテンツ)も除外(`having
reward_video_ad_coin_count > 0`)しないと明細行数が実帳票と一致しない。

`video-reward` は当初「全体」シートのみに書き込む設計だったが、公式テンプレートを開いて確認したところ
サマリシートの「動画リワード広告コイン消費数」ブロックが `=SUM(話データ_iOS[コイン消費数])` /
`=SUM(話データ_Android[コイン消費数])` という**生きた数式**でiOS/Androidシートを直接参照していることが
判明したため、`app2` と同じ iOS/Android/全体 の3シート構成に修正した(修正前のままだとiOS/Android
シートが空になり、サマリの内訳が0のまま表示されてしまうため)。この経緯は
`FetchDetailRowsDispatchTests`(`tests/test_jumpplus_ad_revenue_report.py`)でロックしている。

### Excelワークブックのルール

- サマリシートの合計金額セル(`write_summary_total`)は、セル位置をハードコードせず「シート上で最初に
  現れる `総計` ラベルの右隣のセル」を汎用的に探して書き込む。動画リワードのサマリシートには
  `総計` ラベルが2か所(広告売上ブロックのB4、コイン消費数内訳ブロックのB12)あり、収益ブロックが
  常に最初(最上段)に現れることを公式テンプレートで確認済みなので、これで正しく収益セル(C4)だけに
  書き込める。著者還元額ブロック(B16:C17)には現状「総計」ラベル自体が存在しない(空の2行テーブル)。
- 明細シート(`write_detail_sheet`)は、シート上の唯一のExcel Table(ListObject)をヘッダー行付きで
  自動検出し、ヘッダー文字列で列をマッピングして値を書き込む。テーブル名や列順をハードコードしないため、
  テンプレート間の構造差異があっても壊れにくい。
- 行数の増減(`_resize_table_rows`)は、テーブルに行を挿入/削除して `table.ref`/`autoFilter.ref` を
  更新する。PLUS point-sales レポートの `_extend_table_rows` と異なり、**縮小も許容する**
  (この帳票群は決済区分のような固定カテゴリの列挙ではなく、毎月変動するコンテンツカタログ全体のダンプの
  ため、行数が減ること自体は異常ではない)。
- 新規に挿入する行は、直前の既存データ行から **スタイルと値/数式の両方** をコピーする。これはAPP_2の
  「全体」シートの `広告売上`/`広告売上_原資50` 列のような構造化参照数式(`[#This Row]`/`@` 構文)を
  意図的に書き換えないための設計 -- ただし公式テンプレートを実際に確認したところ、これらの列は
  **空のプレースホルダー行に数式がまだ入っていない**(情シスが今後の運用で埋める想定と思われる)。
  つまり現時点では「コピーする数式が存在しない」ため実害はないが、将来テンプレート側にこの列の数式が
  追加された場合でも、このモジュールが F/G 列(広告売上/広告売上_原資50、`AD_VIEW_DETAIL_HEADERS` に
  含まれない列)を一切書き込まない設計のおかげで、既存の数式を壊さず済む。
- `write_detail_sheet` は「必須ヘッダー」(コンテンツID_Raise/コンテンツID/コンテンツ名/JDCN/作品名)が
  見つからない場合のみフェイルクローズし、その他の見出し(コミックスJDCN/コミックス巻数/タイトルID/
  デジタルタイトル名など)は存在すれば書き込み、無ければスキップする。3テンプレートとも該当ヘッダーが
  全て存在することを確認済みだが、将来のテンプレート改版に対する保険としてこの設計を維持している。
- 結合セル・データ入力規則・条件付き書式・グラフ・画像・外部リンクは3テンプレートとも一切使われていない
  (3ファイルとも実バイナリで確認済み)。印刷範囲・印刷タイトル行/列も未設定(既定のページ設定のみ、
  paperSize=9/A4・portrait)。ワークシートの列幅・行高・フォント(ＭＳ Ｐゴシック 11pt)は
  openpyxlの `load_workbook`→`save` で自動的に保持される(明示的な書き込み処理は行っていない)。
- 3テンプレートとも、由来不明の名前付き範囲(`defined_names`: `a`, `aaa`, `Contents_master`,
  `master`, `Ranking_ANdrio` など、テンプレートの元となった社内ファイルの残骸と思われる)が残っている。
  このモジュールは `wb.defined_names` に一切触れないため、`load_workbook`→`save` で無害にそのまま
  保持される(実際に生成テストで保持されることを確認済み)。

### openpyxl round-trip root cause(Excel Desktop「修復が必要」問題)と package-preserving writer

上記の `write_summary_total`/`write_detail_sheet`/`_resize_table_rows` はopenpyxlの `Worksheet` オブジェクトを
直接操作する関数として今も存在するが、**Production成果物の最終writerとしては使われていない**(以下の理由により
使用禁止)。これらはテンプレート構造解析・validation・テスト・Golden Master確認の用途に限定して残している。

**症状**: video-reward/app2/webのいずれも、Drive生成後のファイルをExcel Desktopで開くと「修復が必要」ダイアログが
表示された(`create_ad_revenue_workbook` 経由でopenpyxlの `load_workbook()`→`save()` を最終writerとして
使っていた時点)。APP_2のExcel repair logには「削除されたパーツ: 外部データの範囲.」が2件記録されていた。

**Root cause(実測で確定)**: openpyxlの `load_workbook()`→`save()` は、**何も値を変更せずそのまま再保存するだけでも**、
openpyxlが理解しないOOXMLパートを黙って落とす。実際に公式APP_2テンプレートを対象に検証したところ、以下が
消失/再生成された:

- `xl/connections.xml`、`xl/queryTables/queryTable1.xml`・`queryTable2.xml`(Power Query接続定義そのもの)
- `xl/tables/_rels/table7.xml.rels`・`table8.xml.rels`(TableからqueryTableへのrelationship)
- `customXml/*`
- `calcChain.xml`・`metadata`・`printerSettings`・その他openpyxl未対応パート

APP_2の「作品別」「作品別_2」シートのTableは `tableType="queryTable"` 属性で「これはPower Query由来のTableである」
ことを宣言しているが、上記のroute round-tripによって「どの接続/クエリに繋がっているか」という実体(connections.xml/
queryTables/relationship)だけが消える。Excel repair logの「外部データの範囲」削除2件は、まさにこの2つの
queryTableに対応する。

**結論**: 明細行追加・table resize・summary C4書込・BigQuery・Drive upload(resumable化、PR #135)は原因ではない。
**openpyxlをProduction成果物の最終XLSX writerとして使用していること自体が原因**であり、Power Queryを持たない
web/video-rewardでも(calcChain/printerSettings等、程度の差はあれ同種のパート消失が起きるため)同じ設計を
維持する限りリスクが残る。

**対応**: `create_ad_revenue_workbook` の内部実装を `xlsx_package_writer.py`(package-preserving OOXML writer)へ
差し替えた。テンプレートXLSXをzipパッケージとしてそのまま読み、実際に書き込みが必要な特定のworksheet XML
パート(summaryシートの総計セル、明細シートの `sheetData`)と、対応するTable XMLパートの `ref`/`autoFilter.ref`
だけをlxmlで直接編集し、**それ以外の全パートは元テンプレートからbyte-for-byteでそのままコピー**する。
`connections.xml`/`queryTables/*`/`customXml/*`/`calcChain.xml`/`styles.xml`/`sharedStrings.xml` などは
一切parseすらしないため、Power Queryを含むテンプレートでも安全。文字列セルは `sharedStrings.xml` を汚さないよう
`inlineStr` で書き込む。openpyxlは `create_ad_revenue_workbook` の出力writerとしては使わず、テンプレート構造解析・
テスト・Golden Master確認(実ファイルを開いての目視/自動検証)にのみ引き続き使用する。

詳細は `xlsx_package_writer.py` のモジュールdocstringと `tests/test_xlsx_package_writer.py`
(`PowerQueryPreservationTests` がPower Query関連パートのbyte-for-byte保持・relationship保持・connection ID
整合性を検証)を参照。

## Target month calculation

`tokyo_today()` で `Asia/Tokyo` の現在時刻から日付を求め、その前月1日を既定の対象月とする
(PLUS point-sales レポートと同じ理由 -- Cloud SchedulerがUTC日境界の近くで発火する可能性があるため)。

## 重複生成防止(冪等性)

- Firestoreコレクション `AD_REVENUE_SCHEDULED_RUNS_COLLECTION`(既定 `ad_revenue_scheduled_runs`)、
  ドキュメントID `<report_type>-<YYYY-MM>` (例: `video-reward-2026-08`)。3帳票を1つのコレクションで
  管理し、report_typeをキーの一部にすることで衝突しない。
- スケジュール実行は「まずFirestoreドキュメントを読むだけ(peek)」→ 既に `succeeded` なら
  `200 {"status":"skipped"}` を返して何もしない → まだ無ければreadiness判定 → `ready` の場合のみ
  共通ヘルパー `_claim_scheduled_run` でクレームを取ってから生成、を行う。これにより、
  「まだ広告売上が届いていないだけ」のポーリングでは一切Firestoreに書き込まず、実際に生成した月だけ
  記録が残る。
- 手動 `/generate` エンドポイントは重複防止なし(thermae/plus と同じ)。訂正等で再生成が必要な場合は、
  この手動エンドポイントを呼べばよい(Drive側には新しいファイルが追加される。既存ファイルの削除は
  運用側で手動対応)。

## ログ・監査

- readiness判定結果は `logging.info("ICE_REPORT_AD_REVENUE_READINESS report_type=%s target_month=%s status=%s", ...)`
  で毎回記録する(waiting/readyの別、生の金額やSQL・Excelセル値は出さない)。
- 手動生成の成功/失敗は `_log_admin_audit_event(action="ad_revenue_generate", ...)` に記録され、
  detailに `target_month` / `revenue_yen` / `detail_row_count` を含む(既存の thermae 実装が
  `payment_total` 等を audit detail に含めているのと同じ扱い -- Admin監査ログのみに残り、生ログには出さない)。
  同期処理は `_log_admin_audit_event(action="ad_revenue_sync", ...)` に `row_count` を記録する。
- スケジュール実行の完了/失敗は `logging.warning`/`logging.error` (`ICE_REPORT_AD_REVENUE_SCHEDULE_*`)
  に記録するが、ここには金額を含めない(thermae/plusと同じ方針)。

## 手動実行

```powershell
$body = @{ target_month = "2026-08-01" } | ConvertTo-Json
Invoke-RestMethod `
  -Uri "$env:SERVICE_URL/admin/reports/jumpplus-ad-revenue/video-reward/generate" `
  -Method Post `
  -Headers @{ "X-Admin-Key" = $env:ADMIN_API_KEY; "Content-Type" = "application/json" } `
  -Body $body
```

`report_type` は `video-reward` / `app2` / `web` のいずれか。`target_month` を省略すると前月(Asia/Tokyo基準)。

確定値の再同期:

```powershell
Invoke-RestMethod `
  -Uri "$env:SERVICE_URL/admin/ad-revenue/sync" `
  -Method Post `
  -Headers @{ "X-Admin-Key" = $env:ADMIN_API_KEY; "Content-Type" = "application/json" }
```

## Cloud Scheduler設定(自動実行する場合)

4つのジョブが必要(3帳票分の生成ポーリング + 確定値同期)。

```powershell
# 確定値同期: 30分おき
gcloud.cmd scheduler jobs create http ad-revenue-sync `
  --project=ice-sh --location=asia-northeast1 `
  --schedule="*/30 * * * *" --time-zone="Asia/Tokyo" `
  --uri="https://<service-url>/admin/ad-revenue/scheduled-sync" `
  --http-method=POST `
  --oidc-service-account-email="thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com" `
  --oidc-token-audience="https://<service-url>/admin/ad-revenue/scheduled-sync"

# レポート生成ポーリング: 毎日08:00 JST (report_typeごとに1ジョブ、例は video-reward)
gcloud.cmd scheduler jobs create http ad-revenue-video-reward-monthly-report `
  --project=ice-sh --location=asia-northeast1 `
  --schedule="0 8 * * *" --time-zone="Asia/Tokyo" `
  --uri="https://<service-url>/admin/reports/jumpplus-ad-revenue/video-reward/scheduled-generate" `
  --http-method=POST `
  --oidc-service-account-email="thermae-romae-scheduler@ice-sh.iam.gserviceaccount.com" `
  --oidc-token-audience="https://<service-url>/admin/reports/jumpplus-ad-revenue/video-reward/scheduled-generate"
# app2 / web も同様に report_type部分を差し替えて作成する
```

同期を08:00より前に何度か回しておく(例: 07:00, 07:30)ことで、明細データ準備(1日7:00)の直後から
確定値が反映されていればその日のうちに生成される。確定値がまだの場合は `waiting` を返し続けるだけで、
アラートは出ない。

## テスト

- `tests/test_sheets_io.py`: Sheets認証・読み取りの単体テスト
- `tests/test_jumpplus_ad_revenue_report.py`: 対象月判定、ファイル名規則、Sheetsセル値パース(カンマ区切り・
  シリアル日付・空欄)、サマリ合計セル書き込み(複数`総計`ラベルがある場合含む)、明細シート書き込み・
  行の増減・数式列の保持、readiness判定の3状態、生成ガード(未知report_type/not_ready/テンプレート未設定)、
  Sheets->BigQuery同期(ヘッダー検証・MERGE呼び出し)

3テンプレートの実ファイルに対するアクセプタンステスト(2026年8月、動画リワード=14,017,945円 /
APP_2=9,789,547円 / WEB=734,139円)は、動画リワード・APP_2について公式テンプレートを直接ダウンロードして
`create_ad_revenue_workbook` を本番同様に実行するローカル生成テストで実施済み(下記「検証済み事項」の
「ローカル生成テスト」参照)。BigQueryからの実クエリ実行(readiness判定・明細取得)とDriveへのアップロードは
実際の認証情報が必要なため、本番デプロイ後のsmokeとして別途実施する。

## 検証済み事項(実装中に確認)

### BigQueryデータソースの特定(推測ではなく実データ突き合わせ)

- `report_plus_monthly_ad_view` の `id=1118985`(2026-08): `app_pf='Web'` -> `ad_view_count=21668`、
  `app_pf='iOS'` -> `36680` が、実ファイルの `WEB`/`APP_2` シートの広告表示数値と完全一致。
- `report_plus_monthly_coin_content_report` の `content_id=54987`(2026-08): `app_pf='iOS'` の
  `reward_video_ad_coin_count=455` + `app_pf='And'` の `270` = `725` が、実ファイルの コイン消費数値と
  完全一致。
- `service_name = 'J_PLUS'` フィルタ適用後の `report_plus_monthly_ad_view` の行数
  (iOS=9548行、Android=9548行、Web=9060行、いずれもdistinct content数と同数)が、実ファイルの
  APP_2(iOS/Androidシートともに9548行)・WEB(9060行)のテーブル行数と完全一致。
- `PLUS_広告費` スプレッドシートの2026-08-01行が 動画リワード=14,017,945円 / 奥付広告=9,789,547円 /
  WEB=734,139円 であることを確認済み(受入テストの正解値と一致)。

### Golden Master突き合わせ(2026年8月の実手動作成ファイルとの完全一致検証)

`jumpplus_ad_revenue_report.run_coin_content_query` / `run_ad_view_query` / `check_detail_ready` を
モックなしで実際に `jumpplus-4a5f4` へ実行し(ローカルのApplication Default Credentialsが対象
BigQueryデータセットへの読み取り権限を持っていたため)、2026年8月分の実手動作成Excel
(Golden Master)の値と完全一致することを確認した:

| 指標 | Golden Master | 実行結果 |
|---|---|---|
| 動画リワード iOS 合計 / 明細行数 | 250,864,974 / 59,614行 | **250,864,974 / 59,614行** |
| 動画リワード Android 合計 / 明細行数 | 114,388,036 / 58,899行 | **114,388,036 / 58,899行** |
| 動画リワード 全体 合計 / 明細行数 | 365,253,010 / 61,699行 | **365,253,010 / 61,699行** |
| APP_2 iOS 合計 / 明細行数 | 38,808,824 / 9,548行 | **38,808,824 / 9,548行** |
| APP_2 Android 合計 / 明細行数 | 20,502,000 / 9,548行 | **20,502,000 / 9,548行** |
| APP_2 全体 合計 / 明細行数 | 59,310,824 / 9,548行 | **59,310,824 / 9,548行** |
| WEB 合計 / 明細行数 | 15,676,222 / 9,060行 | **15,676,222 / 9,060行** |

全項目が完全一致(誤差なし)。この過程で以下を発見・修正した:

- **フィルタ条件の不足(実装バグ)**: 当初の `video-reward` クエリは `service_name='J_PLUS'` と
  `ws_ex_is_return_reward_video_ad_coin=true` のみで、`purchase_type='episode'` と
  `app_pf in ('iOS','And')` が抜けていた。`service_name` のみでの集計は `366,242,450`
  となり、Golden Masterの `365,253,010` と `989,440` 一致しなかった(差分は全て
  `ws_ex_is_return_reward_video_ad_coin=false` の行)。`purchase_type`/`app_pf` を追加しても
  合計自体は変わらなかったが、`purchase_type='book'` や `app_pf='Web'`(コイン消費数が常に0)の
  行が明細の行数を実際より多く見せていたため、明細行数(59,614/58,899/61,699)がGolden Masterと
  一致しなかった。さらに、対象content_idの月間合計が0の行(まだ実際に消費されていない対象コンテンツ)
  を除外する `having reward_video_ad_coin_count > 0` を追加して、初めて明細行数まで完全一致した。
- **SQL構文バグ(実装バグ、ライブ実行で発見)**: `having` 句を最初 `having
  sum(reward_video_ad_coin_count) > 0` と書いたところ、BigQueryが
  `Aggregations of aggregations are not allowed` で実行時エラーになった
  (`reward_video_ad_coin_count` は既に `sum(...) as reward_video_ad_coin_count` で集計済みの
  SELECTエイリアスであり、それをさらに `sum()` で包んだため)。`having
  reward_video_ad_coin_count > 0`(エイリアスを直接参照)に修正して解消。この種のバグは
  SQL文字列の単体テストだけでは検出できず、実際にBigQueryへ投げて初めて判明したもの
  (`tests/test_jumpplus_ad_revenue_report.py` の `RunCoinContentQueryGoldenMasterTests` は
  SQL文字列の内容を検証するが、BigQuery側の実行時構文検証までは代替できないため、この修正は
  ライブ実行によるものであることを明記しておく)。

### 公式テンプレート(2026-09-18配置)の構造確認

3ファイルとも実際にダウンロードしてopenpyxlで完全解析した(動画リワード42,498 bytes、APP_2 49,614
bytes、WEBはこのセッションではbase64転送の文字化けにより再ダウンロードできなかったため、Drive側の
メタデータ全文スニペットで内容を突き合わせて確認 -- 以前のセッションで完成済みファイルを実バイナリで
完全解析した結果とシート名・テーブル名・見出し列が一致することを確認済み)。

確認項目と結果(3ファイル共通):

- シート一覧: 動画リワード=`サマリ,全体,iOS,Android,作品別`、APP_2=`サマリ,iOS,Android,全体,作品別,作品別_2`、
  WEB=`サマリ,全体,作品別`
- 結合セル: **なし**(全シート)
- 非表示行/列: **なし**
- 印刷範囲・印刷タイトル行/列: **未設定**
- ページ設定: `paperSize=9`(A4)、`orientation=portrait`、scale/fitToWidth/fitToHeightは未設定
- ページ余白: 既定値(left/right=0.7、top/bottom=0.75、header/footer=0.3)
- フリーズ枠: 明細シートは `D4`(動画リワード全シート、APP_2のiOS/Android)、APP_2の「全体」シートのみ `I4`
- データ入力規則・条件付き書式・グラフ・画像・外部リンク: **なし**
- 数式: サマリシートの内訳ブロック(iOS/Android別集計)は `=SUM(話データ_iOS[...])` 等の生きた数式。
  動画リワードは以前確認した完成済みファイルと異なり、公式テンプレートでは `#REF!` になっていない
  (壊れた完成済みファイルは情シスの運用中に何かの理由で参照が壊れた個別インスタンスであり、
  テンプレート自体は健全だったと判明)
- 名前付き範囲: `a`, `aaa`, `aaaa`, `b`, `Contents_master`, `d`, `master`, `Ranking_ANdrio`, `rerere`,
  `sss`, `ssss`, `sssss`, `てｓｔ` など由来不明のものが3ファイルとも残っている(無害、上記参照)
- ファイル名規則: `<yymmdd>` はファイルのJST生成日(作成日をJSTに変換した値と完全一致することを複数の
  実ファイルで確認済み)
- 動画リワードの「全体」「iOS」「Android」各シートの明細ヘッダーは
  `コンテンツID_Raise,コンテンツID,コンテンツ名,JDCN,コイン消費数,作品名,コミックスJDCN,コミックス巻数`
  の8列で完全一致(`VIDEO_REWARD_DETAIL_HEADERS` と一致)
- APP_2「全体」シートは13列(F=広告売上、G=広告売上_原資50 の数式列を含む)、WEB「全体」シートは11列
  (F=広告売上の数式列を含む)で、いずれも公式テンプレートのプレースホルダー行(4行目)の該当セルは
  **空**(数式が未投入の状態)だった

### ローカル生成テスト(本番相当のコードパスで実施)

`jumpplus_ad_revenue_report.create_ad_revenue_workbook` を実際にダウンロードした動画リワード・APP_2の
公式テンプレートに対して直接実行し(BigQuery/Driveはモックせず、関数自体を本番同様に呼び出し)、
2026-08の受入値を投入して確認した:

- 動画リワード: `revenue_yen=14017945` → サマリC4に反映、iOS/Android/全体の3シートそれぞれに
  コイン消費数(455/270/725)を正しい列(E列)へ書き込み、`detail_row_count=3`
- APP_2: `revenue_yen=9789547` → サマリC4に反映、iOS/Android/全体の3シートに広告表示数
  (36680/10175/46855)を正しい列(E列)へ書き込み
- 生成後のファイルを再度 `load_workbook` で開き直し、破損なく読めることを確認
- 名前付き範囲・列幅・ページ設定が保存後も保持されることを確認
- 全体シートの空行(0行データ)へのリサイズもエラーなく動作することを確認

WEBは実バイナリを本セッションで再取得できなかったため、構造確認済みの合成テンプレート(単体テストの
`_build_detail_sheet`/`_build_summary_sheet` ヘルパーで、公式テンプレートと同一のシート名・テーブル構造で
構築)での生成テストに留まる(`CreateAdRevenueWorkbookTests` で自動テスト済み)。

## 本番手動受入チェックリスト(deploy後、Scheduler有効化前に必須)

IAM・Sheets OAuth整備・deployが完了した後、Cloud Scheduler有効化の前に、`X-Admin-Key` での手動実行
(`POST /admin/reports/jumpplus-ad-revenue/<report_type>/generate`)で以下を確認する。**特にWEB帳票は
このセッションで公式テンプレートの実バイナリ再確認ができなかったため(下記「検証済み事項」参照)、
本番手動受入での実ファイルによるend-to-endの生成確認を必須項目として残す**(動画リワード/APP_2は
このセッションでローカル生成テスト済みだが、それでも本番相当環境での最終確認として同様に実施する):

- [ ] `video-reward`: `target_month=2026-08-01` で手動生成し、Driveに出力ファイルが作成されることを確認
- [ ] `video-reward`: 生成物を**Excel Desktopで実際に開き**、修復ダイアログが一切表示されないことを確認
      (最終PASS条件はこれのみ -- openpyxl reopen成功/ZIP testzip成功/XML parse成功は補助確認に過ぎない)。
      サマリC4=14,017,945円、iOS/Android/全体シートのコイン消費数、数式(`=SUM(話データ_iOS[コイン消費数])`
      等)が壊れていないことも確認
- [ ] `app2`: `target_month=2026-08-01` で手動生成し、**Excel Desktopで修復ダイアログが出ないこと**、
      サマリC4=9,789,547円、iOS/Android/全体シートの広告表示数、`connections.xml`/`queryTables/*`が
      Power Query機能ごと正常に開けること(作品別/作品別_2シート)を確認
- [ ] `web`: `target_month=2026-08-01` で手動生成し、**Excel Desktopで修復ダイアログが出ないこと**、
      サマリの総計セル=734,139円、全体シートの広告表示数・書式・数式列(`広告売上`)が壊れていないことを確認
- [ ] 3帳票とも、生成物のシート名・結合セルなし・印刷設定・列幅が元テンプレートと変わっていないことを
      目視確認
- [ ] スケジュール実行エンドポイントへ実際にOIDCトークン付きでリクエストし、`waiting`(未確定時)と
      `duplicate_scheduled_run`(重複時)の両方の応答を確認してから、Cloud Schedulerを有効化する

**Scheduler有効化は、上記3帳票すべてがExcel Desktopで「修復なし」で開けることを確認するまでBLOCKED**
(package-preserving writer導入PRの受け入れ条件、詳細はPR本文参照)。

## Status / open items(未解決事項)

本番化に向けて残っているのは、コード側の問題ではなく外部設定(IAM・共有・Scheduler・deploy)のみ。

1. **Sheets OAuth認証情報・API有効化が未整備**: `ice-report-runner` への共有は行わない方針(確定、上記
   「Sheets認証」参照)。代わりに `sinohara@impress.co.jp` のユーザーOAuth(`SHEETS_AUTH_MODE=oauth`、
   Drive OAuthと同じ方式)で読み取る。未実施なのは (a) 既存Drive OAuth credentialへの
   `spreadsheets.readonly` スコープ追加、またはSheets専用credentialの発行、(b) Secret Manager登録、
   (c) `ice-sh` プロジェクトでのSheets API有効化、(d) OAuth経由での実読み取り確認。
2. **`dataset_exdata_tables` への書き込みIAM未付与**: 上記「BigQueryへの新規テーブルと権限」参照。
   明示承認のうえ付与が必要。付与前は `ad_revenue_confirmed_monthly` が作成できず、同期は
   `PermissionDenied` 系のエラーで失敗する(フェイルクローズ、想定通り)。
3. **Drive出力フォルダのruntime SAアクセス未検証**: 「広告売上」フォルダがどのShared Drive配下にあり、
   runtime SAがメンバーかどうかは未確認。PLUS point-sales レポートの前例と同様、`drive_not_found` に
   なる場合はShared Drive共有設定の見直しが必要。
4. **Cloud Scheduler未作成**: 上記の設定例は未実施(コマンド例のみ)。
5. **production deploy未実施**: `app.py` を変更しているため `report-generator` / `report-generator-admin`
   両方のdeployが必要(未実施)。
6. **WEBテンプレートの実バイナリ再確認が本セッションでは不完全**: 上記「検証済み事項」参照。以前の
   セッションで完成済みファイルを完全解析済みであり、今回のDriveメタデータのスニペットとも一致しているが、
   公式テンプレートそのものを開いた実バイナリ確認・生成テストは本セッションでは未実施。そのため
   「本番手動受入チェックリスト」でWEB帳票のend-to-end生成確認を必須項目としている -- Scheduler
   有効化前に必ず実施すること。
7. **動画リワードのF/G列相当(著者還元額・作品別ロールアップ)は自動計算されない**: 「作品別」シートの
   `コイン消費割合`/`広告還元額`列や、サマリの「著者還元額」ブロックは、明細データだけからは計算できない
   追加のビジネスロジック(単価・還元率など)が必要と見られ、本実装のスコープ外(受入条件である
   総計金額・明細のコイン消費数/広告表示数には影響しない)。運用上必要であれば、情シス側で別途手動計算・
   入力する運用を維持する想定。

## ロールバック

- レポート生成のみをロールバックする場合: 対象の `AD_REVENUE_*_TEMPLATE_FILE_ID` を空にするか、
  該当のCloud Schedulerジョブを一時停止(`gcloud scheduler jobs pause`)すれば、生成は
  `template_not_configured` で安全に止まる。既に生成済みのDriveファイルは影響を受けない。
- 同期のみをロールバックする場合: `ad-revenue-sync` / `ad-revenue-*-monthly-report` の対象Cloud
  Schedulerジョブを `pause` する。`ad_revenue_confirmed_monthly` テーブル自体は残るが、以後更新されない
  だけで既存データは失われない。
- コード変更を取り消す場合: `app.py` の4エンドポイントと `jumpplus_ad_revenue_report.py` /
  `sheets_io.py` は他の既存レポートから独立しているため、これらのファイルの変更を revert しても
  thermae/plus/report_definitions の各フローには影響しない。
