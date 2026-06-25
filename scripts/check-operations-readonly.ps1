param(
    [string]$BaseUrl = "https://report-generator-635067190197.asia-northeast1.run.app",
    [string]$Project = "ice-sh",
    [string]$Region = "asia-northeast1",
    [string]$Service = "report-generator",
    [string]$AdminSecret = "report-generator-admin-api-key",
    [int]$DeliveryLimit = 5,
    [int]$DownloadLogLimit = 5,
    [string]$Freshness = "30m",
    [string]$UptimeCheck = "projects/ice-sh/uptimeCheckConfigs/ice-report-generator-api-health-uptime-8PiDl_nq6TM",
    [string]$UptimeAlertPolicy = "projects/ice-sh/alertPolicies/17507730695585410786",
    [switch]$CaptureScreenshots,
    [string]$ScreenshotOutDir = "artifacts",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

function Invoke-HttpCheck {
    param(
        [string]$Uri,
        [hashtable]$Headers = @{},
        [string]$Method = "Get"
    )

    try {
        $params = @{
            Uri = $Uri
            Method = $Method
            UseBasicParsing = $true
            TimeoutSec = 60
        }
        if ($Headers.Count -gt 0) {
            $params.Headers = $Headers
        }

        $response = Invoke-WebRequest @params
        return [pscustomobject]@{
            statusCode = [int]$response.StatusCode
            contentLength = [int]$response.RawContentLength
            content = [string]$response.Content
            error = $null
        }
    } catch {
        $statusCode = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode.value__
        }
        return [pscustomobject]@{
            statusCode = $statusCode
            contentLength = $null
            content = ""
            error = $_.Exception.Message
        }
    }
}

function Convert-JsonOrNull {
    param([string]$Content)

    if ([string]::IsNullOrWhiteSpace($Content)) {
        return $null
    }
    try {
        return ($Content | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-ArrayPropertyCount {
    param(
        $Object,
        [string[]]$Names
    )

    if ($null -eq $Object) {
        return 0
    }
    foreach ($name in $Names) {
        $property = $Object.PSObject.Properties[$name]
        if ($property -and $null -ne $property.Value) {
            return @($property.Value).Count
        }
    }
    return 0
}

function Invoke-GcloudJson {
    param([scriptblock]$Command)

    $text = & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud command failed."
    }
    if ([string]::IsNullOrWhiteSpace(($text | Out-String))) {
        return $null
    }
    return ($text | ConvertFrom-Json)
}

function Get-LogCount {
    param(
        [string]$Filter,
        [int]$Limit = 100
    )

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

$workspace = (Resolve-Path ".").Path
$base = $BaseUrl.TrimEnd("/")
$generatedAt = (Get-Date).ToUniversalTime().ToString("o")
$GcloudCommand = if (Get-Command gcloud.cmd -ErrorAction SilentlyContinue) {
    "gcloud.cmd"
} else {
    "gcloud"
}

$adminKey = (& $GcloudCommand secrets versions access latest --secret=$AdminSecret --project=$Project 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($adminKey)) {
    throw "Failed to read admin key from Secret Manager."
}
$adminKey = $adminKey.Trim()

$serviceState = Invoke-GcloudJson {
    & $GcloudCommand run services describe $Service `
        --project=$Project `
        --region=$Region `
        --format=json
}

$latestRevision = $serviceState.status.latestReadyRevisionName
$image = $serviceState.spec.template.spec.containers[0].image
$traffic = @($serviceState.status.traffic | ForEach-Object {
    [pscustomobject]@{
        revisionName = $_.revisionName
        percent = $_.percent
        latestRevision = $_.latestRevision
    }
})

$headers = @{ "X-Admin-Key" = $adminKey }
$wrongHeaders = @{ "X-Admin-Key" = "wrong-admin-key-for-readonly-check" }

$health = Invoke-HttpCheck -Uri "$base/api-health"
$adminPage = Invoke-HttpCheck -Uri "$base/admin"
$deliveriesNoAuth = Invoke-HttpCheck -Uri "$base/deliveries?limit=1"
$deliveriesWrongKey = Invoke-HttpCheck -Uri "$base/deliveries?limit=1" -Headers $wrongHeaders
$deliveries = Invoke-HttpCheck -Uri "$base/deliveries?limit=$DeliveryLimit" -Headers $headers
$downloadLogs = Invoke-HttpCheck -Uri "$base/download-logs?limit=$DownloadLogLimit" -Headers $headers

$deliveriesJson = Convert-JsonOrNull $deliveries.content
$downloadLogsJson = Convert-JsonOrNull $downloadLogs.content
$deliveryCount = Get-ArrayPropertyCount $deliveriesJson @("items", "deliveries")
$downloadLogCount = Get-ArrayPropertyCount $downloadLogsJson @("items", "logs")

$revisionFilter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="' + $Service + '"'
if ($latestRevision) {
    $revisionFilter += ' AND resource.labels.revision_name="' + $latestRevision + '"'
}

$runtimeErrorCount = Get-LogCount ($revisionFilter + ' AND severity>=ERROR')
$adminAuditCount = Get-LogCount ($revisionFilter + ' AND textPayload:ICE_REPORT_ADMIN_AUDIT')
$adminAuthFailureCount = Get-LogCount ($revisionFilter + ' AND textPayload:ICE_REPORT_SECURITY_EVENT AND textPayload:admin_auth_failed')
$mailAttemptCount = Get-LogCount ($revisionFilter + ' AND textPayload:ICE_REPORT_MAIL_DELIVERY_ATTEMPT')
$otpSentCount = Get-LogCount ($revisionFilter + ' AND textPayload:ICE_REPORT_OTP_DELIVERY_SENT')

$uptimeConfig = $null
if (-not [string]::IsNullOrWhiteSpace($UptimeCheck)) {
    $uptimeConfig = Invoke-GcloudJson {
        & $GcloudCommand monitoring uptime describe $UptimeCheck `
            --project=$Project `
            --format=json
    }
}

$uptimePolicy = $null
if (-not [string]::IsNullOrWhiteSpace($UptimeAlertPolicy)) {
    $uptimePolicy = Invoke-GcloudJson {
        & $GcloudCommand monitoring policies describe $UptimeAlertPolicy `
            --project=$Project `
            --format=json
    }
}

$screenshotMetrics = @()
if ($CaptureScreenshots) {
    $captureScript = Join-Path (Join-Path $workspace "scripts") "capture-admin-deliveries.ps1"
    $screenshotMetrics = @(
        & $captureScript `
            -BaseUrl $base `
            -Project $Project `
            -AdminSecret $AdminSecret `
            -OutDir $ScreenshotOutDir
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Screenshot capture failed."
    }
}

$checks = [ordered]@{
    apiHealth200 = ($health.statusCode -eq 200)
    apiHealthBodyOk = ($health.content -like "*ok*")
    adminPage200 = ($adminPage.statusCode -eq 200)
    deliveriesNoAuth401 = ($deliveriesNoAuth.statusCode -eq 401)
    deliveriesWrongKey401 = ($deliveriesWrongKey.statusCode -eq 401)
    deliveriesAdmin200 = ($deliveries.statusCode -eq 200)
    downloadLogsAdmin200 = ($downloadLogs.statusCode -eq 200)
    cloudRunHasReadyRevision = -not [string]::IsNullOrWhiteSpace($latestRevision)
    cloudRunTraffic100 = (@($traffic | Where-Object { $_.percent -eq 100 }).Count -ge 1)
    noRuntimeErrors = ($runtimeErrorCount -eq 0)
}

$failedChecks = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
$passed = ($failedChecks.Count -eq 0)

$notionSummary = @"
ICE Report Generator read-only operational check

Generated at: $generatedAt
Base URL: $base
Latest ready revision: $latestRevision
Image: $image
Overall: $(if ($passed) { "PASS" } else { "FAIL" })

HTTP:
- /api-health: $($health.statusCode)
- /admin: $($adminPage.statusCode)
- /deliveries no auth: $($deliveriesNoAuth.statusCode)
- /deliveries wrong key: $($deliveriesWrongKey.statusCode)
- /deliveries admin: $($deliveries.statusCode), count=$deliveryCount
- /download-logs admin: $($downloadLogs.statusCode), count=$downloadLogCount

Logs ($Freshness):
- runtime ERROR: $runtimeErrorCount
- admin audit: $adminAuditCount
- admin auth failures: $adminAuthFailureCount
- mail attempts: $mailAttemptCount
- OTP sent: $otpSentCount

Monitoring:
- uptime check: $UptimeCheck
- uptime alert policy: $UptimeAlertPolicy

Failed checks: $(if ($failedChecks.Count -eq 0) { "none" } else { $failedChecks -join ", " })
"@

$adminKey = $null

$result = [pscustomobject]@{
    generatedAt = $generatedAt
    project = $Project
    region = $Region
    service = $Service
    baseUrl = $base
    latestReadyRevision = $latestRevision
    image = $image
    traffic = $traffic
    endpointStatus = [pscustomobject]@{
        apiHealth = $health.statusCode
        adminPage = $adminPage.statusCode
        deliveriesNoAuth = $deliveriesNoAuth.statusCode
        deliveriesWrongKey = $deliveriesWrongKey.statusCode
        deliveriesAdmin = $deliveries.statusCode
        downloadLogsAdmin = $downloadLogs.statusCode
    }
    counts = [pscustomobject]@{
        deliveriesReturned = $deliveryCount
        downloadLogsReturned = $downloadLogCount
        runtimeErrors = $runtimeErrorCount
        adminAuditLogs = $adminAuditCount
        adminAuthFailures = $adminAuthFailureCount
        mailAttempts = $mailAttemptCount
        otpDeliverySent = $otpSentCount
    }
    monitoring = [pscustomobject]@{
        uptimeCheck = if ($uptimeConfig) {
            [pscustomobject]@{
                name = $uptimeConfig.name
                displayName = $uptimeConfig.displayName
                period = $uptimeConfig.period
                timeout = $uptimeConfig.timeout
            }
        } else { $null }
        uptimeAlertPolicy = if ($uptimePolicy) {
            [pscustomobject]@{
                name = $uptimePolicy.name
                displayName = $uptimePolicy.displayName
                enabled = $uptimePolicy.enabled
            }
        } else { $null }
    }
    screenshots = $screenshotMetrics
    checks = $checks
    failedChecks = $failedChecks
    passed = $passed
    notionSummary = $notionSummary
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
} else {
    $result
}

if (-not $passed) {
    exit 1
}
