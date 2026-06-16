# Deploy

Cloud Run `report-generator` のリリース手順です。運用確認や障害時手順は `docs/operations.md`、SES 切替の詳細は `docs/ses-cutover-checklist.md` を参照します。

## 前提

- deploy 対象 branch: `main`
- GCP project: `ice-sh`
- Region: `asia-northeast1`
- Runtime service account: `ice-report-runner@ice-sh.iam.gserviceaccount.com`
- Deploy impersonation service account: `ice-deployer@ice-sh.iam.gserviceaccount.com`
- Image repository: `asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator`

## 1. 事前確認

```powershell
git switch main
git fetch origin
git status --short --branch
git log -1 --oneline
```

確認ポイント:

- `main` が最新の deploy 対象 commit を指している
- 作業ツリーが clean
- deploy 対象 commit SHA を控えている

## 2. Build

Docker build は必ず `--no-cache` を使います。tag は commit SHA にします。

```powershell
$sha = (git rev-parse HEAD).Trim()
$image = "asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:$sha"
docker build --no-cache -t $image .
```

## 3. Push

```powershell
docker push $image
```

push 後に digest が出るため、リリース記録へ残します。

## 4. Deploy

```powershell
gcloud.cmd run deploy report-generator `
  --image $image `
  --region asia-northeast1 `
  --project ice-sh `
  --memory 2Gi `
  --service-account ice-report-runner@ice-sh.iam.gserviceaccount.com `
  --allow-unauthenticated `
  --impersonate-service-account=ice-deployer@ice-sh.iam.gserviceaccount.com `
  --quiet
```

deploy 結果に表示される revision 名を控えます。

## 5. 反映確認

```powershell
gcloud.cmd run services describe report-generator `
  --region=asia-northeast1 `
  --project=ice-sh `
  --format='value(spec.template.spec.containers[0].image,status.latestReadyRevisionName,status.traffic[0].percent)'
```

確認ポイント:

- image tag が deploy 対象 commit SHA
- latest ready revision が想定の revision
- traffic が `100`

## 6. Smoke Test

基本確認:

```powershell
$base = 'https://report-generator-635067190197.asia-northeast1.run.app'
$health = Invoke-WebRequest -Uri "$base/api-health" -UseBasicParsing
$admin = Invoke-WebRequest -Uri "$base/admin" -UseBasicParsing
[pscustomobject]@{
  healthStatus = $health.StatusCode
  healthContent = $health.Content
  adminStatus = $admin.StatusCode
  adminLength = $admin.RawContentLength
}
```

必要に応じて `docs/operations.md` の OTP smoke test を 1 回だけ実行します。実メールが送信されるため、対象 delivery と宛先を事前に確認してください。

## 7. Cloud Logging 確認

```powershell
$revision = '<deployed-revision>'
$filter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="report-generator" AND resource.labels.revision_name="' + $revision + '" AND (textPayload:"ICE_REPORT_MAIL_DELIVERY_ATTEMPT" OR textPayload:"ICE_REPORT_OTP_DELIVERY_SENT" OR textPayload:"mail_provider_auth_failed")'
gcloud.cmd logging read $filter `
  --project=ice-sh `
  --freshness=30m `
  --limit=50 `
  --format='value(timestamp,textPayload)'
```

確認ポイント:

- OTP smoke 実施時に `ICE_REPORT_MAIL_DELIVERY_ATTEMPT` が出る
- OTP smoke 実施時に `ICE_REPORT_OTP_DELIVERY_SENT` が出る
- `mail_provider_auth_failed` が増えていない
- 生 PIN、生メールアドレス、生 token がログへ出ていない

## 8. Rollback

直前の正常 revision へ戻す場合:

```powershell
gcloud.cmd run services update-traffic report-generator `
  --project=ice-sh `
  --region=asia-northeast1 `
  --to-revisions=<stable-revision>=100 `
  --impersonate-service-account=ice-deployer@ice-sh.iam.gserviceaccount.com `
  --quiet
```

rollback 後は `/api-health`、`/admin`、Cloud Logging、必要最小限の OTP smoke を確認します。

## Admin専用service + IAP 移行時

通常の `report-generator` はpublic download、OTP、healthを含む利用者向けserviceとして維持します。人間向けAdmin UIをIAP化する場合は、別service `report-generator-admin` を作成して影響範囲を分離します。

前提:

- Cloud Run direct IAPを使う
- IAP APIが有効
- Admin専用serviceは `--no-allow-unauthenticated` と `--iap` でdeployする
- IAP service agent `service-<PROJECT_NUMBER>@gcp-sa-iap.iam.gserviceaccount.com` に `roles/run.invoker` を付与する
- 管理者user/groupに `roles/iap.httpsResourceAccessor` を付与する
- public service全体へIAPを直接適用しない

検証用deploy例:

```powershell
$sha = (git rev-parse HEAD).Trim()
$image = "asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:$sha"

gcloud.cmd run deploy report-generator-admin `
  --image $image `
  --region asia-northeast1 `
  --project ice-sh `
  --memory 2Gi `
  --service-account ice-report-runner@ice-sh.iam.gserviceaccount.com `
  --set-env-vars ADMIN_IAP_AUTH_ENABLED=1,ADMIN_IAP_ALLOWED_EMAILS=sinohara@impress.co.jp,ADMIN_IAP_SERVICE_NAME=report-generator-admin `
  --no-allow-unauthenticated `
  --iap `
  --impersonate-service-account=ice-deployer@ice-sh.iam.gserviceaccount.com `
  --quiet
```

IAP service agentのInvoker付与例:

```powershell
$projectNumber = gcloud.cmd projects describe ice-sh --format='value(projectNumber)'
gcloud.cmd run services add-iam-policy-binding report-generator-admin `
  --project=ice-sh `
  --region=asia-northeast1 `
  --member="serviceAccount:service-$projectNumber@gcp-sa-iap.iam.gserviceaccount.com" `
  --role=roles/run.invoker
```

smoke:

- 許可済みuserで Admin UI に到達できる
- 未許可userまたは未ログインでは Admin UI に到達できない
- public service の `/api-health` が `200`
- public service の `/d/*` と OTP flow がIAPの影響を受けない
- `X-Admin-Key` を使うscript/API経路が必要範囲で継続する

rollback:

Admin dedicated service cutover / rollback quick reference:

- Keep public service `report-generator` untouched for Admin-only cutover and
  rollback.
- Before cutover, run `scripts\check-admin-iap-readonly.ps1` and complete
  allowed-user browser smoke on `report-generator-admin`.
- For traffic rollback, use:

```powershell
gcloud.cmd run services update-traffic report-generator-admin `
  --project ice-sh `
  --region asia-northeast1 `
  --to-revisions=<PREVIOUS_GOOD_REVISION>=100
```

- For IAP policy rollback, restore
  `user:sinohara@impress.co.jp` with `roles/iap.httpsResourceAccessor` on
  `report-generator-admin`, then remove the faulty group binding if needed.
- Do not grant `roles/run.invoker` directly to users or groups. Keep Cloud Run
  invoker limited to the IAP service agent.
- Full procedure: `docs/operations.md` section
  `Admin dedicated service cutover / rollback`.

- `report-generator-admin` の直前正常revisionへtrafficを戻す
- IAP policy変更が原因なら、Admin専用serviceのIAP policyだけを戻す
- public service `report-generator` へIAP設定を広げていないことを確認する
