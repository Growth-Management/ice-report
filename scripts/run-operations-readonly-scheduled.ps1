param(
    [string]$OutDir = "artifacts\operations-readonly",
    [switch]$CaptureScreenshots,
    [switch]$SkipAdminAuditReview,
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
        [string]$AuditSummaryPath = ""
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

$combinedSummary = if ($result) { [string]$result.notionSummary } else { "" }
if ($auditResult) {
    if (-not [string]::IsNullOrWhiteSpace($combinedSummary)) {
        $combinedSummary += [Environment]::NewLine + [Environment]::NewLine
    }
    $combinedSummary += [string]$auditResult.notionSummary
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
        -AuditSummaryPath $(if ($auditResult) { $auditSummaryPath } else { "" })
    $notionToken = $null
}

$finalExitCode = $exitCode
if (($null -ne $auditExitCode) -and ($auditExitCode -ne 0)) {
    $finalExitCode = $auditExitCode
}

$stopwatch.Stop()
$runEndedAt = (Get-Date).ToUniversalTime()
$runPassed = if ($result) {
        [bool]$result.passed -and
            (($null -eq $auditExitCode) -or ($auditExitCode -eq 0)) -and
            (($null -eq $auditResult) -or [bool]$auditResult.querySucceeded)
    } else { $false }
$failedChecks = @()
if ($result -and ($null -ne $result.failedChecks)) {
    $failedChecks = @($result.failedChecks)
} else {
    $failedChecks = @("operations_result_missing")
}
$auditSucceeded = if ($null -eq $auditResult) { $null } else { [bool]$auditResult.querySucceeded }
$auditFailureReason = if ($auditResult -and $auditResult.error) {
    [string]$auditResult.error
} elseif (($null -ne $auditExitCode) -and ($auditExitCode -ne 0) -and ($null -eq $auditResult)) {
    "admin_audit_review_failed_without_json"
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
    runMetadataPath = $runMetadataPath
    notionRecorded = ($null -ne $notionWrite)
    notionWrite = $notionWrite
    exitCode = $finalExitCode
    operationsExitCode = $exitCode
    auditExitCode = $auditExitCode
    failedChecks = [object[]]$failedChecks
    auditQuerySucceeded = $auditSucceeded
    auditFailureReason = $auditFailureReason
}

$runMetadata | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 -LiteralPath $runMetadataPath
$runMetadata

exit $finalExitCode
