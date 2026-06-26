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
    [int]$RecentFailureLimit = 10,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$gcloudInfo = Get-Command "gcloud.cmd" -ErrorAction SilentlyContinue
if (-not $gcloudInfo) {
    $gcloudInfo = Get-Command "gcloud" -ErrorAction SilentlyContinue
}
if (-not $gcloudInfo) {
    throw "gcloud command was not found."
}
$GcloudCommand = $gcloudInfo.Source

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

function Get-LogEntries {
    param(
        [string]$Filter,
        [int]$EntryLimit
    )

    if ($EntryLimit -le 0) {
        return @()
    }

    $json = & $GcloudCommand logging read $Filter `
        --project=$Project `
        --freshness=$Freshness `
        --limit=$EntryLimit `
        --format="json" 2>$null
    $jsonText = $json | Out-String
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($jsonText)) {
        return $null
    }

    return @($jsonText | ConvertFrom-Json)
}

function Convert-AuditPayloadToSafeRow {
    param(
        [string]$Timestamp,
        [string]$TextPayload
    )

    $action = ""
    $result = ""
    $targetType = ""
    $targetId = ""
    $statusCode = ""
    $reason = ""

    if ($TextPayload -match 'action=([^\s]+)') { $action = $Matches[1] }
    if ($TextPayload -match 'result=([^\s]+)') { $result = $Matches[1] }
    if ($TextPayload -match 'target_type=([^\s]*)') { $targetType = $Matches[1] }
    if ($TextPayload -match 'target_id=([^\s]*)') { $targetId = $Matches[1] }
    if ($TextPayload -match 'status_code=([^\s]*)') { $statusCode = $Matches[1] }
    if ($TextPayload -match 'reason=(.*)$') { $reason = $Matches[1].Trim() }

    return [pscustomobject]@{
        timestamp = $Timestamp
        action = $action
        result = $result
        targetType = $targetType
        targetId = $targetId
        statusCode = $statusCode
        reason = $reason
    }
}

function Convert-AuthFailurePayloadToSafeRow {
    param(
        [string]$Timestamp,
        [string]$TextPayload
    )

    $reason = ""
    if ($TextPayload -match 'reason=([^\s]+)') {
        $reason = $Matches[1]
    }

    return [pscustomobject]@{
        timestamp = $Timestamp
        eventType = "admin_auth_failed"
        reason = $reason
    }
}

$generatedAt = (Get-Date).ToUniversalTime().ToString("o")
$serviceFilter = Join-ServiceFilter $Services
$baseFilter = 'resource.type="cloud_run_revision" AND ' + $serviceFilter + ' AND textPayload:"ICE_REPORT_ADMIN_AUDIT"'
$authFailureFilter = 'resource.type="cloud_run_revision" AND ' + $serviceFilter + ' AND textPayload:"ICE_REPORT_SECURITY_EVENT" AND textPayload:"admin_auth_failed"'

$rows = @()
foreach ($action in $Actions) {
    $actionFilter = $baseFilter + ' AND textPayload:"' + $action + '"'
    $rows += [pscustomobject]@{
        action = $action
        total = Get-LogCount $actionFilter
        success = Get-LogCount ($actionFilter + ' AND textPayload:"result" AND textPayload:"success"')
        failure = Get-LogCount ($actionFilter + ' AND textPayload:"result" AND textPayload:"failure"')
    }
}

$totalAuditLogs = Get-LogCount $baseFilter
$adminAuthFailures = Get-LogCount $authFailureFilter
$recentFailureFilter = $baseFilter + ' AND textPayload:"result" AND textPayload:"failure"'
$recentFailureEntries = Get-LogEntries -Filter $recentFailureFilter -EntryLimit $RecentFailureLimit
$recentAuthFailureEntries = Get-LogEntries -Filter $authFailureFilter -EntryLimit $RecentFailureLimit
$recentAuditFailures = if ($null -eq $recentFailureEntries) {
    $null
} else {
    @($recentFailureEntries | ForEach-Object {
        Convert-AuditPayloadToSafeRow -Timestamp $_.timestamp -TextPayload $_.textPayload
    })
}
$recentAuthFailures = if ($null -eq $recentAuthFailureEntries) {
    $null
} else {
    @($recentAuthFailureEntries | ForEach-Object {
        Convert-AuthFailurePayloadToSafeRow -Timestamp $_.timestamp -TextPayload $_.textPayload
    })
}
$querySucceeded = (
    ($null -ne $totalAuditLogs) -and
    ($null -ne $adminAuthFailures) -and
    ($null -ne $recentAuditFailures) -and
    ($null -ne $recentAuthFailures) -and
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
    "Recent audit failures:"
)
if (@($recentAuditFailures).Count -eq 0) {
    $summaryLines += "- none"
} else {
    foreach ($entry in $recentAuditFailures) {
        $summaryLines += "- $($entry.timestamp) action=$($entry.action) target=$($entry.targetType)/$($entry.targetId) status=$($entry.statusCode) reason=$($entry.reason)"
    }
}

$summaryLines += @(
    "",
    "Recent admin auth failure security events:"
)
if (@($recentAuthFailures).Count -eq 0) {
    $summaryLines += "- none"
} else {
    foreach ($entry in $recentAuthFailures) {
        $summaryLines += "- $($entry.timestamp) reason=$($entry.reason)"
    }
}

$summaryLines += @(
    "",
    "Search filters:",
    "- all audit logs: $baseFilter",
    "- audit failures: $recentFailureFilter",
    "- admin auth failures: $authFailureFilter",
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
    recentFailureLimit = $RecentFailureLimit
    recentAuditFailures = $recentAuditFailures
    recentAuthFailures = $recentAuthFailures
    searchFilters = [pscustomobject]@{
        allAuditLogs = $baseFilter
        auditFailures = $recentFailureFilter
        adminAuthFailures = $authFailureFilter
    }
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
