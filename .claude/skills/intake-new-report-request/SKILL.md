---
name: intake-new-report-request
description: Use when the user asks to check or process new submissions from the "専用レポート追加 依頼フォーム" Notion form — e.g. "依頼フォームの新規受付を確認して", "新規レポート依頼を処理して", "フォームに新しい依頼が来てないか見て", "依頼フォームから新規受付して". Reads pending (ステータス=新規受付) rows from the form's Notion database, summarizes each request, checks it against the new-bespoke-report intake checklist for missing/unclear information, and (after user confirmation) updates status and hands off to the new-bespoke-report skill. Do NOT use this for the Admin UI's report_definitions feature (different, self-service flow) or for actually implementing a bespoke report (that's new-bespoke-report).
version: 1.0.0
---

# 依頼フォームの新規受付処理(intake-new-report-request)

Notion上の「専用レポート追加 依頼フォーム」に届いた依頼を確認し、`new-bespoke-report` skillでの着手判断ができる状態まで整理するskillです。フォーム自体・DB構造は `docs/notion-map.md` に記載があります。まずそちらでcollection IDを確認し、`notion-search` は使わず直接 `notion-fetch` / `notion-query-data-sources` を使ってください。

対象データソース(collection ID): `26bbb2a0-303d-48e7-bd59-3eda52eb7919`(`docs/notion-map.md` の「専用レポート追加 依頼フォーム（DB）」参照。IDが変わっていたら先にそちらを更新する)

## Step 1 — 新規受付を取得する

`notion-query-data-sources` で `ステータス = '新規受付'` の行を取得する。「記入例(サンプル)」ステータスの行は対象外なので自然に除外される。

```json
{
  "data": {
    "data_source_urls": ["collection://26bbb2a0-303d-48e7-bd59-3eda52eb7919"],
    "query": "SELECT * FROM \"collection://26bbb2a0-303d-48e7-bd59-3eda52eb7919\" WHERE \"ステータス\" = ? ORDER BY \"受付日時\" ASC",
    "params": ["新規受付"]
  }
}
```

該当0件なら「新規受付は現在ありません」とだけ報告して終了する。

## Step 2 — 各依頼を要約し、不足項目を照合する

取得した行ごとに、以下を`new-bespoke-report`プレイブック(`docs/new-bespoke-report-playbook.md`)のStep 1着手前チェックと突き合わせる。

| フォーム項目 | 対応するプレイブックの着手前確認事項 |
| --- | --- |
| データソース(BigQueryプロジェクト・データセット・テーブル・集計ロジック) | データソース |
| 配布方式 | 配布方式(Drive保存のみ / OTP付きダウンロードURL) |
| テンプレート所有(格納場所・更新運用担当) | テンプレート所有 |
| スケジュール実行 / スケジュール頻度・時間帯 | スケジュール実行の要否・頻度 |

各依頼について、ユーザーへ次の形式で要約する。

- レポート名(仮)・依頼者・依頼者メール・顧客/納品先・希望対応時期
- 上記4項目それぞれの内容(空欄または「未確定/相談したい」の場合は明示的に「未確定」と書く)
- 補足・その他の内容
- 4項目すべてに具体的な回答があるか(YES/NO)。NOの場合、何が不足しているかを具体的に示す

## Step 3 — ユーザーに判断を仰ぐ

要約を提示したうえで、ユーザーに以下を確認する。

- この依頼を`new-bespoke-report` skillでの実装検討に進めてよいか
- 不足情報がある場合、依頼者へどう確認するか(Notionコメント、Slack、メール等はユーザー側で実施。このskillは自動で依頼者へ連絡しない)

ユーザーの回答を待たずに次のステップ(ステータス更新・実装着手)へ進めない。

## Step 4 — ステータスを更新する(ユーザー確認後のみ)

`notion-update-page`(`update_properties`)で対象ページの`ステータス`を更新する。

- 進める場合: `確認中`(不足情報の確認待ち)または`実装着手`(情報が揃っており着手する場合)
- 保留する場合: `保留`

対応担当エンジニアの割り当てもこのタイミングでユーザーに確認して設定する(`対応担当エンジニア`プロパティ、PEOPLE型)。

## Step 5 — 実装へ引き継ぐ

`実装着手`にした依頼は、要件が`new-bespoke-report`プレイブックのStep 1着手前確認事項を満たした状態として扱い、そのまま`new-bespoke-report` skillの Step 2 以降(専用モジュール作成)へ進める。このskill自体はレポートの実装は行わない。

## 注意事項

- フォームの依頼者メールアドレスなど個人情報は、ユーザーへの報告時も含め、必要な範囲でのみ扱う。Slack通知や公開チャンネルへ転記しない
- 1回の実行で複数件の新規受付がある場合は、全件をまとめて要約してから、どれから着手するかをユーザーに確認する

## 関連

claude.aiのChatやChatGPTなど、このリポジトリのファイルを直接参照できない環境で同じヒアリングを進める場合は、`docs/new-report-intake-portable-prompt.md`(自己完結版プロンプト、各プラットフォームへの設置手順つき)を使う。処理の流れを変更したときは両方を合わせて更新する。
