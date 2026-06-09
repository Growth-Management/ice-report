param(
    [string]$BaseUrl = "https://report-generator-635067190197.asia-northeast1.run.app",
    [string]$Project = "ice-sh",
    [string]$AdminSecret = "report-generator-admin-api-key",
    [string]$OutDir = "artifacts"
)

$ErrorActionPreference = "Stop"

$workspace = (Resolve-Path ".").Path
$artifactDir = Join-Path $workspace $OutDir
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null

$profileDir = Join-Path $workspace ".tmp_chrome_profile"
if (Test-Path -LiteralPath $profileDir) {
    $resolved = (Resolve-Path -LiteralPath $profileDir).Path
    if (-not $resolved.StartsWith($workspace)) {
        throw "Refusing to remove profile outside workspace: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path -LiteralPath $chrome)) {
    $chrome = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
}
if (-not (Test-Path -LiteralPath $chrome)) {
    throw "Chrome or Edge executable was not found."
}

$adminKey = & gcloud.cmd secrets versions access latest --secret=$AdminSecret --project=$Project
if ($LASTEXITCODE -ne 0 -or -not $adminKey) {
    throw "Failed to read admin key from Secret Manager."
}

$port = Get-Random -Minimum 9300 -Maximum 9800
$proc = $null
$ws = $null

function Receive-CdpMessage([System.Net.WebSockets.ClientWebSocket]$socket) {
    $ms = [System.IO.MemoryStream]::new()
    $buffer = New-Object byte[] 1048576
    do {
        $segment = [System.ArraySegment[byte]]::new($buffer)
        $result = $socket.ReceiveAsync(
            $segment,
            [Threading.CancellationToken]::None
        ).GetAwaiter().GetResult()
        if ($result.Count -gt 0) {
            $ms.Write($buffer, 0, $result.Count)
        }
    } while (-not $result.EndOfMessage)

    [Text.Encoding]::UTF8.GetString($ms.ToArray())
}

function Send-CdpCommand(
    [System.Net.WebSockets.ClientWebSocket]$socket,
    [int]$id,
    [string]$method,
    $params = @{}
) {
    $payload = @{ id = $id; method = $method; params = $params } |
        ConvertTo-Json -Depth 20 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $null = $socket.SendAsync(
        [System.ArraySegment[byte]]::new($bytes),
        [System.Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult()

    while ($true) {
        $message = Receive-CdpMessage $socket
        $obj = $message | ConvertFrom-Json
        if ($obj.id -eq $id) {
            if ($obj.error) {
                throw ($obj.error | ConvertTo-Json -Compress)
            }
            return $obj
        }
    }
}

try {
    $args = @(
        "--headless=new",
        "--remote-debugging-port=$port",
        "--user-data-dir=$profileDir",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "about:blank"
    )
    $proc = Start-Process -FilePath $chrome -ArgumentList $args -PassThru -WindowStyle Hidden
    $jsonUrl = "http://127.0.0.1:$port/json"
    $targets = $null
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $targets = Invoke-RestMethod -Uri $jsonUrl -UseBasicParsing
            break
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $targets) {
        throw "Chrome DevTools endpoint did not start."
    }

    $page = @($targets | Where-Object { $_.type -eq "page" } | Select-Object -First 1)[0]
    $ws = [System.Net.WebSockets.ClientWebSocket]::new()
    $null = $ws.ConnectAsync(
        [Uri]$page.webSocketDebuggerUrl,
        [Threading.CancellationToken]::None
    ).GetAwaiter().GetResult()

    $id = 1
    Send-CdpCommand $ws $id "Page.enable" | Out-Null; $id++
    Send-CdpCommand $ws $id "Runtime.enable" | Out-Null; $id++
    Send-CdpCommand $ws $id "Page.navigate" @{ url = "$BaseUrl/api-health" } | Out-Null; $id++
    Start-Sleep -Seconds 2

    $adminJson = $adminKey | ConvertTo-Json -Compress
    Send-CdpCommand $ws $id "Runtime.evaluate" @{
        expression = "localStorage.setItem('ice_admin_api_key', $adminJson); true"
        awaitPromise = $true
    } | Out-Null
    $id++

    $captures = @(
        @{ name = "desktop"; width = 1440; height = 1200; mobile = $false },
        @{ name = "mobile"; width = 390; height = 1200; mobile = $true }
    )
    $metrics = @()

    foreach ($capture in $captures) {
        Send-CdpCommand $ws $id "Emulation.setDeviceMetricsOverride" @{
            width = $capture.width
            height = $capture.height
            deviceScaleFactor = 1
            mobile = $capture.mobile
        } | Out-Null
        $id++

        Send-CdpCommand $ws $id "Page.navigate" @{ url = "$BaseUrl/admin" } | Out-Null
        $id++
        Start-Sleep -Seconds 8

        $metricExpression = @'
(() => {
  const table = document.querySelector('.delivery-table');
  const wrap = table ? table.closest('.table-wrap') : null;
  const rows = table ? table.querySelectorAll('tbody tr').length : 0;
  const longUrl = document.querySelector('.delivery-url-link');
  const gcsToggle = document.querySelector('.delivery-uri-toggle');
  return {
    hasTable: !!table,
    rows,
    wrapperClientWidth: wrap ? wrap.clientWidth : 0,
    wrapperScrollWidth: wrap ? wrap.scrollWidth : 0,
    tableClientWidth: table ? table.clientWidth : 0,
    tableScrollWidth: table ? table.scrollWidth : 0,
    hasLongUrl: !!longUrl,
    longUrlTextLength: longUrl ? longUrl.textContent.length : 0,
    hasGcsToggle: !!gcsToggle,
    bodyScrollWidth: document.body.scrollWidth,
    viewportWidth: window.innerWidth
  };
})()
'@
        $metricResult = Send-CdpCommand $ws $id "Runtime.evaluate" @{
            expression = $metricExpression
            returnByValue = $true
            awaitPromise = $true
        }
        $id++

        $value = $metricResult.result.result.value
        $path = Join-Path $artifactDir "admin-deliveries-$($capture.name).png"
        $shot = Send-CdpCommand $ws $id "Page.captureScreenshot" @{
            format = "png"
            fromSurface = $true
            captureBeyondViewport = $true
        }
        $id++
        [IO.File]::WriteAllBytes($path, [Convert]::FromBase64String($shot.result.data))

        $metrics += [pscustomobject]@{
            viewport = $capture.name
            screenshot = $path
            hasTable = $value.hasTable
            rows = $value.rows
            wrapperClientWidth = $value.wrapperClientWidth
            wrapperScrollWidth = $value.wrapperScrollWidth
            tableClientWidth = $value.tableClientWidth
            tableScrollWidth = $value.tableScrollWidth
            hasLongUrl = $value.hasLongUrl
            longUrlTextLength = $value.longUrlTextLength
            hasGcsToggle = $value.hasGcsToggle
            bodyScrollWidth = $value.bodyScrollWidth
            viewportWidth = $value.viewportWidth
        }
    }

    $metrics
} finally {
    if ($ws) {
        $ws.Dispose()
    }
    if ($proc -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force
    }
    if (Test-Path -LiteralPath $profileDir) {
        $resolved = (Resolve-Path -LiteralPath $profileDir).Path
        if ($resolved.StartsWith($workspace)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
