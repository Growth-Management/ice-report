param(
    [string]$Profile = "",
    [string]$Region = "ap-northeast-1",
    [string]$EmailIdentity = "ice-sv.jp"
)

$ErrorActionPreference = "Stop"

function Invoke-AwsJson([string[]]$AwsArgs) {
    $args = @()
    if ($Profile) {
        $args += @("--profile", $Profile)
    }
    $args += $AwsArgs

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & aws @args 2>&1
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

$callerResult = Invoke-AwsJson @("sts", "get-caller-identity")
$accountResult = Invoke-AwsJson @("sesv2", "get-account", "--region", $Region)
$identityResult = Invoke-AwsJson @(
    "sesv2",
    "get-email-identity",
    "--region", $Region,
    "--email-identity", $EmailIdentity
)

$caller = if ($callerResult.succeeded) { $callerResult.value } else { $null }
$account = if ($accountResult.succeeded) { $accountResult.value } else { $null }
$identity = if ($identityResult.succeeded) { $identityResult.value } else { $null }

[pscustomobject]@{
    profile = if ($Profile) { $Profile } else { "<current>" }
    callerSucceeded = $callerResult.succeeded
    callerArn = if ($caller) { $caller.Arn } else { $null }
    callerError = $callerResult.error
    accountReadSucceeded = $accountResult.succeeded
    accountReadError = $accountResult.error
    productionAccessEnabled = if ($account) { $account.ProductionAccessEnabled } else { $null }
    sendingEnabled = if ($account) { $account.SendingEnabled } else { $null }
    enforcementStatus = if ($account) { $account.EnforcementStatus } else { $null }
    max24HourSend = if ($account) { $account.SendQuota.Max24HourSend } else { $null }
    maxSendRate = if ($account) { $account.SendQuota.MaxSendRate } else { $null }
    sentLast24Hours = if ($account) { $account.SendQuota.SentLast24Hours } else { $null }
    identityReadSucceeded = $identityResult.succeeded
    identityReadError = $identityResult.error
    identityVerificationStatus = if ($identity) { $identity.VerificationStatus } else { $null }
    verifiedForSendingStatus = if ($identity) { $identity.VerifiedForSendingStatus } else { $null }
    dkimStatus = if ($identity) { $identity.DkimAttributes.Status } else { $null }
    mailFromDomain = if ($identity) { $identity.MailFromAttributes.MailFromDomain } else { $null }
    mailFromDomainStatus = if ($identity) { $identity.MailFromAttributes.MailFromDomainStatus } else { $null }
    behaviorOnMxFailure = if ($identity) { $identity.MailFromAttributes.BehaviorOnMxFailure } else { $null }
}
