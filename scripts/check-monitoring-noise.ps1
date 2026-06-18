param(
    [string]$Project = "ice-sh",
    [string]$Service = "report-generator",
    [string]$Freshness = "7d",
    [int]$Limit = 1000,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

function Get-LogCount {
    param([string]$Filter)

    $lines = @(
        & gcloud.cmd logging read $Filter `
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
$baseFilter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="' + $Service + '"'

$signals = @(
    [pscustomobject]@{
        name = "runtime_errors"
        severity = "critical"
        filter = $baseFilter + ' AND severity>=ERROR'
        currentDecision = "Keep threshold at one or more events in a 5-minute window."
    },
    [pscustomobject]@{
        name = "otp_delivery_failed"
        severity = "critical"
        filter = $baseFilter + ' AND textPayload:"ICE_REPORT_SECURITY_EVENT type=otp_delivery_failed"'
        currentDecision = "Keep threshold at one or more events in a 5-minute window."
    },
    [pscustomobject]@{
        name = "mail_delivery_failure"
        severity = "critical"
        filter = $baseFilter + ' AND textPayload:"ICE_REPORT_MAIL_DELIVERY_ATTEMPT" AND textPayload:"result=failure"'
        currentDecision = "Keep threshold at one or more events in a 5-minute window."
    },
    [pscustomobject]@{
        name = "mail_provider_auth_failed"
        severity = "critical"
        filter = $baseFilter + ' AND (textPayload:"mail_provider_auth_failed" OR textPayload:"safe_reason=mail_provider_auth_failed")'
        currentDecision = "Keep threshold at one or more events in a 5-minute window."
    },
    [pscustomobject]@{
        name = "admin_auth_failed"
        severity = "warning"
        filter = $baseFilter + ' AND textPayload:"ICE_REPORT_SECURITY_EVENT" AND textPayload:"admin_auth_failed"'
        currentDecision = "Review counts only. Do not alert until repeated unexpected failures are observed."
    }
)

$rows = @()
foreach ($signal in $signals) {
    $rows += [pscustomobject]@{
        name = $signal.name
        severity = $signal.severity
        count = Get-LogCount $signal.filter
        currentDecision = $signal.currentDecision
    }
}

$querySucceeded = (@($rows | Where-Object { $null -eq $_.count }).Count -eq 0)
$criticalCount = (@($rows | Where-Object { $_.severity -eq "critical" } | Measure-Object -Property count -Sum).Sum)
$warningCount = (@($rows | Where-Object { $_.severity -eq "warning" } | Measure-Object -Property count -Sum).Sum)

$summaryLines = @(
    "ICE Report Generator monitoring noise review",
    "",
    "Generated at: $generatedAt",
    "Project: $Project",
    "Service: $Service",
    "Freshness: $Freshness",
    "Overall query: $(if ($querySucceeded) { 'PASS' } else { 'FAIL' })",
    "",
    "Counts:",
    "- critical signals total: $criticalCount",
    "- warning signals total: $warningCount"
)

foreach ($row in $rows) {
    $summaryLines += "- $($row.name): severity=$($row.severity), count=$($row.count), decision=$($row.currentDecision)"
}

$summaryLines += @(
    "",
    "Decision rule: do not change alert thresholds from counts alone. Review affected users, incident notes, deploy history, and whether the signal represented real user impact."
)

$result = [pscustomobject]@{
    generatedAt = $generatedAt
    project = $Project
    service = $Service
    freshness = $Freshness
    limit = $Limit
    querySucceeded = $querySucceeded
    criticalSignalsTotal = $criticalCount
    warningSignalsTotal = $warningCount
    signals = $rows
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
