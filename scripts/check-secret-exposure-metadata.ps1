param(
    [string]$Project = "ice-sh",
    [string]$Region = "asia-northeast1",
    [string]$Service = "report-generator",
    [string[]]$SensitivePaths = @("env.yaml", "webhook.txt"),
    [string[]]$SecretNames = @(
        "report-generator-admin-api-key",
        "slack-download-webhook-url",
        "aws-ses-access-key-id",
        "aws-ses-secret-access-key",
        "aws-ses-region",
        "aws-ses-from-address"
    ),
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

function Invoke-JsonCommand {
    param([string[]]$Command)

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Command[0] $Command[1..($Command.Length - 1)] 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($exitCode -ne 0 -or -not $output) {
        return $null
    }

    try {
        return ($output | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-GitPathMetadata {
    param([string[]]$Paths)

    $rows = @()
    foreach ($path in $Paths) {
        $tracked = (& git ls-files -- $path)
        $logRows = @(& git log --format="%h`t%ad`t%s" --date=short -- $path)

        $rows += [pscustomobject]@{
            path = $path
            trackedInHead = [bool]$tracked
            commitCount = $logRows.Count
            firstCommit = if ($logRows.Count -gt 0) { $logRows[-1] } else { "" }
            latestCommit = if ($logRows.Count -gt 0) { $logRows[0] } else { "" }
        }
    }

    return $rows
}

function Get-CloudRunEnvMetadata {
    param(
        [string]$Project,
        [string]$Region,
        [string]$Service
    )

    $serviceJson = Invoke-JsonCommand @(
        "gcloud.cmd",
        "run",
        "services",
        "describe",
        $Service,
        "--project",
        $Project,
        "--region",
        $Region,
        "--format=json"
    )

    if ($null -eq $serviceJson) {
        return @()
    }

    $envs = @($serviceJson.spec.template.spec.containers[0].env)
    return @(
        $envs | ForEach-Object {
            [pscustomobject]@{
                name = $_.name
                secretName = $_.valueFrom.secretKeyRef.name
                secretKey = $_.valueFrom.secretKeyRef.key
                hasLiteralValue = [bool]$_.value
            }
        } | Sort-Object name
    )
}

function Get-SecretVersionMetadata {
    param(
        [string]$Project,
        [string[]]$Names
    )

    $rows = @()
    foreach ($name in $Names) {
        $secret = Invoke-JsonCommand @(
            "gcloud.cmd",
            "secrets",
            "describe",
            $name,
            "--project",
            $Project,
            "--format=json"
        )

        if ($null -eq $secret) {
            $rows += [pscustomobject]@{
                name = $name
                exists = $false
                createTime = ""
                versions = @()
            }
            continue
        }

        $versions = Invoke-JsonCommand @(
            "gcloud.cmd",
            "secrets",
            "versions",
            "list",
            $name,
            "--project",
            $Project,
            "--format=json"
        )

        $versionRows = @()
        if ($null -ne $versions) {
            $versionRows = @(
                $versions | ForEach-Object {
                    [pscustomobject]@{
                        version = ($_.name -split "/")[-1]
                        state = $_.state
                        createTime = $_.createTime
                        destroyTime = $_.destroyTime
                    }
                } | Sort-Object version -Descending
            )
        }

        $rows += [pscustomobject]@{
            name = $name
            exists = $true
            createTime = $secret.createTime
            versions = $versionRows
        }
    }

    return $rows
}

$gitPaths = Get-GitPathMetadata -Paths $SensitivePaths
$cloudRunEnv = Get-CloudRunEnvMetadata -Project $Project -Region $Region -Service $Service
$secrets = Get-SecretVersionMetadata -Project $Project -Names $SecretNames

$legacyAwsEnvRefs = @(
    $cloudRunEnv | Where-Object {
        $_.name -in @("AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY")
    }
)

$enabledSecretVersions = @(
    $secrets |
        Where-Object { $_.exists } |
        ForEach-Object {
            $secretName = $_.name
            @($_.versions) | Where-Object { $_.state -eq "ENABLED" -or $_.state -eq "enabled" } | ForEach-Object {
                [pscustomobject]@{
                    secret = $secretName
                    version = $_.version
                    createTime = $_.createTime
                }
            }
        }
)

$result = [pscustomobject]@{
    generatedAt = (Get-Date).ToUniversalTime().ToString("o")
    project = $Project
    region = $Region
    service = $Service
    gitPaths = $gitPaths
    cloudRunEnv = $cloudRunEnv
    legacyAwsEnvRefCount = $legacyAwsEnvRefs.Count
    secrets = $secrets
    enabledSecretVersions = $enabledSecretVersions
    note = "This report intentionally omits secret values and file contents."
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 12
} else {
    $result
}
