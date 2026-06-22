param(
    [string[]]$Paths = @("README.md", "docs"),
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$patterns = @(
    "AWS_SES_ACCESS_KEY_ID",
    "AWS_SES_SECRET_ACCESS_KEY",
    "MAIL_FROM_EMAIL",
    "MAIL_FROM_NAME",
    "MAIL_PROVIDER_SES_REGION",
    "MAIL_PROVIDER_SES_CONFIGURATION_SET",
    "MAIL_PROVIDER_TIMEOUT_SECONDS",
    "access key",
    "long-lived access key",
    "legacy Secret",
    "legacy access key",
    "fallback name",
    "fallback env",
    "rollback"
)

$allowedFiles = @(
    "docs/env-compatibility.md",
    "docs/retention-deletion-approval-template.md",
    "docs/security.md",
    "docs/ses-cutover-checklist.md",
    "docs/roadmap.md",
    "README.md"
)

$benignPatterns = @(
    "rollback",
    "env-compatibility.md"
)
$benignRegex = [string]::Join("|", $benignPatterns)

$knownBenignRefs = @(
    "docs/monitoring.md:200",
    "docs/operations.md:151",
    "docs/operations.md:723",
    "docs/operations.md:1072",
    "docs/setup.md:69",
    "docs/setup.md:72"
)

$knownBenignTextPatterns = @(
    "ice-report-ops.*Access Key",
    "Access Key.*Secret.*Admin Key.*PIN.*token",
    "credential.*secret.*access key"
)
$knownBenignTextRegex = [string]::Join("|", $knownBenignTextPatterns)

$escapedPatterns = $patterns | ForEach-Object { [regex]::Escape($_) }
$regex = [string]::Join("|", $escapedPatterns)
$results = @()

foreach ($path in $Paths) {
    if (-not (Test-Path -LiteralPath $path)) {
        continue
    }

    $files = @()
    $item = Get-Item -LiteralPath $path
    if ($item.PSIsContainer) {
        $files = @(Get-ChildItem -LiteralPath $path -Recurse -File -Include *.md,*.ps1,*.py)
    } else {
        $files = @($item)
    }

    foreach ($file in $files) {
        $relative = Resolve-Path -LiteralPath $file.FullName -Relative
        $relative = $relative -replace '^[.][\\/]', ''
        $relative = $relative.Replace('\', '/')
        $lines = Get-Content -LiteralPath $file.FullName -Encoding UTF8

        for ($index = 0; $index -lt $lines.Count; $index++) {
            $line = $lines[$index]
            if ($line -notmatch $regex) {
                continue
            }

            $allowedFile = $allowedFiles -contains $relative
            $knownBenignRef = $knownBenignRefs -contains "$relative`:$($index + 1)"
            $knownBenignText = $line -match $knownBenignTextRegex
            $benign = (-not $allowedFile) -and ($line -match $benignRegex)
            $allowed = $allowedFile -or $benign -or $knownBenignRef -or $knownBenignText

            $results += [pscustomobject]@{
                path = $relative
                line = $index + 1
                allowed = $allowed
                allowedFile = $allowedFile
                benignOutsideAllowedFile = $benign
                knownBenignRef = $knownBenignRef
                knownBenignText = $knownBenignText
                text = $line.Trim()
            }
        }
    }
}

$result = [pscustomobject]@{
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    patterns = $patterns
    totalMatches = $results.Count
    unexpectedMatches = @($results | Where-Object { -not $_.allowed }).Count
    matches = $results
    note = "Review matches for legacy env/access-key assumptions. Allowed files may intentionally document deprecated names."
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 8
} else {
    $result
}
