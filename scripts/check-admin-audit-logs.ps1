param(
    [string]$Project = "ice-sh",
    [string[]]$Services = @("report-generator", "report-generator-admin"),
    [string]$Freshness = "24h",
    [int]$Limit = 1000,
    [string[]]$Actions = @(
        "admin_auth",
        "generate_report",
        "delivery_create",
        "delivery_version_add",
        "delivery_disable",
        "delivery_enable",
        "cleanup_expired_deliveries"
    ),
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$GcloudCommand = if (Get-Command gcloud.cmd -ErrorAction SilentlyContinue) {
    "gcloud.cmd"
} else {
    "gcloud"
}

function Join-ServiceFilter {
    param([string[]]$ServiceNames)

    $parts = @($ServiceNames | ForEach-Object {
        'resource.labels.service_name="' + $_ + '"'
    })
    if ($parts.Count -eq 1) {
        return $parts[0]
    }
    return "(" + ($parts -join " OR ") + ")"
}

function Get-LogCount {
    param([string]$Filter)

    $lines = @(
        & $GcloudCommand logging read $Filter `
            --project=$Project `
            --freshness=$Freshness `
            --limit=$Limit `
            --format="value(timestamp)" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    return @($lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
}

$generatedAt = (Get-Date).ToUniversalTime().ToString("o")
$serviceFilter = Join-ServiceFilter $Services
$baseFilter = 'resource.type="cloud_run_revision" AND ' + $serviceFilter + ' AND textPayload:"ICE_REPORT_ADMIN_AUDIT"'
$authFailureFilter = 'resource.type="cloud_run_revision" AND ' + $serviceFilter + ' AND textPayload:"ICE_REPORT_SECURITY_EVENT" AND textPayload:"admin_auth_failed"'

$rows = @()
foreach ($action in $Actions) {
    $actionFilter = $baseFilter + ' AND textPayload:"action=' + $action + '"'
    $rows += [pscustomobject]@{
        action = $action
        total = Get-LogCount $actionFilter
        success = Get-LogCount ($actionFilter + ' AND textPayload:"result=success"')
        failure = Get-LogCount ($actionFilter + ' AND textPayload:"result=failure"')
    }
}

$totalAuditLogs = Get-LogCount $baseFilter
$adminAuthFailures = Get-LogCount $authFailureFilter
$querySucceeded = (
    ($null -ne $totalAuditLogs) -and
    ($null -ne $adminAuthFailures) -and
    (@($rows | Where-Object {
        ($null -eq $_.total) -or ($null -eq $_.success) -or ($null -eq $_.failure)
    }).Count -eq 0)
)

$summaryLines = @(
    "ICE Report Generator admin audit log review",
    "",
    "Generated at: $generatedAt",
    "Project: $Project",
    "Services: $($Services -join ', ')",
    "Freshness: $Freshness",
    "Overall query: $(if ($querySucceeded) { 'PASS' } else { 'FAIL' })",
    "",
    "Counts:",
    "- ICE_REPORT_ADMIN_AUDIT total: $totalAuditLogs",
    "- admin_auth_failed security events: $adminAuthFailures",
    ""
)

foreach ($row in $rows) {
    $summaryLines += "- $($row.action): total=$($row.total), success=$($row.success), failure=$($row.failure)"
}

$summaryLines += @(
    "",
    "Transfer rule: paste counts and investigation notes only. Do not paste Admin key fingerprint, credential, PIN, token, raw recipient email, message body, provider event JSON, IP address, or user agent."
)

$result = [pscustomobject]@{
    generatedAt = $generatedAt
    project = $Project
    services = $Services
    freshness = $Freshness
    limit = $Limit
    totalAuditLogs = $totalAuditLogs
    adminAuthFailures = $adminAuthFailures
    actions = $rows
    querySucceeded = $querySucceeded
    notionSummary = ($summaryLines -join [Environment]::NewLine)
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 10
} else {
    $result
}

if (-not $querySucceeded) {
    exit 1
}
