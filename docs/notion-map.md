# Notion構造マップ

目的: Claude（Chat/Code/Cowork）がNotionにアクセスする際、`notion-search`での探索を毎回行わず、
このマップから直接IDを参照してfetch/queryできるようにする。

作成日: 2026-08-26

---

## 主要ページ・データベース

| タイトル | URL | 種別 |
|---|---|---|
| 【ICE Report Generator】Admin UI タブ分割 + report_id明示化 | https://app.notion.com/p/3c1fb056746d81e5a1bee2f6a94a44fd | ページ（タスク管理DBの1レコード。Admin UI関連の作業ログを継続追記する運用） |
| 【ICE Report Generator】レポート定義の追加マニュアル | https://app.notion.com/p/3c2fb056746d8147910dffc172f3f327 | ページ（運用マニュアル、単独ページ） |
| 【ICE Report Generator】部署/プロジェクト別Admin画面分離の導入判断 | https://app.notion.com/p/3c2fb056746d81fb8a46ce0dbff418a0 | ページ（保留中の設計判断記録） |
| 【ICE Report Generator】専用レポート追加 依頼フォーム | https://app.notion.com/p/3c2fb056746d81b3937becd970943f36 | ページ（子にフォーム用DBを持つ） |
| ├ 専用レポート追加 依頼フォーム（DB） | collection://26bbb2a0-303d-48e7-bd59-3eda52eb7919 | データソース（`notion-query-data-sources`で直接クエリ可） |

## 未マッピング（要調査）

- 上記タスクログページの親DB「タスク管理」、プロジェクト管理DB側の「ICE Report Generator」に対応する上位プロジェクトページの正式なマッピングは未確認（`各事業部の業務プロセス改善` 配下にある可能性があるが精査していない）

---

## 運用ルール

- 新規に重要なNotionページ/DBを作成・多用し始めたら、このファイルに追記する
- データベースは `notion-fetch` で取得した際の `<data-source>` collection:// URIも合わせて記載すると、`notion-query-data-sources`（SQL）が直接使え、レコード取得が軽量化する
- 半年〜1年に一度、リンク切れ・統合済みページの棚卸しを行う
