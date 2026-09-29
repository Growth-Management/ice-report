# ジャンプ＋コイン出納レポート（jumpplus-coin-ledger）

`report_definitions` では対応できない専用（bespoke）レポート。`docs/new-bespoke-report-playbook.md` に沿って実装した。

- report_id: `jumpplus-coin-ledger`
- モジュール:
  - `jumpplus_coin_ledger_report.py`: 対象月・readiness・BigQuery・アップロード・オーケストレーション・ローカルCLI
  - `jumpplus_coin_ledger_workbooks.py`: テンプレート専用のXLSX writer
- 配布: 3ファイルを1つのOTP付き配布URL（multi-file delivery）で配布する。顧客へのメール自動送信は行わない

> 状態（2026-09-28）: ローカル実装・unit test・実テンプレートテストまで完了。Golden Run、Production deploy、Cloud Scheduler job / SA 作成、IAM変更、本番delivery発行はいずれも未実施（末尾「Production未実施項目」）。

## 目的

ジャンプ＋の月次コイン出納（発行・繰越・消費・システム調整・残高）と、作品別・コンテンツ別のコイン消費を、集英社向け正式Excelテンプレートで月次に作成して配布する。

1回の実行で次の3ファイルを1つの納品単位として作成する。

| file_key | 表示名 | ファイル名 | 内容 |
|---|---|---|---|
| `app` | App版 | `【少年ジャンプ＋】消費コイン_yyyy年mm月期_yymmdd.xlsx` | iOS + Android（出納3ブロック・サマリ3シート・明細4シート） |
| `web` | WEB版 | `【少年ジャンプ＋】消費コイン_WEB_yyyy年mm月期_yymmdd.xlsx` | Web（出納・サマリ・話/巻明細。表記は「ポイント」） |
| `product` | 話売商品一覧 | `話売商品_一覧_yymm.xlsx` | 現在の全話売商品マスタ（target_monthの購入実績ではない） |

- `yyyy年mm月期` と `yymm` は target_month、`yymmdd` は生成日（Asia/Tokyo）
- 例: target_month = 2026-08、生成日 = 2026-09-01 の場合、`…_2026年08月期_260901.xlsx` / `…_WEB_2026年08月期_260901.xlsx` / `話売商品_一覧_2608.xlsx`

## Data sources

BigQuery project `jumpplus-4a5f4`（読み取りのみ）。

| 用途 | table | 条件 |
|---|---|---|
| 出納 | `dataset_datamart_tables.report_plus_monthly_coin_report` | `month_jst = target_month`。`app_pf` は iOS / And / Web、`coin_type` は現行の6区分 |
| コンテンツ別消費 | `dataset_datamart_tables.report_plus_monthly_coin_content_report` | `purchase_date_month_jst = target_month`。`app_id` は 31(iOS) / 32(And) / 101(Web)、`purchase_type` は episode / book |
| 話売商品マスタ | `dataset_aggregation_tables.raise_master_contents_works` | `content_type = 'episode'`（件数はコードに固定しない） |

coin_type（出納シートの行順）:

| 行 | coin_type | App表記 | WEB表記 |
|---|---|---|---|
| 1 | `pay_coin` | 有償コイン | 有償ポイント |
| 2 | `pay_bonus_coin` | 購入お得コイン | 購入お得ポイント |
| 3 | `free_ad_coin` | 無償広告コイン | 無償広告ポイント |
| 4 | `free_bonus_coin` | 無償ボーナスコイン | 無償ボーナスポイント |
| 5 | `pay_gift_coin` | 贈答用購入コイン | 贈答用購入ポイント |
| 6 | `reward_video_ad_coin` | 動画リワード広告コイン | 動画リワード広告ポイント |

### `_mom` 非依存

`report_plus_monthly_coin_content_report_mom` は前月固定のため、任意の target_month では使えない。

- base table を `purchase_date_month_jst = @target_month` で直接読む
- テーブル名を env で上書きした場合でも、`_mom` で終わる名前は `forbidden_source_table` で拒否する（`_checked_table`）
- `_mom` にしか無い補助列4つは固定値 `'-'` で補う（`MOM_ONLY_FIXED_COLUMNS`）: `ex_comics_start_date`, `ex_note`, `ex_magazine`, `ex_file_type`

調査時点（2026-08）の記録: base table から同じ列契約で作った結果が `_mom` と193,196行で完全一致した。

## target_month

| 実行 | 規則 |
|---|---|
| 手動 `POST /admin/reports/jumpplus-coin-ledger/generate` | `target_month` 必須。`YYYY-MM` または `YYYY-MM-01`。過去月なら任意に指定できる |
| 自動 `POST /admin/reports/jumpplus-coin-ledger/scheduled-generate` | 省略時は前月（Asia/Tokyo）。例: 2026-10-01 07:00 JST → 2026-09。年跨ぎにも対応（2027-01-01 → 2026-12） |

未完了の月（当月以降）と不正な形式は `invalid_target_month`（400）で拒否する。

## Readiness

07:00になったことだけを理由に生成しない。生成前に次をすべて確認し、1つでもNGなら `source_not_ready`（HTTP 503、`retryable: true`）とする。

| check | 条件 | NG時のreason |
|---|---|---|
| ledger | iOS / And / Web が揃い、各platformに6 coin_typeが1行ずつ（3×6 = 18行） | `ledger_platform_missing` / `ledger_coin_type_missing` / `ledger_duplicate_grain` / `ledger_unexpected_coin_type` |
| content | app_id 31 / 32 / 101 に行がある | `content_platform_missing` |
| reconciliation | platformごとに `SUM(content.total_use_coins)`（episode+book）= `SUM(ledger.use_coins_m)` | `ledger_content_reconciliation_mismatch` |
| product master | episodeが1行以上あり、`id` にnull・重複が無い | `product_master_empty` / `product_master_duplicate_id` |

readinessがNGのときは次のとおりになる。

- Excelを作らない。Drive / GCS へは何も置かない。deliveryも作らない
- scheduled runの記録は `source_not_ready` とし、後でclaimし直せる状態のまま残す（成功扱いで固定しない）
- ログ・レスポンスに出すのは件数とpass/failだけで、コイン合計値は出さない

2026-08の確認値（reconciliation）: iOS 597,971,628 / And 235,717,131 / Web 11,367,402。

出納シートの書き込み時には、テンプレートの残高式（`F = C + B - D + E`）で計算した値と `balance_coins_m` を突き合わせる。不一致なら `ledger_balance_mismatch` で停止する。

## テンプレート

正式テンプレートはDriveフォルダ `13M5HROiHR9mHGOjQ_a5mmJ8eTNWrVATA` に置かれている。更新運用はシステム管理ユニットが担当する。

| file_key | Drive file ID | env（上書き用） |
|---|---|---|
| app | `1jZuoDggHBtfeiEPrRscImSo_k--l_I66`（出納独立版、2026-09-28更新） | `JUMPPLUS_COIN_LEDGER_APP_TEMPLATE_FILE_ID` |
| web | `1kD_KjDSNmhp-JrP8OkAUaDosobeUvftB` | `JUMPPLUS_COIN_LEDGER_WEB_TEMPLATE_FILE_ID` |
| product | `1eMWR4uh8y06-He6I0_7csTy7EStbztMt` | `JUMPPLUS_COIN_LEDGER_PRODUCT_TEMPLATE_FILE_ID` |

ローカル検証用のコピーは `C:\temp\jumpplus-coin-ledger-templates` に置く。Gitには追加しない。`tests/test_jumpplus_coin_ledger_workbooks.py` の実テンプレートテストはこのpath（または env `JUMPPLUS_COIN_LEDGER_TEMPLATE_DIR`）を使い、どちらも無い環境ではskipする。

### 構造（2026-09-28 解析）

いずれも Power Query / connections / queryTable / customXml は無い。

**App**（8シート。App側の定義名は `#REF!` の残骸だけなので触らない）

- `出納`（先頭シート、zoom 85%）: 合計 / Apple / Google の3ブロック
  - 位置: ヘッダー行 3 / 15 / 27（A列は「コイン種別」）、コイン行 4-9 / 16-21 / 28-33、総計行 10 / 22 / 34
  - 書くのは B-E 列（発行 / 繰越（※前月以前） / 消費 / システム調整※棚卸し）だけ。F-H 列（残高 / 当月消費率 / 繰越込消費率）と総計行はテンプレートの数式を保持する
  - 値: 合計 = iOS+And、Apple = iOS、Google = And。B=`issue_coins_m`、C=`carried_over_coins_m`、D=`use_coins_m`、E=`cancellation_coins_m`
- `サマリ` / `サマリ (Apple)` / `サマリ (Google)`（zoom 85%）: Excel Table `サマリ_Total`（A:O、種別列あり）/ `サマリ_Apple` / `サマリ_Google`（A:N）
  - いずれもSUBTOTALの合計行付きで、空の書式行が事前確保されている。作品数に合わせて行数を合わせる
  - 作品名 = `ex_work_name`
  - 各platformシートにも合計シートと同じ作品一覧を出す（該当platformの消費が無い作品は0）
  - 並び（work list）: 作品ごとの `MIN(name_kana)` 昇順、同値は作品名昇順（NULLは先頭）。App の3シートは同じwork list順、WEBはWEB側のwork list順。2026-08 Golden Masterと行順が完全一致（App 1,745 / WEB 821作品）
- 明細4シート（zoom 80%、E4で固定）: `有料話消費コイン（Apple/Google）`（Table `話明細_*`）、`有料巻消費コイン（Apple/Google）`（Table `巻明細_*`）。18列で、後ろ2列は話=コミックスJDCN / コミックス巻数、巻=雑誌 / 種別
  - 並び: `name_kana` 昇順、同値は `content_id` 昇順。Golden Masterの全明細シートは `name_kana` で単調非減少かつ各kanaの行数が一致することを確認済み。Golden側の同値内の順序はどの列でも再現できない（ソース側で非決定的）ため、同値の中の順序は `content_id` で決定的にしている（2026-08: 同値グループ内の125位置がGoldenと異なる。**ACCEPTED_DIFF確定(2026-09-29)**: `scripts/build_coin_ledger_order_groups.py` でBigQuery content行から `コンテンツID_Raise -> name_kana` を再現し、`--order-groups` で厳密検証(name_kana groupの並び順一致・各groupの行数一致・group内キー集合一致・全列値一致・差分は全てgroup内部の順列のみ)を通過。5シート(Apple話45/Google話62/Apple巻2/Google巻6/WEB話10)すべてPASSし、合計125件と一致。`compare_coin_ledger_golden.py` はこの検証結果を `detail_row_order_within_name_kana_ties` としてACCEPTED_DIFFへ分類する）

**WEB**（4シート。Excel保存版で、calcChain・shared formula・`_FilterDatabase` 定義名がある）

- 出納（1ブロック、行4-9、総計10。A3の見出しは空）、`サマリ`（Table `サマリ_Total`）、`有料話消費ポイント`、`有料巻消費ポイント`

**話売商品**（シート `file`、Table `テーブル1` A1:J、ヘッダー1行目、A2固定、zoom 85%）

- 列: コンテンツID_Raise=`prefixed_id`、コンテンツID=`v2_content_id_token`、コンテンツ名=`name`、作品名=`work_title`、著者名=`author_name`、JDCN=`jdcn`、価格（コイン）=`price_in_coin`、配信開始日=`ex_sales_start_date`（Excelネイティブ日付、NULLは空セル）、コミックスJDCN=`ex_comics_jdcn`、コミックス巻数=`ex_episode_package_no`
- 「生成時点の全話売商品マスタ」を出す仕様。価格・配信開始日・コミックスJDCN・コミックス巻数はマスタの現在値を正とし、過去の納品物（購入履歴ベースの旧一覧）との差は current-master drift として扱う。差分件数はマスタの更新で変わるため固定の期待値にしない

明細の列の対応: コンテンツID_Raise=`prefixed_id`、コンテンツID=`v2_content_id_token`、価格=`unit_price`、DL数=`download_count`、消費=`total_use_coins`、有償=`pay_coins_total`、購入お得=`pay_bonus_coins_total`、無償広告=`free_ad_coins_total`、無償ボーナス=`free_bonus_coins_total`、贈答=`pay_gift_coins_total`、動画リワード広告=`reward_video_ad_coin_count`、配信開始日=話は`ex_sales_start_date`（Excelネイティブ日付。テンプレートの `yy/mm/dd` 書式のまま、NULL / NaT は空セル）、巻は固定値`'-'`（旧`_mom`の`ex_comics_start_date`相当。`ex_sales_start_date`は使わない）、備考=`ex_note`、作品名=`ex_work_name`、雑誌=`ex_magazine`、種別=`ex_file_type`。

サマリの「種別」: App は当月の購入実績(明細)に現れた `ex_comic_type` を**すべて**('/' 連結で複数可)、WEB は常に空欄(ex_comic_typeを出さない。Golden Masterの821作品すべて空欄と一致)。

**2026-09-29 追加調査で解決・確定(旧: 未確定だった12作品差)**:

- 当初「作品内の最頻値」(1種類だけ選ぶ)だった実装が原因で、当月の購入実績に複数の`ex_comic_type`がある10作品(例: チェンソーマン、ヘタリアWorld☆Stars)で単一値に丸められていた。**修正: 当月実績にある型を全て`/`連結する**(`build_summary_rows`の`comic_types`をCounterからsetへ変更)。
- 連結順は `raise_master_contents_works` から取得する(`fetch_comic_type_order_dates`): 各`ex_comic_type`が最初に`content_type='episode'`として現れた日付(なければ任意の`content_type`での最初の日付)の昇順。ただし **`ノベル`は常に最後**(本編に対する副次コンテンツという位置づけ。2026-08 Golden Masterで確認した全ての複合値作品は主要種別が先・ノベルが後だった)。この2条件(ノベル最後・非ノベルは episode優先の日付昇順)で、2026-08 Golden Masterの複合種別10作品すべての連結順を再現できることを確認した(例: チェンソーマンはJC episodeがJC+ episodeより先に始まったため「JC/JC+」、ヘタリアWorld☆Stars/幼稚園WARSはJC+のepisodeがJCより大幅に先だったため「JC+/JC」)。
- **重要**: 対象となる型の集合(どの`ex_comic_type`を表示するか)は、あくまで**当月の購入実績**から決める。`raise_master_contents_works`の全履歴の型をそのまま採用すると、当月1種類しか売れていない大多数の作品(Golden Masterでは単一値)まで誤って複合値になってしまうことを実装検証で確認した(一度その誤った設計で試し、unexpected diffが12→37件に悪化したため撤回)。masterは「複数観測された型をどの順で連結するか」の決定にのみ使う。
- **2026-08 Golden限定のACCEPTED_DIFFとして確定(2026-09-29)**: UNTRACE-アントレース-／ネーム版、スミハナ-贋作浮世絵巻- の2作品。
  - 当月の明細では`ex_comic_type`が全てNULL。`raise_master_contents_works`を確認したところ、この2作品はいずれも`ex_comic_type='その他'`(`JC+`ではない)のみで、Golden Master値「JC+」を裏付けるデータが現行sourceのどこにも見つからなかった。
  - **現行の正式source(当月content detail・raise_master_contents_works)からGolden値を再現する決定的ルールが存在しない** -- Goldenに合わせるには作品名固有のhardcodeが必要になるため、**writerには一切手を入れていない**(作品名によるJC+ hardcode・NULLの一律JC+変換・masterの「その他」のJC+変換・Goldenからの逆引きは、いずれも実施しない・今後も禁止)。`種別`はsource-drivenのまま`None`を出力する。
  - `scripts/compare_coin_ledger_golden.py`へ、`--target-month 2026-08` かつ 対象キーが厳密に上記2作品**のみ**の場合に限り `legacy_golden_only_comic_type_2026_08` としてACCEPTED_DIFFへ分類する処理を追加した(`LEGACY_GOLDEN_ONLY_COMIC_TYPE_2026_08_KEYS`)。**これは汎用的な「種別が違えばACCEPTED」ルールではない**: 別作品でtype setが不一致の場合、別target_monthで同様の差が発生した場合、複合種別の集合自体が不一致な場合は、引き続きFAIL(unexpected)として扱う。3件目のキーが混入した場合もそのキー分はunexpectedのまま。
  - **今後の運用方針**: 新しい月次実行で同種の(NULL/その他型なのにGoldenだけ値がある)差分が別作品に発生しても、自動的にACCEPTED_DIFF扱いにしない。都度再調査し、必要なら個別に承認・追加する。

※ 列の対応・並び順・日付型は 2026-08 Golden Run（下記）で確認した。

### Writer

`xlsx_package_writer`（package-preserving）で書く。openpyxlで production workbook を load→save することはしない。

- 使う関数: `replace_detail_rows`（Table resize、合計行と数式の保持）、`write_fixed_cells`（固定セル。数式セルへの上書きは拒否）、`read_cell_texts`（テンプレートのラベル位置の検証）、`sync_filter_database_names`、`force_recalculation_on_load`
- テンプレートのラベルや見出しが変わっていたら、`ledger_template_layout_mismatch` / `template_header_missing` で停止する
- 生成したファイルはアップロード前に `zipfile.testzip()`、全XML part の parse、openpyxl read-only での読み戻しで検証する

## 3ファイル / 1 delivery

- 順序: 3ファイルをローカルで生成・検証 → GCSへ3件 → Driveへ3件 → Firestoreのdeliveryを1回のトランザクションで書く
- Driveの途中で失敗した場合は、その回にアップロード済みのDriveファイルをゴミ箱へ移す（best-effort）。deliveryは書かないので、一部のファイルだけが公開されることはない
- GCS: `BUCKET_NAME`（`JUMPPLUS_COIN_LEDGER_BUCKET_NAME` で上書き可）の `reports/jumpplus-coin-ledger/YYYY-MM/<run_stamp>/<file_name>`。run_stampごとにpathを分け、過去versionのobjectは上書きしない
- Drive: 正式フォルダ `13M5HROiHR9mHGOjQ_a5mmJ8eTNWrVATA`（`JUMPPLUS_COIN_LEDGER_OUTPUT_FOLDER_ID`）
- delivery ID: `jumpplus-coin-ledger_YYYY-MM`（固定）。1 report・1 targetMonth につき公開URLは1つ
  - `delivery_kind: "multi_file"`
  - versionごとに `files: [{file_key, label, gcs_uri, bucket, object_name, file_name}]` を持つ
  - `files` を持たない version は従来の single-file として扱う（既存schemaはそのまま）
- allowlist / 期限: 既存の標準を使う。許可domainは管理画面の既定と同じ `shueisha.co.jp, sur.co.jp, hitotsubashi.co.jp, impress.co.jp`（`JUMPPLUS_COIN_LEDGER_ALLOWED_DOMAINS` で上書き可）、期限は `DEFAULT_EXPIRES_DAYS`
- 受信者の画面: OTP認証は1回。その後にファイル一覧（App版 / WEB版 / 話売商品一覧）を表示し、各ファイルを個別にダウンロードする
  - session TTL は既存どおり
  - one-time はファイル単位: 同じsessionで各ファイル1回ずつ。3ファイルすべて取得した時点でsessionを使用済みにする
  - ダウンロードはファイルごとに `download_logs` へ記録する
  - single-file delivery は従来どおり、認証後に1ファイルへ直接redirectする
- 既存の `/deliveries/<id>/versions`（single-file用）は、multi-file delivery に対しては 409 `multi_file_delivery_managed_by_report` を返す
- Slack通知には、配布URL・token・GCS pathを含めない

## Rerun / version

> 過去月を再生成した場合、上流（`report_plus_monthly_coin_report` 等）の履歴が後日補正されていれば、当時納品したファイルと繰越・残高などが異なる場合がある（例: 2026-08 WEB出納の繰越3区分）。BigQueryの現在値を正とし、過去納品値のimmutable snapshot化は行っていない（scope外）。話売商品も生成時点のマスタを出すため、再生成すると当時と内容が変わり得る。

- 同じ target_month を手動で再生成すると、既存deliveryを残したまま新しい version（3ファイルのセット）を追加し、`current_version` を切り替える。公開URL・allowlist・期限・active状態は変わらない
  - 注意: version追加では `expires_at` を延長しない（既存の `add_delivery_version` と同じ扱い）
- `create_delivery: false` を指定すると、Drive保存までを行いdeliveryは作らない
- scheduled実行はidempotency key `scheduled:YYYY-MM` を持つ。Scheduler retryで同じversionが二重に追加されることはない

## Scheduler

| 項目 | 値 |
|---|---|
| job | `jumpplus-coin-ledger-monthly-report`（**未作成**） |
| schedule | `0 7 1 * *`（毎月1日 07:00） |
| timezone | `Asia/Tokyo` |
| URI | `https://report-generator-635067190197.asia-northeast1.run.app/admin/reports/jumpplus-coin-ledger/scheduled-generate`（POST） |
| OIDC audience | Cloud Runのルートorigin（パスなし）。env `JUMPPLUS_COIN_LEDGER_SCHEDULER_AUDIENCE` に同じ値を設定する |
| 呼び出しSA | `jumpplus-coin-ledger-scheduler@ice-sh.iam.gserviceaccount.com`（予定、**未作成**）。env `JUMPPLUS_COIN_LEDGER_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS` |
| retry（提案） | max-retry-attempts 5、min-backoff 600s、max-backoff 3600s、max-doublings 3、attempt-deadline 1800s（07:00から約3時間retryする。repoにScheduler retryの標準が無いため新規提案） |

scheduled-generateの応答（Cloud Schedulerは非2xxをretryする）:

| 応答 | 状況 |
|---|---|
| 200 `succeeded` | 生成・配布が完了した |
| 200 `skipped` | 生成済み（`already_generated`）、またはscheduled versionが既にある（`already_delivered`） |
| 503 `source_not_ready` | データ未達。何も作らない。retryで再実行する |
| 409 | 同じ月を別の呼び出しが実行中 |
| 5xx | その他の失敗。記録は `failed` になり、次のretryでclaimし直せる |

- 重複防止: `_claim_scheduled_run`（collection `jumpplus_coin_ledger_scheduled_runs`、run_id = `YYYY-MM`）
- `failed` / `source_not_ready`、および30分以上更新されていない `running`（`JUMPPLUS_COIN_LEDGER_STALE_RUNNING_MINUTES`）は、次の呼び出しでclaimし直せる
- ログ: 構造化JSONでstdoutへ出す（severity INFO / WARNING / ERROR、`component: jumpplus-coin-ledger`）。項目は report / target_month / trigger / readiness / row counts / Drive upload completion / delivery_id / elapsed。SQL本文・セル値・token・PIN・Signed URL・生メールは出さない

## Schedules registry

`report_schedules.REPORT_SCHEDULE_SPECS` の `jumpplus-coin-ledger` エントリに、上表の expected（cron / timezone / endpoint / audience = service root）が登録されている。管理画面の Schedules タブでは、job作成前は `NOT_CREATED`、作成後は `OK` になることが完了条件（`docs/operations.md`「Schedules」参照）。

## Golden Run（2026-08 実施: 2026-09-28）

- target_month: 2026-08
- 方法: ローカルCLI（BigQueryを読み、ローカルでファイルを作るだけ。Drive / GCS / Firestore / delivery には触れない）

```powershell
python jumpplus_coin_ledger_report.py --target-month 2026-08 --generated-date 2026-09-01 `
  --template-dir <app.xlsx / web.xlsx / product.xlsx を置いたdir> --output-dir <ローカル出力先>
```

期待値:

| 対象 | 期待 |
|---|---|
| App サマリ | 1,745作品 / 833,688,759 |
| Apple サマリ | 1,745作品 / 597,971,628 |
| Google サマリ | 1,745作品 / 235,717,131 |
| WEB サマリ | 821作品 / 11,367,402 |
| Apple 話 | 69,960行 / 416,659,205 |
| Google 話 | 68,266行 / 182,865,295 |
| Apple 巻 | 9,679行 / 181,312,423 |
| Google 巻 | 6,980行 / 52,851,836 |
| WEB 話 | 35,623行 / 5,468,395 |
| WEB 巻 | 2,688行 / 5,899,007 |
| 話売商品 | 100,823行（2026-09-28調査時点の参考値。コードには固定しない） |

突合: `scripts/compare_coin_ledger_golden.py`（ローカルファイルのみ。値は出さず件数・列別diff・キーのサンプルを出す。`--expected` で上表、`--order-groups` で並びの同値グループを検証）。差分は unexpected / accepted（理由付き）に分けて数える。

結果（4回目、2026-09-29 追加調査・writer修正・legacy allowlist追加込み）: 判定 **PASS_WITH_ACCEPTED_DIFFS（unexpected 0）**。期待値表の件数・総計はすべて一致。`--target-month 2026-08` を指定して実行（後述のlegacy allowlistは2026-08限定のため必須）。

| 区分 | 内容 | 件数 |
|---|---|---|
| 一致 | App/Apple/Google/WEB サマリ（行順・作品集合・全列。WEB種別は空欄で一致）、App サマリ種別（当月実績の型を`/`連結、1,743/1,745作品で一致）、全明細の全列（コンテンツID=`v2_content_id_token`、話の配信開始日=Excel日付、巻の配信開始日 `'-'`）、App出納3ブロック（B-H、システム調整=`cancellation_coins_m`）、Apple/Googleの0件作品の0埋め | - |
| accepted: detail_row_order_within_name_kana_ties | 明細の行順（`--order-groups`で厳密検証、name_kana同値グループ内の順列のみ。Apple話45 / Google話62 / Apple巻2 / Google巻6 / WEB話10） | 125 |
| accepted: legacy_golden_only_comic_type_2026_08 | App サマリ種別: UNTRACE-アントレース-／ネーム版、スミハナ-贋作浮世絵巻- の2作品限定(上記「サマリの『種別』」節参照。汎用ルールではない) | 2 |
| accepted: WEB historical ledger drift | WEB出納の繰越（有償・無償ボーナス・贈答用購入）と連動する残高・繰越込消費率 | 9セル |
| accepted: current master | 話売商品のID追加 20,695 / 価格 3,053 / 配信開始日 414（マスタNULL）/ コミックスJDCN 113 / 巻数 113 | - |
| accepted: Golden不備 | WEB「有料巻消費ポイント」がGoldenでは話シートのコピー。生成物は2,688行・5,899,007・キー一意・話とキー重複なしで独立検証 | - |

`"NaT"` 文字列は0件（修正前は話売商品で21,109セル）。

前回（2回目、仕様確定後）は判定 FAIL（unexpected 137: 種別12 + 行順125）だった。種別12件の詳細は「明細4シート」節の「サマリの『種別』」を参照。行順125件は本節「明細4シート」の並び節を参照。

## 運用手順

- **月次（自動）**: 毎月1日 07:00 JST にScheduler jobが前月分を実行する
  - `source_not_ready` の間はretryが続く。約3時間経っても未達なら、TROCCO / データマートの状況を確認し、揃ってから手動実行する
- **手動実行 / 再生成**:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "$base/admin/reports/jumpplus-coin-ledger/generate" `
  -Headers @{ 'X-Admin-Key' = $adminKey } `
  -ContentType 'application/json' `
  -Body '{"target_month":"2026-08"}'
```

  - レスポンスの `delivery.public_download_url` を、社内手順に従って送付先へ連絡する（自動メールは無い）
  - URLは公開チャンネルに貼らない
- **確認**:
  - Cloud Logging: `jsonPayload.component="jumpplus-coin-ledger"`
  - Firestore: `jumpplus_coin_ledger_scheduled_runs/<YYYY-MM>` の `status`
  - 管理画面: 配布一覧、Schedules タブ
- **データ未達のとき**: `reasons` を確認する（件数とpass/failのみが出る）。readinessの条件を緩めて生成することはしない

## Rollback

- 誤ったversionを公開してしまった場合:
  - 修正後に再生成して新しいversionをcurrentにする
  - 配布自体を止める場合は `POST /deliveries/<delivery_id>/disable`
- 自動実行を止める場合: Cloud Scheduler job の pause（別途承認）
- アプリのrollback: `docs/deploy.md` の手順で前のrevisionへ戻す（`report-generator` と `report-generator-admin` の両方）
- 誤ってDriveに置いたファイル: Drive上で手動削除する（アプリがゴミ箱へ移すのは、同じ実行内で途中失敗したときの自分のアップロード分だけ）

## Production未実施項目

- [x] 2026-08 Golden Run と突合結果の記録（2026-09-29: **PASS_WITH_ACCEPTED_DIFFS、unexpected 0**。詳細は「Golden Run」節）
- [ ] Production deploy（`app.py` を変更しているため `report-generator` と `report-generator-admin` の両方）
- [ ] Cloud Scheduler SA `jumpplus-coin-ledger-scheduler` の作成と、Cloud Run env（`JUMPPLUS_COIN_LEDGER_SCHEDULER_ALLOWED_SERVICE_ACCOUNTS` / `_AUDIENCE`）の設定
- [ ] Cloud Scheduler job `jumpplus-coin-ledger-monthly-report` の作成、OIDC smoke、重複（409）smoke、source_not_ready（503）smoke
- [ ] 3-file delivery smoke（OTP → 一覧 → 3ファイル個別DL → 同じsessionでの再DLが拒否されること）
- [ ] INFOログがCloud Loggingに届くことの確認
- [ ] Schedules タブで `jumpplus-coin-ledger` が `OK` になること
- [ ] `roles/cloudscheduler.viewer` の付与（`docs/operations.md`「Schedules」。Production deploy準備フェーズで別途承認）
