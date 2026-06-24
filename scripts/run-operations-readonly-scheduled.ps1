param(
    [string]$OutDir = "artifacts\operations-readonly",
    [switch]$CaptureScreenshots,
    [switch]$SkipAdminAuditReview,
    [switch]$SkipAdminIapReview,
    [switch]$SkipDocLegacyReview,
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
        [string]$DocLegacySummaryPath = ""
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
$runMetadataPath = Join-Path $resolvedOutDir "operations-readonly-run-metadata-$timestamp.json"

$checkScript = Join-Path $workspace "scripts\check-operations-readonly.ps1"
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
    $auditScript = Join-Path $workspace "scripts\check-admin-audit-logs.ps1"
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
    $adminIapScript = Join-Path $workspace "scripts\check-admin-iap-readonly.ps1"
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
    $docLegacyScript = Join-Path $workspace "scripts\check-doc-legacy-references.ps1"
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
        -DocLegacySummaryPath $(if ($docLegacyResult) { $docLegacySummaryPath } else { "" })
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

$stopwatch.Stop()
$runEndedAt = (Get-Date).ToUniversalTime()
$runPassed = if ($result) {
        [bool]$result.passed -and
            (($null -eq $auditExitCode) -or ($auditExitCode -eq 0)) -and
            (($null -eq $auditResult) -or [bool]$auditResult.querySucceeded) -and
            (($null -eq $adminIapExitCode) -or ($adminIapExitCode -eq 0)) -and
            (($null -eq $adminIapResult) -or [bool]$adminIapResult.passed) -and
            (($null -eq $docLegacyExitCode) -or ($docLegacyExitCode -eq 0)) -and
            (($null -eq $docLegacyResult) -or [bool]$docLegacyResult.passed)
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
    runMetadataPath = $runMetadataPath
    notionRecorded = ($null -ne $notionWrite)
    notionWrite = $notionWrite
    exitCode = $finalExitCode
    operationsExitCode = $exitCode
    auditExitCode = $auditExitCode
    adminIapExitCode = $adminIapExitCode
    docLegacyExitCode = $docLegacyExitCode
    failedChecks = [object[]]$failedChecks
    auditQuerySucceeded = $auditSucceeded
    auditFailureReason = $auditFailureReason
    adminIapCheckPassed = $adminIapSucceeded
    adminIapFailureReason = $adminIapFailureReason
    docLegacyCheckPassed = $docLegacySucceeded
    docLegacyUnexpectedMatches = if ($docLegacyResult) { [int]$docLegacyResult.unexpectedMatches } else { $null }
    docLegacyFailureReason = $docLegacyFailureReason
    expectedIapUsers = [object[]]$ExpectedIapUsers
}

$runMetadata | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 -LiteralPath $runMetadataPath
$runMetadata

exit $finalExitCode
