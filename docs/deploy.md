
---

# docs/deploy.md

かなり重要。

```md
# Deploy

## Build

```bash
docker build --no-cache -t asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:latest .

```Push
docker push asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:latest

```Deploy
gcloud run deploy report-generator --image asia-northeast1-docker.pkg.dev/ice-sh/ice-report/report-generator:latest --region asia-northeast1 --memory 2Gi --service-account ice-report-runner@ice-sh.iam.gserviceaccount.com --env-vars-file env.yaml --allow-unauthenticated --impersonate-service-account=ice-deployer@ice-sh.iam.gserviceaccount.com