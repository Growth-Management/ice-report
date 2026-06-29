param(
    [string]$Project = "ice-sh",
    [string]$Region = "asia-northeast1",
    [string]$AdminService = "report-generator-admin",
    [string]$PublicBaseUrl = "https://report-generator-635067190197.asia-northeast1.run.app",
    [string]$AdminBaseUrl = "https://report-generator-admin-635067190197.asia-northeast1.run.app",
    [string]$Freshness = "30m",
    [string[]]$ExpectedIapUsers = @("sinohara@impress.co.jp"),
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

function Normalize-Email {
    param([string]$Value)

    $text = ([string]$Value).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return ""
    }
    if ($text.StartsWith("accounts.google.com:")) {
        $text = $text.Substring("accounts.google.com:".Length)
    }
    if ($text.StartsWith("user:")) {
        $text = $text.Substring("user:".Length)
    }
    return $text.Trim().ToLowerInvariant()
}

function Normalize-IamMember {
    param([string]$Value)

    $text = ([string]$Value).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return ""
    }
    if ($text.StartsWith("user:")) {
        return "user:$(Normalize-Email $text)"
    }
    return $text.ToLowerInvariant()
}

function Split-AllowlistEmails {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return @()
    }
    return @(
        $Value.Replace(";", ",").Split(",") |
            ForEach-Object { Normalize-Email $_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Sort-Object -Unique
    )
}

function Get-EnvValue {
    param(
        [object[]]$EnvItems,
        [string]$Name
    )

    $match = @($EnvItems | Where-Object { $_.name -eq $Name } | Select-Object -First 1)
    if ($match.Count -eq 0) {
        return ""
    }
    return [string]$match[0].value
}

function Resolve-CurlCommand {
    $curlInfo = Get-Command "curl.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $curlInfo) {
        $curlInfo = Get-Command "curl" -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if (-not $curlInfo) {
        throw "curl command was not found."
    }
    return $curlInfo.Source
}

function Invoke-HttpNoRedirect {
    param([string]$Uri)

    $curlCommand = Resolve-CurlCommand
    $lines = @(& $curlCommand -sS -I --max-redirs 0 $Uri 2>&1)
    if ($LASTEXITCODE -ne 0) {
        return [pscustomobject]@{
            statusCode = $null
            contentLength = $null
            iapGeneratedResponse = $false
            locationHost = ""
            error = ($lines -join "`n")
        }
    }

    $statusCode = $null
    $headers = @{}
    foreach ($line in $lines) {
        if ($line -match '^HTTP/\S+\s+(\d+)') {
            $statusCode = [int]$Matches[1]
            continue
        }
        if ($line -match '^([^:]+):\s*(.*)$') {
            $headers[$Matches[1].ToLowerInvariant()] = $Matches[2].Trim()
        }
    }

    $locationHost = ""
    if ($headers.ContainsKey("location")) {
        try {
            $locationHost = ([uri]$headers["location"]).Host
        } catch {
            $locationHost = ""
        }
    }

    $contentLength = $null
    if ($headers.ContainsKey("content-length")) {
        try {
            $contentLength = [int]$headers["content-length"]
        } catch {
            $contentLength = $null
        }
    }

    return [pscustomobject]@{
        statusCode = $statusCode
        contentLength = $contentLength
        iapGeneratedResponse = (($headers["x-goog-iap-generated-response"] -as [string]).ToLowerInvariant() -eq "true")
        locationHost = $locationHost
        error = $null
    }
}

function Invoke-HttpCheck {
    param([string]$Uri)

    try {
        $response = Invoke-WebRequest `
            -Uri $Uri `
            -UseBasicParsing `
            -TimeoutSec 60
        return [pscustomobject]@{
            statusCode = [int]$response.StatusCode
            contentLength = [int]$response.RawContentLength
            bodyContainsOk = ([string]$response.Content -like "*ok*")
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
            bodyContainsOk = $false
            error = $_.Exception.Message
        }
    }
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

$generatedAt = (Get-Date).ToUniversalTime().ToString("o")
$adminBase = $AdminBaseUrl.TrimEnd("/")
$publicBase = $PublicBaseUrl.TrimEnd("/")

$serviceState = Invoke-GcloudJson {
    & $GcloudCommand run services describe $AdminService `
        --project=$Project `
        --region=$Region `
        --format=json
}

$projectNumber = $serviceState.metadata.namespace
$iapServiceAgent = "serviceAccount:service-$projectNumber@gcp-sa-iap.iam.gserviceaccount.com"
$latestRevision = $serviceState.status.latestReadyRevisionName
$image = $serviceState.spec.template.spec.containers[0].image
$iapEnabled = (($serviceState.metadata.annotations."run.googleapis.com/iap-enabled") -eq "true")
$envItems = @($serviceState.spec.template.spec.containers[0].env)
$envNames = @($envItems | ForEach-Object { $_.name } | Sort-Object)
$traffic = @($serviceState.status.traffic | ForEach-Object {
    [pscustomobject]@{
        revisionName = $_.revisionName
        percent = $_.percent
        latestRevision = $_.latestRevision
    }
})

$runIam = Invoke-GcloudJson {
    & $GcloudCommand run services get-iam-policy $AdminService `
        --project=$Project `
        --region=$Region `
        --format=json
}

$iapIam = Invoke-GcloudJson {
    & $GcloudCommand iap web get-iam-policy `
        --project=$Project `
        --region=$Region `
        --resource-type=cloud-run `
        --service=$AdminService `
        --format=json
}

$runInvokerMembers = @((
    @($runIam.bindings) |
        Where-Object { $_.role -eq "roles/run.invoker" } |
        ForEach-Object { @($_.members) }
) | Sort-Object -Unique)

$iapAccessorMembers = @((
    @($iapIam.bindings) |
        Where-Object { $_.role -eq "roles/iap.httpsResourceAccessor" } |
        ForEach-Object { @($_.members) }
) | Sort-Object -Unique)

$expectedIapUsersNormalized = @(
    $ExpectedIapUsers |
        ForEach-Object { Normalize-Email $_ } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique
)
$expectedIapUserMembers = @($expectedIapUsersNormalized | ForEach-Object { "user:$_" })
$iapAccessorMembersNormalized = @($iapAccessorMembers | ForEach-Object { Normalize-IamMember $_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
$missingExpectedIapAccessors = @($expectedIapUserMembers | Where-Object { $iapAccessorMembersNormalized -notcontains $_ })
$unexpectedIapAccessors = @($iapAccessorMembersNormalized | Where-Object { $expectedIapUserMembers -notcontains $_ })
$serviceAllowlistUsers = @(Split-AllowlistEmails (Get-EnvValue $envItems "ADMIN_IAP_ALLOWED_EMAILS"))
$missingServiceAllowlistUsers = @($expectedIapUsersNormalized | Where-Object { $serviceAllowlistUsers -notcontains $_ })
$unexpectedServiceAllowlistUsers = @($serviceAllowlistUsers | Where-Object { $expectedIapUsersNormalized -notcontains $_ })

$adminNoAuth = Invoke-HttpNoRedirect -Uri "$adminBase/admin"
$publicHealth = Invoke-HttpCheck -Uri "$publicBase/api-health"
$publicAdmin = Invoke-HttpCheck -Uri "$publicBase/admin"

$revisionFilter = 'resource.type="cloud_run_revision" AND resource.labels.service_name="' + $AdminService + '"'
if ($latestRevision) {
    $revisionFilter += ' AND resource.labels.revision_name="' + $latestRevision + '"'
}

$runtimeErrorCount = Get-LogCount ($revisionFilter + ' AND severity>=ERROR')
$adminAuditCount = Get-LogCount ($revisionFilter + ' AND textPayload:ICE_REPORT_ADMIN_AUDIT')
$adminAuthFailureCount = Get-LogCount ($revisionFilter + ' AND textPayload:ICE_REPORT_SECURITY_EVENT AND textPayload:admin_auth_failed')

$requiredEnvNames = @(
    "ADMIN_IAP_AUTH_ENABLED",
    "ADMIN_IAP_ALLOWED_EMAILS",
    "ADMIN_IAP_SERVICE_NAME"
)
$missingEnvNames = @($requiredEnvNames | Where-Object { $envNames -notcontains $_ })

$checks = [ordered]@{
    adminServiceIapEnabled = $iapEnabled
    adminServiceReadyRevision = -not [string]::IsNullOrWhiteSpace($latestRevision)
    adminServiceTraffic100 = (@($traffic | Where-Object { $_.percent -eq 100 }).Count -ge 1)
    adminServiceHasIapEnv = ($missingEnvNames.Count -eq 0)
    cloudRunInvokerOnlyIapServiceAgent = (
        ($runInvokerMembers.Count -eq 1) -and
        ($runInvokerMembers[0] -eq $iapServiceAgent)
    )
    iapAccessorConfigured = ($iapAccessorMembers.Count -ge 1)
    expectedIapUsersHaveAccessor = ($missingExpectedIapAccessors.Count -eq 0)
    noUnexpectedIapAccessors = ($unexpectedIapAccessors.Count -eq 0)
    expectedIapUsersInServiceAllowlist = ($missingServiceAllowlistUsers.Count -eq 0)
    noUnexpectedServiceAllowlistUsers = ($unexpectedServiceAllowlistUsers.Count -eq 0)
    adminNoAuthRedirectsToGoogle = (
        ($adminNoAuth.statusCode -eq 302) -and
        $adminNoAuth.iapGeneratedResponse -and
        ($adminNoAuth.locationHost -eq "accounts.google.com")
    )
    publicApiHealth200 = ($publicHealth.statusCode -eq 200)
    publicAdmin200 = ($publicAdmin.statusCode -eq 200)
    noAdminRuntimeErrors = ($runtimeErrorCount -eq 0)
}

$failedChecks = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
$passed = ($failedChecks.Count -eq 0)

$notionSummary = @"
ICE Report Generator Admin IAP read-only smoke

Generated at: $generatedAt
Admin service: $AdminService
Admin URL: $adminBase
Public URL: $publicBase
Latest ready revision: $latestRevision
Image: $image
Overall: $(if ($passed) { "PASS" } else { "FAIL" })

IAP / IAM:
- IAP enabled: $iapEnabled
- Cloud Run invoker members: $($runInvokerMembers -join ", ")
- IAP accessor members: $($iapAccessorMembers -join ", ")
- expected IAP users: $($expectedIapUsersNormalized -join ", ")
- missing expected IAP accessors: $(if ($missingExpectedIapAccessors.Count -eq 0) { "none" } else { $missingExpectedIapAccessors -join ", " })
- unexpected IAP accessors: $(if ($unexpectedIapAccessors.Count -eq 0) { "none" } else { $unexpectedIapAccessors -join ", " })
- service allowlist users: $($serviceAllowlistUsers -join ", ")
- missing service allowlist users: $(if ($missingServiceAllowlistUsers.Count -eq 0) { "none" } else { $missingServiceAllowlistUsers -join ", " })
- unexpected service allowlist users: $(if ($unexpectedServiceAllowlistUsers.Count -eq 0) { "none" } else { $unexpectedServiceAllowlistUsers -join ", " })
- required IAP env missing: $(if ($missingEnvNames.Count -eq 0) { "none" } else { $missingEnvNames -join ", " })

HTTP:
- admin /admin no auth: $($adminNoAuth.statusCode), iapGenerated=$($adminNoAuth.iapGeneratedResponse), locationHost=$($adminNoAuth.locationHost)
- public /api-health: $($publicHealth.statusCode)
- public /admin: $($publicAdmin.statusCode)

Logs ($Freshness):
- admin runtime ERROR: $runtimeErrorCount
- admin audit: $adminAuditCount
- admin auth failures: $adminAuthFailureCount

Failed checks: $(if ($failedChecks.Count -eq 0) { "none" } else { $failedChecks -join ", " })

Manual check still required:
- Allowed IAP user can complete browser login and load Admin UI.
- Human operation smoke, such as delivery create/version add/disable/enable, is executed separately.
"@

$result = [pscustomobject]@{
    generatedAt = $generatedAt
    project = $Project
    region = $Region
    adminService = $AdminService
    adminBaseUrl = $adminBase
    publicBaseUrl = $publicBase
    latestReadyRevision = $latestRevision
    image = $image
    iapEnabled = $iapEnabled
    envNames = $envNames
    missingEnvNames = $missingEnvNames
    traffic = $traffic
    iam = [pscustomobject]@{
        expectedIapServiceAgent = $iapServiceAgent
        expectedIapUsers = $expectedIapUsersNormalized
        runInvokerMembers = $runInvokerMembers
        iapAccessorMembers = $iapAccessorMembers
        iapAccessorMembersNormalized = $iapAccessorMembersNormalized
        missingExpectedIapAccessors = $missingExpectedIapAccessors
        unexpectedIapAccessors = $unexpectedIapAccessors
        serviceAllowlistUsers = $serviceAllowlistUsers
        missingServiceAllowlistUsers = $missingServiceAllowlistUsers
        unexpectedServiceAllowlistUsers = $unexpectedServiceAllowlistUsers
    }
    endpointStatus = [pscustomobject]@{
        adminNoAuth = $adminNoAuth
        publicApiHealth = $publicHealth
        publicAdmin = $publicAdmin
    }
    counts = [pscustomobject]@{
        adminRuntimeErrors = $runtimeErrorCount
        adminAuditLogs = $adminAuditCount
        adminAuthFailures = $adminAuthFailureCount
    }
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
