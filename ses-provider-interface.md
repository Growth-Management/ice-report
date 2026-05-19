# SES Provider Interface

## 目的

ICE Report Generator の OTP / PIN 送信を、呼び出し側から provider 固有事情を切り離した構造へ寄せる。

## 追加ファイル

- `mail_provider.py`
- `otp_mail.py`
- `mail_runtime.py`

## 責務分離

- `app.py`
  - PIN 発行、認可、rate limit、監査イベント記録
  - provider 名や送信先、テンプレートに必要な最小情報だけを service 層へ渡す
- `otp_mail.py`
  - OTP/PIN 用の subject / text / html を組み立てる
- `mail_provider.py`
  - provider 共通の request / result / error を持つ
  - `logging` / `noop` / `ses` の差分を吸収する
- `mail_runtime.py`
  - `MAIL_PROVIDER` を見て runtime provider を解決する
  - SES を選んだ場合だけ boto3 client を lazy に生成する

## app.py 差し込み位置

`_issue_pin()` で challenge を Firestore に保存した直後に、次の順で送信する。

1. `build_otp_pin_mail()` でメール本文を生成
2. `build_runtime_mail_provider()` または `send_otp_pin_email()` で provider を解決
3. `provider.send()` を実行
4. 成功時は provider 名と message id を Cloud Logging / `security_events` に記録
5. 失敗時は詳細を内部ログに残し、UI には一般化した送信失敗メッセージだけを返す

## 推奨環境変数

- `MAIL_PROVIDER`
  - `logging` / `noop` / `ses`
- `MAIL_FROM_EMAIL`
- `MAIL_REPLY_TO_EMAILS`
  - カンマ区切り
- `MAIL_PROVIDER_SES_CONFIGURATION_SET`
- `MAIL_PROVIDER_SES_REGION`
- `MAIL_PROVIDER_TIMEOUT_SECONDS`

## エラー方針

- 呼び出し側へは `MailDeliveryError.safe_reason` だけを渡す
- provider 固有例外や AWS 応答は内部ログに閉じ込める
- OTP/PIN は認証インフラなので、送信失敗時は challenge 作成だけ成功させて放置しない

## 次段階

- `develop` の `app.py` に provider 呼び出しと送信失敗ハンドリングを反映する
- 送信成功 / 送信失敗を `security_events` と運用通知へ接続する
- 本番用 Secret 名と Cloud Run 環境変数を固定する
