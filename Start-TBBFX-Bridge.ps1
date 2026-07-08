param(
    [string]$BackendUrl = "http://127.0.0.1:5000",
    [string]$PagesUrl = "https://tbbfx-intelligence-web.pages.dev",
    [Alias("Key")]
    [string]$SecurityKey = $env:TBBFX_FEATURE_UPDATE_KEY,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"

function Find-Cloudflared {
    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "$env:ProgramFiles\cloudflared\cloudflared.exe",
        "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
        "$env:LocalAppData\Programs\cloudflared\cloudflared.exe",
        "$env:LocalAppData\cloudflared\cloudflared.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "cloudflared was not found. Install it with: winget install --id Cloudflare.cloudflared -e"
}

function Test-Backend {
    param([string]$Url)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/macro" -TimeoutSec 5
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    }
    catch {
        return $false
    }
}

$cloudflared = Find-Cloudflared

Write-Host ""
Write-Host "TBBFX Cloudflare bridge" -ForegroundColor Green
Write-Host "cloudflared: $cloudflared" -ForegroundColor DarkGray
Write-Host "backend:     $BackendUrl" -ForegroundColor DarkGray
if ($SecurityKey) {
    Write-Host "security:    key supplied for protected bridge requests" -ForegroundColor DarkGray
}
Write-Host ""

if (-not (Test-Backend -Url $BackendUrl)) {
    Write-Host "Warning: $BackendUrl/api/macro did not respond yet." -ForegroundColor Yellow
    Write-Host "Start SignalRFeatureStore first, then keep this bridge window open." -ForegroundColor Yellow
    Write-Host ""
}

$tempLog = Join-Path $env:TEMP ("tbbfx-cloudflared-" + [Guid]::NewGuid().ToString("N") + ".log")
$arguments = @("tunnel", "--url", $BackendUrl)

Write-Host "Starting quick tunnel. Keep this PowerShell window open while using the hosted terminal." -ForegroundColor Cyan
Write-Host ""

$process = Start-Process -FilePath $cloudflared -ArgumentList $arguments -NoNewWindow -PassThru -RedirectStandardError $tempLog

$tunnelUrl = $null
$deadline = (Get-Date).AddSeconds(45)

while ((Get-Date) -lt $deadline -and -not $process.HasExited) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $tempLog) {
        $content = Get-Content -LiteralPath $tempLog -Raw -ErrorAction SilentlyContinue
        $match = [regex]::Match($content, "https://[a-z0-9-]+\.trycloudflare\.com")
        if ($match.Success) {
            $tunnelUrl = $match.Value
            break
        }
    }
}

if (-not $tunnelUrl) {
    Write-Host "Cloudflare did not return a tunnel URL yet. Recent log output:" -ForegroundColor Yellow
    if (Test-Path -LiteralPath $tempLog) {
        Get-Content -LiteralPath $tempLog -Tail 40
    }
    throw "Tunnel URL was not detected."
}

$encodedTunnelUrl = [Uri]::EscapeDataString($tunnelUrl)
$liveUrl = "$PagesUrl/?bridge=$encodedTunnelUrl"
$orderflowUrl = "$PagesUrl/orderflow/?bridge=$encodedTunnelUrl"

if ($SecurityKey) {
    $encodedKey = [Uri]::EscapeDataString($SecurityKey)
    $liveUrl = "$liveUrl&allowValidation=true&key=$encodedKey"
    $orderflowUrl = "$orderflowUrl&key=$encodedKey"
}

Write-Host ""
Write-Host "Bridge is live:" -ForegroundColor Green
Write-Host $tunnelUrl -ForegroundColor White
Write-Host ""
Write-Host "Hosted terminal:" -ForegroundColor Green
Write-Host $liveUrl -ForegroundColor White
Write-Host ""
Write-Host "Hosted order flow:" -ForegroundColor Green
Write-Host $orderflowUrl -ForegroundColor White
Write-Host ""

if (-not $SecurityKey) {
    Write-Host "No security key was supplied. Read-only telemetry should work, but protected validation/profile endpoints need the backend startup key." -ForegroundColor Yellow
    Write-Host "Run again with: .\Start-TBBFX-Bridge.ps1 -Key <backend-startup-key> -OpenBrowser" -ForegroundColor Yellow
    Write-Host ""
}

if ($OpenBrowser) {
    Start-Process $liveUrl
    Start-Process $orderflowUrl
}

Write-Host "Press Ctrl+C to stop the bridge." -ForegroundColor Yellow
Wait-Process -Id $process.Id
