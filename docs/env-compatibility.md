# Environment Compatibility Inventory

ICE Report Generator の旧 env 名と AWS access key 前提手順の棚卸しです。現在の本番経路は Cloud Run service account から AWS STS `AssumeRoleWithWebIdentity` を使う構成で、長期 AWS access key は使いません。

## 結論

- 本番 Cloud Run `report-generator` には旧 env 名は設定されていません。
- 本番 Cloud Run は `AWS_SES_*` の canonical 名だけを使います。
- `mail_runtime.py` の旧 env fallback は、過去の設定や検証環境との互換目的で当面残します。
- 新規 deploy、手順書、運用記録では旧 env 名を使いません。
- Secret Manager の legacy access key secret は、値を読まずに存在確認したうえで 2026-06-01 に削除済みです。

## 2026-05-26 棚卸し結果

### Cloud Run

確認対象 service:

- project: `ice-sh`
- region: `asia-northeast1`
- service: `report-generator`

旧 env 名の残存:

- `AWS_SES_ACCESS_KEY_ID`: なし
- `AWS_SES_SECRET_ACCESS_KEY`: なし
- `MAIL_FROM_EMAIL`: なし
- `MAIL_FROM_NAME`: なし
- `MAIL_PROVIDER_SES_REGION`: なし
- `MAIL_PROVIDER_SES_CONFIGURATION_SET`: なし
- `MAIL_PROVIDER_TIMEOUT_SECONDS`: なし

SES 関連の現行 env 名:

- `MAIL_PROVIDER`
- `AWS_SES_REGION`
- `AWS_SES_FROM_ADDRESS`
- `AWS_SES_ROLE_ARN`
- `AWS_SES_WEB_IDENTITY_AUDIENCE`

### Secret Manager

legacy access key secret として残存を確認:

- `aws-ses-access-key-id`
- `aws-ses-secret-access-key`

この 2 つは現在の本番 mail runtime では使いません。

2026-06-01 に明示承認後、次の 2 Secret を削除しました。削除後の `gcloud secrets describe` はどちらも `NOT_FOUND` を返すことを確認済みです。

- Secret Manager `aws-ses-access-key-id`
- Secret Manager `aws-ses-secret-access-key`

### Cloud Run 参照確認

`asia-northeast1` の Cloud Run services / jobs で、legacy secret または旧 env 名を参照していないことを確認済みです。

- services checked: `2`
- jobs checked: `0`
- legacy secret / 旧 env 参照: `0`

## Env 名の扱い

| 用途 | Canonical | 旧 fallback | 判断 |
| --- | --- | --- | --- |
| SES from address | `AWS_SES_FROM_ADDRESS` | `MAIL_FROM_EMAIL` | fallback は互換目的で残す。新規設定では使わない |
| SES from name | `AWS_SES_FROM_NAME` | `MAIL_FROM_NAME` | fallback は互換目的で残す。新規設定では使わない |
| SES region | `AWS_SES_REGION` | `MAIL_PROVIDER_SES_REGION`, `AWS_REGION`, `AWS_DEFAULT_REGION` | `AWS_SES_REGION` を正とする。AWS 標準名は local/dev 互換として残す |
| SES configuration set | `AWS_SES_CONFIGURATION_SET` | `MAIL_PROVIDER_SES_CONFIGURATION_SET` | fallback は互換目的で残す。新規設定では使わない |
| SES timeout | `AWS_SES_TIMEOUT_SECONDS` | `MAIL_PROVIDER_TIMEOUT_SECONDS` | fallback は互換目的で残す。新規設定では使わない |
| SES auth | `AWS_SES_ROLE_ARN`, `AWS_SES_WEB_IDENTITY_AUDIENCE` | `AWS_SES_ACCESS_KEY_ID`, `AWS_SES_SECRET_ACCESS_KEY` | access key 方式は本番非採用。Secret は 2026-06-01 削除済み |

## 運用ルール

- 新しい手順や deploy では canonical 名だけを書く。
- fallback 名は「既存環境を壊さないための読み取り互換」として扱う。
- fallback 名を使った本番復旧は行わない。
- access key 方式へ戻さない。
- `MAIL_PROVIDER=logging` は access key 方式ではなく、OTP 送信停止時の切り分け・一時 rollback 用として残す。

## 削除実施結果

2026-06-01 に明示承認後、削除済み:

- Secret Manager `aws-ses-access-key-id`
- Secret Manager `aws-ses-secret-access-key`

削除前確認結果:

1. Cloud Run services に `AWS_SES_ACCESS_KEY_ID` / `AWS_SES_SECRET_ACCESS_KEY` 参照がない
2. Secret version を参照する別 service / job がない
3. SES 実送信 smoke test が Web Identity 経路で成功している
4. 旧 key が AWS 側で無効化済みである
5. 削除実施者、実施日時、確認結果を Notion に記録する

access key 前提の手順書やメモが残っていないかは、継続して通常のドキュメント棚卸し対象として扱います。

## 確認コマンド

Cloud Run env の旧名確認:

```powershell
$envRows = @(gcloud.cmd run services describe report-generator `
  --region=asia-northeast1 `
  --project=ice-sh `
  --format='json(spec.template.spec.containers[0].env)' | ConvertFrom-Json)

$envVars = @($envRows.spec.template.spec.containers[0].env)
$legacyNames = @(
  'AWS_SES_ACCESS_KEY_ID',
  'AWS_SES_SECRET_ACCESS_KEY',
  'MAIL_FROM_EMAIL',
  'MAIL_FROM_NAME',
  'MAIL_PROVIDER_SES_REGION',
  'MAIL_PROVIDER_SES_CONFIGURATION_SET',
  'MAIL_PROVIDER_TIMEOUT_SECONDS'
)

$envVars | Where-Object { $legacyNames -contains $_.name } | Select-Object name,value,valueFrom
```

Secret Manager の legacy secret 確認:

```powershell
$legacySecretNames = @(
  'aws-ses-access-key-id',
  'aws-ses-secret-access-key',
  'AWS_SES_ACCESS_KEY_ID',
  'AWS_SES_SECRET_ACCESS_KEY'
)

gcloud.cmd secrets list --project=ice-sh --format='value(name)' |
  Where-Object { $legacySecretNames -contains $_ }
```
