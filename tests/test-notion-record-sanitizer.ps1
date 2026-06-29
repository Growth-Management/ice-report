param()

$ErrorActionPreference = "Stop"

$workspace = Resolve-Path (Join-Path $PSScriptRoot "..")
$scriptPath = Join-Path $workspace "scripts\run-operations-readonly-scheduled.ps1"
$scriptText = Get-Content -LiteralPath $scriptPath -Raw -Encoding UTF8
$prefix = ($scriptText -split '(?m)^\$workspace = ')[0]

Invoke-Expression $prefix

$sample = @"
allowed user: sinohara@impress.co.jp
Authorization: Bearer abc.def.ghi
X-Admin-Key: secret-admin-key
webhook: https://hooks.slack.com/services/T000/B000/SECRET
access: AKIA1234567890ABCDEF
signed url: https://example.com/d/token?X-Goog-Signature=abc
"@

$redacted = Protect-NotionRecordText -Text $sample
$violations = @(Test-NotionRecordSafety -Text $redacted)

if ($redacted -match "sinohara@impress.co.jp") {
    throw "raw email was not redacted"
}
if ($redacted -match "hooks\.slack\.com") {
    throw "Slack webhook URL was not redacted"
}
if ($redacted -match "AKIA1234567890ABCDEF") {
    throw "AWS access key was not redacted"
}
if ($redacted -match "abc\.def\.ghi") {
    throw "bearer token was not redacted"
}
if ($violations.Count -ne 0) {
    throw "sanitized Notion text still has violations: $($violations -join ', ')"
}

[pscustomobject]@{
    passed = $true
    checked = @(
        "raw_email",
        "bearer_token",
        "x_admin_key",
        "slack_webhook",
        "aws_access_key",
        "signed_url"
    )
}
