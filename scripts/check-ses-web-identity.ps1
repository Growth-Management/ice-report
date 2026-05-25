param(
    [string]$Project = "ice-sh",
    [string]$ServiceAccount = "ice-report-runner@ice-sh.iam.gserviceaccount.com",
    [string]$Audience = "aws-ses-report-generator-prod",
    [string]$RoleArn = "arn:aws:iam::855532282119:role/IceReportSesWebIdentityRole",
    [string]$Region = "ap-northeast-1",
    [string]$EmailIdentity = "ice-sv.jp"
)

$ErrorActionPreference = "Stop"

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$token = & gcloud.cmd auth print-identity-token `
    --project=$Project `
    --audiences=$Audience `
    --impersonate-service-account=$ServiceAccount `
    --quiet 2>$null
$tokenExitCode = $LASTEXITCODE
$ErrorActionPreference = $oldErrorActionPreference
if ($tokenExitCode -ne 0 -or -not $token) {
    throw "Failed to mint Google identity token."
}

function Invoke-AwsJson([string[]]$AwsArgs) {
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & aws @AwsArgs 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorActionPreference

    if ($exitCode -ne 0) {
        $rawError = (($output | Out-String).Trim())
        $awsError = (($rawError -split "`r?`n") |
            Where-Object { $_ -match "^aws:" } |
            ForEach-Object { $_.Trim() }) -join " "
        if (-not $awsError) {
            $awsError = $rawError
        }

        return [pscustomobject]@{
            succeeded = $false
            value = $null
            error = $awsError
        }
    }

    return [pscustomobject]@{
        succeeded = $true
        value = (($output | Out-String) | ConvertFrom-Json)
        error = ""
    }
}

$stsResult = Invoke-AwsJson @(
    "sts",
    "assume-role-with-web-identity",
    "--role-arn", $RoleArn,
    "--role-session-name", "codex-local-ses-check",
    "--web-identity-token", $token,
    "--duration-seconds", "900"
)
if (-not $stsResult.succeeded) {
    throw $stsResult.error
}

$sts = $stsResult.value
$env:AWS_ACCESS_KEY_ID = $sts.Credentials.AccessKeyId
$env:AWS_SECRET_ACCESS_KEY = $sts.Credentials.SecretAccessKey
$env:AWS_SESSION_TOKEN = $sts.Credentials.SessionToken

try {
    $account = $null
    $identity = $null
    $accountError = ""
    $identityError = ""

    $accountResult = Invoke-AwsJson @("sesv2", "get-account", "--region", $Region)
    if ($accountResult.succeeded) {
        $account = $accountResult.value
    } else {
        $accountError = $accountResult.error
    }

    $identityResult = Invoke-AwsJson @(
        "sesv2",
        "get-email-identity",
        "--region", $Region,
        "--email-identity", $EmailIdentity
    )
    if ($identityResult.succeeded) {
        $identity = $identityResult.value
    } else {
        $identityError = $identityResult.error
    }

    [pscustomobject]@{
        assumedRoleArn = $sts.AssumedRoleUser.Arn
        credentialsExpireUtc = $sts.Credentials.Expiration
        accountReadSucceeded = [bool]$account
        accountReadError = $accountError
        productionAccessEnabled = if ($account) { $account.ProductionAccessEnabled } else { $null }
        sendingEnabled = if ($account) { $account.SendingEnabled } else { $null }
        enforcementStatus = if ($account) { $account.EnforcementStatus } else { $null }
        max24HourSend = if ($account) { $account.SendQuota.Max24HourSend } else { $null }
        maxSendRate = if ($account) { $account.SendQuota.MaxSendRate } else { $null }
        sentLast24Hours = if ($account) { $account.SendQuota.SentLast24Hours } else { $null }
        identityReadSucceeded = [bool]$identity
        identityReadError = $identityError
        identityVerificationStatus = if ($identity) { $identity.VerificationStatus } else { $null }
        verifiedForSendingStatus = if ($identity) { $identity.VerifiedForSendingStatus } else { $null }
        dkimStatus = if ($identity) { $identity.DkimAttributes.Status } else { $null }
        mailFromDomain = if ($identity) { $identity.MailFromAttributes.MailFromDomain } else { $null }
        mailFromDomainStatus = if ($identity) { $identity.MailFromAttributes.MailFromDomainStatus } else { $null }
        behaviorOnMxFailure = if ($identity) { $identity.MailFromAttributes.BehaviorOnMxFailure } else { $null }
    }
} finally {
    Remove-Item Env:\AWS_ACCESS_KEY_ID -ErrorAction SilentlyContinue
    Remove-Item Env:\AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
}
