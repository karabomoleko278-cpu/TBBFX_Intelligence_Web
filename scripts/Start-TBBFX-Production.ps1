param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$NginxPath = $env:TBBFX_NGINX_PATH,
    [string]$CloudflaredConfig = (Join-Path $HOME ".cloudflared\config.yml"),
    [switch]$SkipTunnel
)

$ErrorActionPreference = "Stop"

function Require-CommandPath {
    param([string]$ExplicitPath, [string]$CommandName, [string]$InstallHint)
    if ($ExplicitPath -and (Test-Path -LiteralPath $ExplicitPath)) {
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }
    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "$CommandName was not found. $InstallHint"
}

function Start-HiddenProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [hashtable]$Environment,
        [string]$LogDirectory
    )

    foreach ($entry in $Environment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, "Process")
    }

    $stdout = Join-Path $LogDirectory "$Name.out.log"
    $stderr = Join-Path $LogDirectory "$Name.err.log"
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr

    [pscustomobject]@{ Name = $Name; Id = $process.Id; Stdout = $stdout; Stderr = $stderr }
}

$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$featureFactory = Join-Path $RepositoryRoot "FeatureFactory"
$signalRStore = Join-Path $RepositoryRoot "SignalRFeatureStore"
$logDirectory = Join-Path $RepositoryRoot "tmp-runtime-logs\production"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

if (-not $env:TBBFX_FEATURE_UPDATE_KEY -or $env:TBBFX_FEATURE_UPDATE_KEY.Length -lt 32) {
    throw "Set TBBFX_FEATURE_UPDATE_KEY to a random value of at least 32 characters in this process. Never save it in the repository."
}

$python = Require-CommandPath "" "python" "Install Python 3.12 and add it to PATH."
$dotnet = Require-CommandPath "" "dotnet" "Install the .NET 10 SDK."
$nginx = Require-CommandPath $NginxPath "nginx" "Install Nginx and set TBBFX_NGINX_PATH to nginx.exe."

$nginxRoot = Split-Path -Parent $nginx
$nginxConfig = Join-Path $RepositoryRoot "infra\nginx\tbbfx-production.conf.example"
$cloudflared = $null
if (-not $SkipTunnel) {
    $knownCloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    $cloudflared = Require-CommandPath $knownCloudflared "cloudflared" "Install cloudflared and create a named tunnel."
    if (-not (Test-Path -LiteralPath $CloudflaredConfig)) {
        throw "Named tunnel config not found at $CloudflaredConfig. Quick tunnels are intentionally rejected for production."
    }
}

$baseEnvironment = @{
    TBBFX_PRODUCTION_MODE = "1"
    TBBFX_PUBLIC_READONLY = "1"
    TBBFX_PUBLIC_RATE_LIMIT_PER_MINUTE = "60"
    TBBFX_FEATURE_UPDATE_KEY = $env:TBBFX_FEATURE_UPDATE_KEY
    SIGNALR_URL = "http://127.0.0.1:5000/features/update"
}

$processes = @()
$processes += Start-HiddenProcess "signalr-store" $dotnet @("run", "--no-launch-profile", "--project", $signalRStore) $RepositoryRoot $baseEnvironment $logDirectory

$roles = @(
    @{ Name = "feature-leader"; Port = "8000"; Role = "leader"; Streams = "1"; News = "1" },
    @{ Name = "feature-macro-replica"; Port = "8001"; Role = "macro-replica"; Streams = "0"; News = "1" },
    @{ Name = "feature-api-replica"; Port = "8002"; Role = "api-replica"; Streams = "0"; News = "0" }
)

foreach ($role in $roles) {
    $environment = @{} + $baseEnvironment
    $environment.TBBFX_PROCESS_ROLE = $role.Role
    $environment.TBBFX_RUN_STREAM_PROCESSORS = $role.Streams
    $environment.TBBFX_RUN_NEWS_AGGREGATOR = $role.News
    $processes += Start-HiddenProcess $role.Name $python @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", $role.Port) $featureFactory $environment $logDirectory
}

Start-Sleep -Seconds 3
& $nginx -p "$nginxRoot\" -c $nginxConfig -t
if ($LASTEXITCODE -ne 0) { throw "Nginx validation failed. Review the generated logs before exposing the gateway." }
$processes += Start-HiddenProcess "nginx" $nginx @("-p", "$nginxRoot\", "-c", $nginxConfig) $nginxRoot @{} $logDirectory

if (-not $SkipTunnel) {
    $processes += Start-HiddenProcess "cloudflared" $cloudflared @("tunnel", "--config", $CloudflaredConfig, "run") $RepositoryRoot @{} $logDirectory
}

$manifest = Join-Path $logDirectory "processes.json"
$processes | ConvertTo-Json | Set-Content -LiteralPath $manifest -Encoding utf8

Write-Host "TBBFX production services started behind http://127.0.0.1:8080" -ForegroundColor Green
Write-Host "Process manifest: $manifest" -ForegroundColor DarkGray
Write-Host "Secrets remain process-local and were not written to disk." -ForegroundColor Yellow
