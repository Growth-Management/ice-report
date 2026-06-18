param(
    [string]$OutDir = "artifacts\operations-readonly",
    [switch]$CaptureScreenshots
)

$ErrorActionPreference = "Stop"

$workspace = (Resolve-Path ".").Path
$resolvedOutDir = Join-Path $workspace $OutDir
New-Item -ItemType Directory -Force -Path $resolvedOutDir | Out-Null

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$jsonPath = Join-Path $resolvedOutDir "operations-readonly-check-$timestamp.json"
$summaryPath = Join-Path $resolvedOutDir "operations-readonly-check-$timestamp-summary.txt"

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

$jsonText = & powershell.exe @args
$exitCode = $LASTEXITCODE

if (-not [string]::IsNullOrWhiteSpace(($jsonText | Out-String))) {
    $jsonText | Set-Content -Encoding UTF8 -LiteralPath $jsonPath
    $result = $jsonText | ConvertFrom-Json
    $result.notionSummary | Set-Content -Encoding UTF8 -LiteralPath $summaryPath
}

[pscustomobject]@{
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    passed = if ($result) { [bool]$result.passed } else { $false }
    jsonPath = $jsonPath
    summaryPath = $summaryPath
    exitCode = $exitCode
}

exit $exitCode
