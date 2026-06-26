param(
    [string]$OutDir = "artifacts/operations-readonly",
    [switch]$CaptureScreenshots,
    [switch]$SkipAdminAuditReview,
    [switch]$SkipAdminIapReview,
    [switch]$SkipDocLegacyReview,
    [switch]$SkipMonitoringReview,
    [switch]$SkipRepoHygieneReview,
    [string[]]$ExpectedIapUsers = @("sinohara@impress.co.jp"),
    [switch]$RecordToNotion,
    [string]$NotionPageId = $env:NOTION_READONLY_CHECK_PAGE_ID,
    [string]$NotionTokenEnv = "NOTION_API_TOKEN",
    [string]$NotionTokenSecret = $env:NOTION_API_TOKEN_SECRET_NAME,
    [string]$NotionTokenSecretProject = "ice-sh",
    [string]$NotionVersion = "2026-03-11"
)

$ErrorActionPreference = "Stop"

function Split-TextChunk {
    param(
        [string]$Text,
        [int]$MaxLength = 1800
    )

    if ([string]::IsNullOrEmpty($Text)) {
        return @("")
    }

    $chunks = @()
    $remaining = $Text
    while ($remaining.Length -gt $MaxLength) {
        $splitAt = $remaining.LastIndexOf("`n", [Math]::Min($MaxLength, $remaining.Length - 1))
        if ($splitAt -le 0) {
            $splitAt = $MaxLength
        }
        $chunks += $remaining.Substring(0, $splitAt)
        $remaining = $remaining.Substring($splitAt).TrimStart()
    }
    if ($remaining.Length -gt 0) {
        $chunks += $remaining
    }
    return $chunks
}

function New-NotionRichText {
    param([string]$Content)

    return @{
        type = "text"
        text = @{
            content = $Content
        }
    }
}

function New-NotionParagraph {
    param([string]$Content)

    return @{
        object = "block"
        type = "paragraph"
        paragraph = @{
            rich_text = @((New-NotionRichText -Content $Content))
        }
    }
}

function Get-NotionApiToken {
    param(
        [string]$TokenEnv,
        [string]$TokenSecret,
        [string]$TokenSecretProject
    )

    if (-not [string]::IsNullOrWhiteSpace($TokenEnv)) {
        $envToken = [Environment]::GetEnvironmentVariable($TokenEnv)
        if (-not [string]::IsNullOrWhiteSpace($envToken)) {
            return $envToken.Trim()
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($TokenSecret)) {
        $gcloudCommand = if (Get-Command gcloud.cmd -ErrorAction SilentlyContinue) {
            "gcloud.cmd"
        } else {
            "gcloud"
        }
        $secretToken = (& $gcloudCommand secrets versions access latest --secret=$TokenSecret --project=$TokenSecretProject 2>$null)
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($secretToken)) {
            throw "Failed to read Notion API token from Secret Manager."
        }
        return $secretToken.Trim()
    }

    throw "Notion token is required. Set NOTION_API_TOKEN or pass -NotionTokenSecret."
}

function Write-NotionReadOnlySummary {
    param(
        [string]$PageId,
        [string]$Token,
        [string]$Version,
        $CheckResult,
        [string]$SummaryText,
        [string]$JsonPath,
        [string]$SummaryPath,
        [string]$RunMetadataPath = "",
        [string]$AuditJsonPath = "",
        [string]$AuditSummaryPath = "",
        [string]$AdminIapJsonPath = "",
        [string]$AdminIapSummaryPath = "",
        [string]$DocLegacyJsonPath = "",
        [string]$DocLegacySummaryPath = "",
        [string]$MonitoringJsonPath = "",
        [string]$MonitoringSummaryPath = "",
        [string]$RepoHygieneJsonPath = "",
        [string]$RepoHygieneSummaryPath = ""
    )

    if ([string]::IsNullOrWhiteSpace($PageId)) {
        throw "Notion page id is required. Set NOTION_READONLY_CHECK_PAGE_ID or pass -NotionPageId."
    }

    $title = "Read-only operational check: "
    if ($CheckResult.passed) {
        $title += "PASS"
    } else {
        $title += "FAIL"
    }
    $title += " ($($CheckResult.generatedAt))"

    $children = @(
        @{
            object = "block"
            type = "heading_2"
            heading_2 = @{
                rich_text = @((New-NotionRichText -Content $title))
            }
        }
    )

    foreach ($chunk in (Split-TextChunk -Text $SummaryText)) {
        $children += (New-NotionParagraph -Content $chunk)
    }

    $artifactSummary = "Local artifacts:`n- JSON: $JsonPath`n- Summary: $SummaryPath"
    if (-not [string]::IsNullOrWhiteSpace($RunMetadataPath)) {
        $artifactSummary += "`n- Run metadata: $RunMetadataPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($AuditJsonPath)) {
        $artifactSummary += "`n- Admin audit JSON: $AuditJsonPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($AuditSummaryPath)) {
        $artifactSummary += "`n- Admin audit summary: $AuditSummaryPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($AdminIapJsonPath)) {
        $artifactSummary += "`n- Admin IAP JSON: $AdminIapJsonPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($AdminIapSummaryPath)) {
        $artifactSummary += "`n- Admin IAP summary: $AdminIapSummaryPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($DocLegacyJsonPath)) {
        $artifactSummary += "`n- Docs legacy JSON: $DocLegacyJsonPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($DocLegacySummaryPath)) {
        $artifactSummary += "`n- Docs legacy summary: $DocLegacySummaryPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($MonitoringJsonPath)) {
        $artifactSummary += "`n- Monitoring noise JSON: $MonitoringJsonPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($MonitoringSummaryPath)) {
        $artifactSummary += "`n- Monitoring noise summary: $MonitoringSummaryPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($RepoHygieneJsonPath)) {
        $artifactSummary += "`n- Repo hygiene JSON: $RepoHygieneJsonPath"
    }
    if (-not [string]::IsNullOrWhiteSpace($RepoHygieneSummaryPath)) {
        $artifactSummary += "`n- Repo hygiene summary: $RepoHygieneSummaryPath"
    }
    $children += (New-NotionParagraph -Content $artifactSummary)

    $body = @{
        children = $children
        position = @{
            type = "end"
        }
    } | ConvertTo-Json -Depth 20

    $headers = @{
        Authorization = "Bearer $Token"
        "Notion-Version" = $Version
    }

    $response = Invoke-RestMethod `
        -Uri "https://api.notion.com/v1/blocks/$PageId/children" `
        -Method Patch `
        -Headers $headers `
        -ContentType "application/json" `
        -Body $body

    return [pscustomobject]@{
        pageId = $PageId
        notionVersion = $Version
        appendedBlockCount = @($response.results).Count
        hasMore = $response.has_more
    }
}

$workspace = (Resolve-Path ".").Path
$resolvedOutDir = Join-Path $workspace $OutDir
New-Item -ItemType Directory -Force -Path $resolvedOutDir | Out-Null
$PowerShellCommand = if (Get-Command pwsh -ErrorAction SilentlyContinue) {
    "pwsh"
} elseif (Get-Command powershell.exe -ErrorAction SilentlyContinue) {
    "powershell.exe"
} else {
    throw "PowerShell executable not found."
}

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$runStartedAt = (Get-Date).ToUniversalTime()
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$jsonPath = Join-Path $resolvedOutDir "operations-readonly-check-$timestamp.json"
$summaryPath = Join-Path $resolvedOutDir "operations-readonly-check-$timestamp-summary.txt"
$auditJsonPath = Join-Path $resolvedOutDir "admin-audit-log-review-$timestamp.json"
$auditSummaryPath = Join-Path $resolvedOutDir "admin-audit-log-review-$timestamp-summary.txt"
$adminIapJsonPath = Join-Path $resolvedOutDir "admin-iap-readonly-check-$timestamp.json"
$adminIapSummaryPath = Join-Path $resolvedOutDir "admin-iap-readonly-check-$timestamp-summary.txt"
$docLegacyJsonPath = Join-Path $resolvedOutDir "docs-legacy-reference-check-$timestamp.json"
$docLegacySummaryPath = Join-Path $resolvedOutDir "docs-legacy-reference-check-$timestamp-summary.txt"
$monitoringJsonPath = Join-Path $resolvedOutDir "monitoring-noise-review-$timestamp.json"
$monitoringSummaryPath = Join-Path $resolvedOutDir "monitoring-noise-review-$timestamp-summary.txt"
$repoHygieneJsonPath = Join-Path $resolvedOutDir "secret-exposure-metadata-$timestamp.json"
$repoHygieneSummaryPath = Join-Path $resolvedOutDir "secret-exposure-metadata-$timestamp-summary.txt"
$runMetadataPath = Join-Path $resolvedOutDir "operations-readonly-run-metadata-$timestamp.json"

$scriptsDir = Join-Path $workspace "scripts"
$checkScript = Join-Path $scriptsDir "check-operations-readonly.ps1"
$args = @(
    "-ExecutionPolicy", "Bypass",
    "-File", $checkScript,
    "-AsJson"
)
if ($CaptureScreenshots) {
    $args += "-CaptureScreenshots"
    $args += "-ScreenshotOutDir"
    $args += $resolvedOutDir
}

$jsonText = & $PowerShellCommand @args
$exitCode = $LASTEXITCODE

if (-not [string]::IsNullOrWhiteSpace(($jsonText | Out-String))) {
    $jsonText | Set-Content -Encoding UTF8 -LiteralPath $jsonPath
    $result = $jsonText | ConvertFrom-Json
    $result.notionSummary | Set-Content -Encoding UTF8 -LiteralPath $summaryPath
}

$auditResult = $null
$auditExitCode = $null
if (-not $SkipAdminAuditReview) {
    $auditScript = Join-Path $scriptsDir "check-admin-audit-logs.ps1"
    $auditArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $auditScript,
        "-AsJson"
    )
    $auditJsonText = & $PowerShellCommand @auditArgs
    $auditExitCode = $LASTEXITCODE
    if (-not [string]::IsNullOrWhiteSpace(($auditJsonText | Out-String))) {
        $auditJsonText | Set-Content -Encoding UTF8 -LiteralPath $auditJsonPath
        $auditResult = $auditJsonText | ConvertFrom-Json
        $auditResult.notionSummary | Set-Content -Encoding UTF8 -LiteralPath $auditSummaryPath
    }
}

$adminIapResult = $null
$adminIapExitCode = $null
if (-not $SkipAdminIapReview) {
    $adminIapScript = Join-Path $scriptsDir "check-admin-iap-readonly.ps1"
    $adminIapArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $adminIapScript,
        "-AsJson",
        "-ExpectedIapUsers"
    ) + @($ExpectedIapUsers)
    $adminIapJsonText = & $PowerShellCommand @adminIapArgs
    $adminIapExitCode = $LASTEXITCODE
    if (-not [string]::IsNullOrWhiteSpace(($adminIapJsonText | Out-String))) {
        $adminIapJsonText | Set-Content -Encoding UTF8 -LiteralPath $adminIapJsonPath
        $adminIapResult = $adminIapJsonText | ConvertFrom-Json
        $adminIapResult.notionSummary | Set-Content -Encoding UTF8 -LiteralPath $adminIapSummaryPath
    }
}

$docLegacyResult = $null
$docLegacyExitCode = $null
if (-not $SkipDocLegacyReview) {
    $docLegacyScript = Join-Path $scriptsDir "check-doc-legacy-references.ps1"
    $docLegacyArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $docLegacyScript,
        "-AsJson"
    )
    $docLegacyJsonText = & $PowerShellCommand @docLegacyArgs
    $docLegacyExitCode = $LASTEXITCODE
    if (-not [string]::IsNullOrWhiteSpace(($docLegacyJsonText | Out-String))) {
        $docLegacyJsonText | Set-Content -Encoding UTF8 -LiteralPath $docLegacyJsonPath
        $docLegacyResult = $docLegacyJsonText | ConvertFrom-Json
        $docLegacyResult.notionSummary | Set-Content -Encoding UTF8 -LiteralPath $docLegacySummaryPath
    }
}

$monitoringResult = $null
$monitoringExitCode = $null
if (-not $SkipMonitoringReview) {
    $monitoringScript = Join-Path $scriptsDir "check-monitoring-noise.ps1"
    $monitoringArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $monitoringScript,
        "-AsJson"
    )
    $monitoringJsonText = & $PowerShellCommand @monitoringArgs
    $monitoringExitCode = $LASTEXITCODE
    if (-not [string]::IsNullOrWhiteSpace(($monitoringJsonText | Out-String))) {
        $monitoringJsonText | Set-Content -Encoding UTF8 -LiteralPath $monitoringJsonPath
        $monitoringResult = $monitoringJsonText | ConvertFrom-Json
        $monitoringResult.notionSummary | Set-Content -Encoding UTF8 -LiteralPath $monitoringSummaryPath
    }
}

$repoHygieneResult = $null
$repoHygieneExitCode = $null
if (-not $SkipRepoHygieneReview) {
    $repoHygieneScript = Join-Path $scriptsDir "check-secret-exposure-metadata.ps1"
    $repoHygieneArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $repoHygieneScript,
        "-AsJson"
    )
    $repoHygieneJsonText = & $PowerShellCommand @repoHygieneArgs
    $repoHygieneExitCode = $LASTEXITCODE
    if (-not [string]::IsNullOrWhiteSpace(($repoHygieneJsonText | Out-String))) {
        $repoHygieneJsonText | Set-Content -Encoding UTF8 -LiteralPath $repoHygieneJsonPath
        $repoHygieneResult = $repoHygieneJsonText | ConvertFrom-Json
        $repoHygieneResult.notionSummary | Set-Content -Encoding UTF8 -LiteralPath $repoHygieneSummaryPath
    }
}

$combinedSummary = if ($result) { [string]$result.notionSummary } else { "" }
if ($auditResult) {
    if (-not [string]::IsNullOrWhiteSpace($combinedSummary)) {
        $combinedSummary += [Environment]::NewLine + [Environment]::NewLine
    }
    $combinedSummary += [string]$auditResult.notionSummary
}
if ($adminIapResult) {
    if (-not [string]::IsNullOrWhiteSpace($combinedSummary)) {
        $combinedSummary += [Environment]::NewLine + [Environment]::NewLine
    }
    $combinedSummary += [string]$adminIapResult.notionSummary
}
if ($docLegacyResult) {
    if (-not [string]::IsNullOrWhiteSpace($combinedSummary)) {
        $combinedSummary += [Environment]::NewLine + [Environment]::NewLine
    }
    $combinedSummary += [string]$docLegacyResult.notionSummary
}
if ($monitoringResult) {
    if (-not [string]::IsNullOrWhiteSpace($combinedSummary)) {
        $combinedSummary += [Environment]::NewLine + [Environment]::NewLine
    }
    $combinedSummary += [string]$monitoringResult.notionSummary
}
if ($repoHygieneResult) {
    if (-not [string]::IsNullOrWhiteSpace($combinedSummary)) {
        $combinedSummary += [Environment]::NewLine + [Environment]::NewLine
    }
    $combinedSummary += [string]$repoHygieneResult.notionSummary
}

$notionWrite = $null
if ($RecordToNotion) {
    if (-not $result) {
        throw "Read-only check result is missing; cannot record to Notion."
    }
    $notionToken = Get-NotionApiToken `
        -TokenEnv $NotionTokenEnv `
        -TokenSecret $NotionTokenSecret `
        -TokenSecretProject $NotionTokenSecretProject
    $notionWrite = Write-NotionReadOnlySummary `
        -PageId $NotionPageId `
        -Token $notionToken `
        -Version $NotionVersion `
        -CheckResult $result `
        -SummaryText $combinedSummary `
        -JsonPath $jsonPath `
        -SummaryPath $summaryPath `
        -RunMetadataPath $runMetadataPath `
        -AuditJsonPath $(if ($auditResult) { $auditJsonPath } else { "" }) `
        -AuditSummaryPath $(if ($auditResult) { $auditSummaryPath } else { "" }) `
        -AdminIapJsonPath $(if ($adminIapResult) { $adminIapJsonPath } else { "" }) `
        -AdminIapSummaryPath $(if ($adminIapResult) { $adminIapSummaryPath } else { "" }) `
        -DocLegacyJsonPath $(if ($docLegacyResult) { $docLegacyJsonPath } else { "" }) `
        -DocLegacySummaryPath $(if ($docLegacyResult) { $docLegacySummaryPath } else { "" }) `
        -MonitoringJsonPath $(if ($monitoringResult) { $monitoringJsonPath } else { "" }) `
        -MonitoringSummaryPath $(if ($monitoringResult) { $monitoringSummaryPath } else { "" }) `
        -RepoHygieneJsonPath $(if ($repoHygieneResult) { $repoHygieneJsonPath } else { "" }) `
        -RepoHygieneSummaryPath $(if ($repoHygieneResult) { $repoHygieneSummaryPath } else { "" })
    $notionToken = $null
}

$finalExitCode = $exitCode
if (($null -ne $auditExitCode) -and ($auditExitCode -ne 0)) {
    $finalExitCode = $auditExitCode
}
if (($null -ne $adminIapExitCode) -and ($adminIapExitCode -ne 0)) {
    $finalExitCode = $adminIapExitCode
}
if (($null -ne $docLegacyExitCode) -and ($docLegacyExitCode -ne 0)) {
    $finalExitCode = $docLegacyExitCode
}
if (($null -ne $monitoringExitCode) -and ($monitoringExitCode -ne 0)) {
    $finalExitCode = $monitoringExitCode
}
if (($null -ne $repoHygieneExitCode) -and ($repoHygieneExitCode -ne 0)) {
    $finalExitCode = $repoHygieneExitCode
}

$stopwatch.Stop()
$runEndedAt = (Get-Date).ToUniversalTime()
$runPassed = if ($result) {
        [bool]$result.passed -and
            (($null -eq $auditExitCode) -or ($auditExitCode -eq 0)) -and
            (($null -eq $auditResult) -or [bool]$auditResult.querySucceeded) -and
            (($null -eq $adminIapExitCode) -or ($adminIapExitCode -eq 0)) -and
            (($null -eq $adminIapResult) -or [bool]$adminIapResult.passed) -and
            (($null -eq $docLegacyExitCode) -or ($docLegacyExitCode -eq 0)) -and
            (($null -eq $docLegacyResult) -or [bool]$docLegacyResult.passed) -and
            (($null -eq $monitoringExitCode) -or ($monitoringExitCode -eq 0)) -and
            (($null -eq $monitoringResult) -or [bool]$monitoringResult.querySucceeded) -and
            (($null -eq $repoHygieneExitCode) -or ($repoHygieneExitCode -eq 0)) -and
            (($null -eq $repoHygieneResult) -or [bool]$repoHygieneResult.passed)
    } else { $false }
$failedChecks = @()
if ($result -and ($null -ne $result.failedChecks)) {
    $failedChecks = @($result.failedChecks)
} else {
    $failedChecks = @("operations_result_missing")
}
$auditSucceeded = if ($null -eq $auditResult) { $null } else { [bool]$auditResult.querySucceeded }
$adminIapSucceeded = if ($null -eq $adminIapResult) { $null } else { [bool]$adminIapResult.passed }
$docLegacySucceeded = if ($null -eq $docLegacyResult) { $null } else { [bool]$docLegacyResult.passed }
$monitoringSucceeded = if ($null -eq $monitoringResult) { $null } else { [bool]$monitoringResult.querySucceeded }
$repoHygieneSucceeded = if ($null -eq $repoHygieneResult) { $null } else { [bool]$repoHygieneResult.passed }
$auditFailureReason = if ($auditResult -and $auditResult.error) {
    [string]$auditResult.error
} elseif (($null -ne $auditExitCode) -and ($auditExitCode -ne 0) -and ($null -eq $auditResult)) {
    "admin_audit_review_failed_without_json"
} else {
    $null
}
$adminIapFailureReason = if (($null -ne $adminIapExitCode) -and ($adminIapExitCode -ne 0) -and ($null -eq $adminIapResult)) {
    "admin_iap_review_failed_without_json"
} else {
    $null
}
$docLegacyFailureReason = if (($null -ne $docLegacyExitCode) -and ($docLegacyExitCode -ne 0) -and ($null -eq $docLegacyResult)) {
    "docs_legacy_review_failed_without_json"
} else {
    $null
}
$monitoringFailureReason = if (($null -ne $monitoringExitCode) -and ($monitoringExitCode -ne 0) -and ($null -eq $monitoringResult)) {
    "monitoring_noise_review_failed_without_json"
} else {
    $null
}
$repoHygieneFailureReason = if (($null -ne $repoHygieneExitCode) -and ($repoHygieneExitCode -ne 0) -and ($null -eq $repoHygieneResult)) {
    "repo_hygiene_review_failed_without_json"
} else {
    $null
}

$runMetadata = [pscustomObject]@{
    generatedAt = $runEndedAt.ToString("o")
    runStartedAt = $runStartedAt.ToString("o")
    runEndedAt = $runEndedAt.ToString("o")
    durationSeconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
    passed = $runPassed
    jsonPath = $jsonPath
    summaryPath = $summaryPath
    auditJsonPath = if ($auditResult) { $auditJsonPath } else { $null }
    auditSummaryPath = if ($auditResult) { $auditSummaryPath } else { $null }
    adminIapJsonPath = if ($adminIapResult) { $adminIapJsonPath } else { $null }
    adminIapSummaryPath = if ($adminIapResult) { $adminIapSummaryPath } else { $null }
    docLegacyJsonPath = if ($docLegacyResult) { $docLegacyJsonPath } else { $null }
    docLegacySummaryPath = if ($docLegacyResult) { $docLegacySummaryPath } else { $null }
    monitoringJsonPath = if ($monitoringResult) { $monitoringJsonPath } else { $null }
    monitoringSummaryPath = if ($monitoringResult) { $monitoringSummaryPath } else { $null }
    repoHygieneJsonPath = if ($repoHygieneResult) { $repoHygieneJsonPath } else { $null }
    repoHygieneSummaryPath = if ($repoHygieneResult) { $repoHygieneSummaryPath } else { $null }
    runMetadataPath = $runMetadataPath
    notionRecorded = ($null -ne $notionWrite)
    notionWrite = $notionWrite
    exitCode = $finalExitCode
    operationsExitCode = $exitCode
    auditExitCode = $auditExitCode
    adminIapExitCode = $adminIapExitCode
    docLegacyExitCode = $docLegacyExitCode
    monitoringExitCode = $monitoringExitCode
    repoHygieneExitCode = $repoHygieneExitCode
    failedChecks = [object[]]$failedChecks
    auditQuerySucceeded = $auditSucceeded
    auditFailureReason = $auditFailureReason
    adminIapCheckPassed = $adminIapSucceeded
    adminIapFailureReason = $adminIapFailureReason
    docLegacyCheckPassed = $docLegacySucceeded
    docLegacyUnexpectedMatches = if ($docLegacyResult) { [int]$docLegacyResult.unexpectedMatches } else { $null }
    docLegacyFailureReason = $docLegacyFailureReason
    monitoringQuerySucceeded = $monitoringSucceeded
    monitoringCriticalSignalsTotal = if ($monitoringResult) { [int]$monitoringResult.criticalSignalsTotal } else { $null }
    monitoringWarningSignalsTotal = if ($monitoringResult) { [int]$monitoringResult.warningSignalsTotal } else { $null }
    monitoringThresholdChangeRecommended = if ($monitoringResult) { [bool]$monitoringResult.thresholdChangeRecommended } else { $null }
    monitoringChannelSplitRecommended = if ($monitoringResult) { [bool]$monitoringResult.channelSplitRecommended } else { $null }
    monitoringFailureReason = $monitoringFailureReason
    repoHygieneCheckPassed = $repoHygieneSucceeded
    repoHygieneTrackedSensitivePathCount = if ($repoHygieneResult) { [int]$repoHygieneResult.repoHygieneSummary.trackedSensitivePathCount } else { $null }
    repoHygieneHistorySensitivePathCount = if ($repoHygieneResult) { [int]$repoHygieneResult.repoHygieneSummary.historySensitivePathCount } else { $null }
    repoHygieneLegacyAwsEnvRefCount = if ($repoHygieneResult) { [int]$repoHygieneResult.repoHygieneSummary.legacyAwsEnvRefCount } else { $null }
    repoHygieneLegacyAwsSecretExistsCount = if ($repoHygieneResult) { [int]$repoHygieneResult.repoHygieneSummary.legacyAwsSecretExistsCount } else { $null }
    repoHygieneRewriteRequiredByCurrentMetadata = if ($repoHygieneResult) { [bool]$repoHygieneResult.repoHygieneSummary.rewriteRequiredByCurrentMetadata } else { $null }
    repoHygieneFailureReason = $repoHygieneFailureReason
    expectedIapUsers = [object[]]$ExpectedIapUsers
}

$runMetadata | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 -LiteralPath $runMetadataPath
$runMetadata

exit $finalExitCode
