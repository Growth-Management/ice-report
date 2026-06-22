param(
    [string]$Project = "ice-sh",
    [string]$Region = "asia-northeast1",
    [string]$Service = "report-generator",
    [string]$AdminSecret = "report-generator-admin-api-key",
    [string]$BaseUrl = "https://report-generator-635067190197.asia-northeast1.run.app",
    [string]$Freshness = "30m",
    [switch]$Execute,
    [switch]$PromptForNewKey,
    [switch]$PromptForOldKey,
    [switch]$VerifyOnly,
    [switch]$SkipHttpVerification,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$GcloudCommand = if (Get-Command gcloud.cmd -ErrorAction SilentlyContinue) {
    "gcloud.cmd"
} else {
    "gcloud"
}

function Convert-SecureStringToPlainText {
    param([securestring]$Value)

    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        if ($ptr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        }
    }
}

function Invoke-HttpCheck {
    param(
        [string]$Uri,
        [hashtable]$Headers = @{}
    )

    try {
        $params = @{
            Uri = $Uri
            Method = "Get"
            UseBasicParsing = $true
            TimeoutSec = 60
        }
        if ($Headers.Count -gt 0) {
            $params.Headers = $Headers
        }
        $response = Invoke-WebRequest @params
        return [pscustomobject]@{
            statusCode = [int]$response.StatusCode
            error = $null
        }
    } catch {
        $statusCode = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode.value__
        }
        return [pscustomobject]@{
            statusCode = $statusCode
            error = $_.Exception.Message
        }
    }
}

function Get-LogCount {
    param([string]$Filter)

    $lines = @(
        & $GcloudCommand logging read $Filter `
            --project=$Project `
            --freshness=$Freshness `
            --limit=100 `
            --format="value(timestamp)" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    return @($lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
}

function Get-CurrentAdminKey {
    $key = (& $GcloudCommand secrets versions access latest --secret=$AdminSecret --project=$Project 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($key)) {
        throw "Failed to read current admin key from Secret Manager."
    }
    return $key.Trim()
}

function Add-AdminKeyVersion {
    param([string]$PlainKey)

    $PlainKey | & $GcloudCommand secrets versions add $AdminSecret `
        --project=$Project `
        --data-file=-
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to add new admin key secret version."
    }
}

function Get-SecretVersionState {
    $versions = @(
        & $GcloudCommand secrets versions list $AdminSecret `
            --project=$Project `
            --sort-by="~createTime" `
            --limit=5 `
            --format=json
    )
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($versions | Out-String))) {
        return @()
    }
    return @($versions | ConvertFrom-Json | ForEach-Object {
        [pscustomobject]@{
            name = $_.name
            state = $_.state
            createTime = $_.createTime
        }
    })
}

$generatedAt = (Get-Date).ToUniversalTime().ToString("o")
$base = $BaseUrl.TrimEnd("/")
$actions = @()
$oldKeyCheck = $null

if ($VerifyOnly -and $Execute) {
    throw "-VerifyOnly and -Execute cannot be used together."
}

if ($Execute -and -not $PromptForNewKey) {
    throw "Execution requires -PromptForNewKey so the new key is entered interactively and not stored in a file."
}

if ($Execute) {
    $secure = Read-Host "New ADMIN_API_KEY" -AsSecureString
    $plain = Convert-SecureStringToPlainText -Value $secure
    try {
        if ([string]::IsNullOrWhiteSpace($plain)) {
            throw "New admin key must not be empty."
        }
        Add-AdminKeyVersion -PlainKey $plain
        $actions += "added_secret_version"
    } finally {
        $plain = $null
        $secure = $null
    }
} elseif (-not $VerifyOnly) {
    $actions += "dry_run_no_secret_written"
}

if ($PromptForOldKey) {
    $oldSecure = Read-Host "Old ADMIN_API_KEY" -AsSecureString
    $oldPlain = Convert-SecureStringToPlainText -Value $oldSecure
    try {
        if (-not [string]::IsNullOrWhiteSpace($oldPlain)) {
            $oldKeyCheck = Invoke-HttpCheck `
                -Uri "$base/deliveries?limit=1" `
                -Headers @{ "X-Admin-Key" = $oldPlain }
        }
    } finally {
        $oldPlain = $null
        $oldSecure = $null
    }
}

$health = $null
$noAuth = $null
$wrongKey = $null
$currentKeyCheck = $null

if (-not $SkipHttpVerification) {
    $currentKey = Get-CurrentAdminKey
    try {
        $health = Invoke-HttpCheck -Uri "$base/api-health"
        $noAuth = Invoke-HttpCheck -Uri "$base/deliveries?limit=1"
        $wrongKey = Invoke-HttpCheck `
            -Uri "$base/deliveries?limit=1" `
            -Headers @{ "X-Admin-Key" = "wrong-admin-key-for-rotation-check" }
        $currentKeyCheck = Invoke-HttpCheck `
            -Uri "$base/deliveries?limit=1" `
            -Headers @{ "X-Admin-Key" = $currentKey }
    } finally {
        $currentKey = $null
    }
}

$revisionFilter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="' + $Service + '"'
$adminAuthFailureCount = Get-LogCount ($revisionFilter + ' AND textPayload:"ICE_REPORT_SECURITY_EVENT" AND textPayload:"admin_auth_failed"')
$adminAuditFailureCount = Get-LogCount ($revisionFilter + ' AND textPayload:"ICE_REPORT_ADMIN_AUDIT" AND textPayload:"action=admin_auth" AND textPayload:"result=failure"')
$runtimeErrorCount = Get-LogCount ($revisionFilter + ' AND severity>=ERROR')
$secretVersions = Get-SecretVersionState

$checks = [ordered]@{
    noRuntimeErrors = ($runtimeErrorCount -eq 0)
}
if (-not $SkipHttpVerification) {
    $checks = [ordered]@{
        apiHealth200 = ($health.statusCode -eq 200)
        noAuth401 = ($noAuth.statusCode -eq 401)
        wrongKey401 = ($wrongKey.statusCode -eq 401)
        currentKey200 = ($currentKeyCheck.statusCode -eq 200)
        noRuntimeErrors = ($runtimeErrorCount -eq 0)
    }
}
if ($oldKeyCheck) {
    $checks["oldKey401"] = ($oldKeyCheck.statusCode -eq 401)
}
$failedChecks = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
$passed = ($failedChecks.Count -eq 0)

$summary = @(
    "ICE Report Generator Admin key rotation check",
    "",
    "Generated at: $generatedAt",
    "Project: $Project",
    "Service: $Service",
    "Mode: $(if ($Execute) { 'EXECUTE' } elseif ($VerifyOnly) { 'VERIFY_ONLY' } else { 'DRY_RUN' })",
    "Secret: $AdminSecret",
    "Overall: $(if ($passed) { 'PASS' } else { 'FAIL' })",
    "",
    "HTTP:",
    "- /api-health: $(if ($health) { $health.statusCode } else { 'skipped' })",
    "- /deliveries no auth: $(if ($noAuth) { $noAuth.statusCode } else { 'skipped' })",
    "- /deliveries wrong key: $(if ($wrongKey) { $wrongKey.statusCode } else { 'skipped' })",
    "- /deliveries current key: $(if ($currentKeyCheck) { $currentKeyCheck.statusCode } else { 'skipped' })",
    "- /deliveries old key: $(if ($oldKeyCheck) { $oldKeyCheck.statusCode } else { 'not checked' })",
    "",
    "Logs ($Freshness):",
    "- admin_auth_failed security events: $adminAuthFailureCount",
    "- admin_auth audit failures: $adminAuditFailureCount",
    "- runtime ERROR: $runtimeErrorCount",
    "",
    "Failed checks: $(if ($failedChecks.Count -eq 0) { 'none' } else { $failedChecks -join ', ' })",
    "",
    "Transfer rule: paste status codes, counts, and version metadata only. Do not paste Admin key, secret payload, token, PIN, raw recipient email, or response bodies."
) -join [Environment]::NewLine

$result = [pscustomobject]@{
    generatedAt = $generatedAt
    project = $Project
    region = $Region
    service = $Service
    baseUrl = $base
    adminSecret = $AdminSecret
    mode = if ($Execute) { "execute" } elseif ($VerifyOnly) { "verify_only" } else { "dry_run" }
    actions = $actions
    endpointStatus = [pscustomobject]@{
        apiHealth = if ($health) { $health.statusCode } else { $null }
        deliveriesNoAuth = if ($noAuth) { $noAuth.statusCode } else { $null }
        deliveriesWrongKey = if ($wrongKey) { $wrongKey.statusCode } else { $null }
        deliveriesCurrentKey = if ($currentKeyCheck) { $currentKeyCheck.statusCode } else { $null }
        deliveriesOldKey = if ($oldKeyCheck) { $oldKeyCheck.statusCode } else { $null }
    }
    counts = [pscustomobject]@{
        adminAuthFailures = $adminAuthFailureCount
        adminAuditFailures = $adminAuditFailureCount
        runtimeErrors = $runtimeErrorCount
    }
    latestSecretVersions = $secretVersions
    checks = $checks
    failedChecks = $failedChecks
    passed = $passed
    notionSummary = $summary
    note = "Output intentionally omits Admin key values, secret payloads, tokens, PINs, raw recipient email, and response bodies."
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 10
} else {
    $result
}

if (-not $passed) {
    exit 1
}
