# Retention Deletion Approval Template

ICE Report Generator の retention / archive lifecycle で、GCS object または
Firestore record の実削除を承認・実施する前に Notion へ残す記録テンプレートです。

このテンプレートは削除承認の記録用です。dry-run 結果だけでは実削除を行いません。
実削除は、対象・理由・保持期限・backup・影響範囲を確認し、明示承認を得た後に
個別手順で実施します。

## 使い方

1. `docs/operations.md` の Retention review 手順で dry-run を実行する。
2. 対象が保持期限と除外条件を満たしていることを確認する。
3. このテンプレートを Notion の削除承認記録へ貼り付ける。
4. 承認者が明示承認するまで実削除しない。
5. 実削除後、削除後確認結果と関連ログを追記する。

## 承認前チェック

- dry-run 結果が保存されている
- active delivery の current version が参照する GCS object ではない
- incident 対応、abuse 調査、顧客問い合わせ、再送依頼が継続していない
- GCS object は Drive backup URL を確認済み
- Drive backup folder URL / owner が分かる、または個別 backup URL を確認済み
- Firestore record は最短保持期間を満たしている
- 実削除は rollback できない、または復旧が限定的であることを承認者が理解している

## Notion 記録テンプレート

```text
ICE Report Generator Retention削除承認

対象種別:
対象ID / GCS URI / collection:
件数:
対象期間:
対象月:
顧客名:
delivery_id:
version:

dry-run実行コマンド:
dry-run artifact / 結果URL:
dry-run結果サマリ:

現在の active/current 参照確認:
保持期限を満たしている根拠:
除外条件の確認:
Drive backup URL:
Drive backup folder URL / owner:
Drive backup閲覧確認者:

削除理由:
影響範囲:
復旧可能性 / rollback不可の理解:
実削除手順:
削除前確認結果:

承認者:
承認日時:
実施者:
実施予定日時:
実削除日時:
削除後確認結果:
関連 audit log / admin audit log:
関連PR / commit:
```

## 記録してはいけない項目

Notion、GitHub、Slack、Cloud Logging、ローカル artifact へ次の値を残しません。

- credential
- access key
- secret value
- Admin key
- token
- PIN
- raw recipient email
- message body
- provider event JSON
- signed URL の token 断片

照合が必要な場合は、`delivery_id`、document id、GCS URI、`email_hash`、
`recipient_hash`、`token_hash` など、運用上許可された検索キーだけを使います。

## 判断ルール

- Drive backup URL が確認できない GCS object は削除しない。
- 専用 Drive backup folder が未確定の場合は、個別 backup URL を必須にする。
- Firestore record の実削除は、原則として個別 PR または管理コマンド整備後に行う。
- wildcard や prefix 一括削除を本番 GCS に対して使わない。
- 承認記録が不完全な場合は、削除せずに差し戻す。
