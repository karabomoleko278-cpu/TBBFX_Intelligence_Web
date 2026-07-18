param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$NginxPath = $env:TBBFX_NGINX_PATH,
    [string]$CloudflaredConfig = (Join-Path $HOME ".cloudflared\config.yml"),
    [switch]$PreflightOnly,
    [switch]$SkipTunnel
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Resolve-CommandPath {
    param(
        [string]$ExplicitPath,
        [string]$CommandName,
        [string[]]$KnownPaths = @()
    )

    if ($ExplicitPath -and (Test-Path -LiteralPath $ExplicitPath)) {
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }

    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    foreach ($path in $KnownPaths) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            return (Resolve-Path -LiteralPath $path).Path
        }
    }

    return $null
}

function Invoke-SecurityScan {
    param([string]$PythonPath, [string]$Root)

    $scanner = Join-Path $Root "scripts\security\secret_scan.py"
    if (-not (Test-Path -LiteralPath $scanner)) {
        throw "Secret scanner is missing: $scanner"
    }

    foreach ($scope in @("--worktree", "--staged", "--history")) {
        & $PythonPath $scanner $scope
        if ($LASTEXITCODE -ne 0) {
            throw "[SECURITY ALERT] Secret scan failed for $scope. Production startup was stopped before any service launched."
        }
    }
}

function Test-TcpPortAvailable {
    param([int]$Port)

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $Port
        )
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($listener) { $listener.Stop() }
    }
}

function Test-NamedTunnelConfiguration {
    param([string]$ConfigPath)

    $issues = [System.Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        $issues.Add("Named Cloudflare Tunnel config not found at $ConfigPath.")
        return $issues
    }

    $raw = Get-Content -LiteralPath $ConfigPath -Raw
    if ($raw -match "REPLACE_WITH|your-tbbfx-domain\.example") {
        $issues.Add("Named tunnel config still contains template placeholders.")
    }

    $tunnelLine = Get-Content -LiteralPath $ConfigPath |
        Where-Object { $_ -match '^\s*tunnel\s*:' } |
        Select-Object -First 1
    if (-not $tunnelLine) {
        $issues.Add("Named tunnel config is missing the tunnel identifier.")
    }

    $credentialLine = Get-Content -LiteralPath $ConfigPath |
        Where-Object { $_ -match '^\s*credentials-file\s*:' } |
        Select-Object -First 1
    if (-not $credentialLine) {
        $issues.Add("Named tunnel config is missing credentials-file.")
    }
    else {
        $credentialPath = (($credentialLine -split ':', 2)[1]).Trim().Trim('"').Trim("'")
        if (-not [System.IO.Path]::IsPathRooted($credentialPath)) {
            $credentialPath = Join-Path (Split-Path -Parent $ConfigPath) $credentialPath
        }
        if (-not (Test-Path -LiteralPath $credentialPath)) {
            $issues.Add("Named tunnel credentials file does not exist: $credentialPath")
        }
    }

    if ($raw -notmatch 'service:\s*http://127\.0\.0\.1:8787') {
        $issues.Add("Named tunnel ingress must target http://127.0.0.1:8787.")
    }
    if ($raw -notmatch '(?m)^\s*-\s*service:\s*http_status:404\s*$') {
        $issues.Add("Named tunnel config must finish with a fail-closed http_status:404 ingress rule.")
    }

    return $issues
}

function Initialize-NginxRuntime {
    param([string]$NginxExecutable, [string]$Root)

    $runtime = Join-Path $Root "tmp-runtime-logs\production\nginx-runtime"
    $runtimeConf = Join-Path $runtime "conf"
    foreach ($relativePath in @(
        "logs",
        "cache\tbbfx_macro",
        "temp\client_body_temp",
        "temp\proxy_temp",
        "temp\fastcgi_temp",
        "temp\uwsgi_temp",
        "temp\scgi_temp"
    )) {
        New-Item -ItemType Directory -Path (Join-Path $runtime $relativePath) -Force | Out-Null
    }
    New-Item -ItemType Directory -Path $runtimeConf -Force | Out-Null

    $nginxRoot = Split-Path -Parent $NginxExecutable
    $mimeCandidates = @(
        (Join-Path $nginxRoot "conf\mime.types"),
        (Join-Path $nginxRoot "mime.types")
    )
    $mimeTypes = $mimeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $mimeTypes) {
        throw "Nginx mime.types was not found next to $NginxExecutable."
    }

    Copy-Item -LiteralPath $mimeTypes -Destination (Join-Path $runtimeConf "mime.types") -Force
    return $runtime
}

function Test-NginxConfiguration {
    param(
        [string]$NginxExecutable,
        [string]$RuntimeRoot,
        [string]$ConfigPath
    )

    # Nginx treats a trailing Windows backslash as an escaped quote. A forward-
    # slash prefix keeps paths with spaces as one argument without corrupting -c.
    $nginxPrefix = ($RuntimeRoot -replace '\\', '/').TrimEnd('/') + '/'
    $runtimeConfig = Join-Path $RuntimeRoot "conf\tbbfx-production.conf"
    Copy-Item -LiteralPath $ConfigPath -Destination $runtimeConfig -Force
    & $NginxExecutable "-p" $nginxPrefix "-c" "conf/tbbfx-production.conf" "-t"
    if ($LASTEXITCODE -ne 0) {
        throw "Nginx configuration validation failed. Nothing was exposed."
    }
}

function Start-HiddenProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [hashtable]$Environment = @{},
        [string]$LogDirectory
    )

    $previous = @{}
    try {
        foreach ($entry in $Environment.GetEnumerator()) {
            $previous[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, "Process")
            [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, "Process")
        }

        $stdout = Join-Path $LogDirectory "$Name.out.log"
        $stderr = Join-Path $LogDirectory "$Name.err.log"
        $processArguments = foreach ($argument in $Arguments) {
            $value = [string]$argument
            if ($value -match '\s') { '"' + $value.Replace('"', '\"') + '"' } else { $value }
        }
        $startParameters = @{
            FilePath = $FilePath
            WorkingDirectory = $WorkingDirectory
            WindowStyle = "Hidden"
            PassThru = $true
            RedirectStandardOutput = $stdout
            RedirectStandardError = $stderr
        }
        if (@($processArguments).Count -gt 0) {
            $startParameters.ArgumentList = $processArguments
        }

        $process = Start-Process @startParameters

        return [pscustomobject]@{
            Name = $Name
            Id = $process.Id
            Stdout = $stdout
            Stderr = $stderr
        }
    }
    finally {
        foreach ($entry in $Environment.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable($entry.Key, $previous[$entry.Key], "Process")
        }
    }
}

function Wait-HttpEndpoint {
    param(
        [string]$Uri,
        [int]$TimeoutSeconds = 45,
        [int[]]$ExpectedStatus = @(200)
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -Method Get -UseBasicParsing -TimeoutSec 3
            if ($ExpectedStatus -contains [int]$response.StatusCode) { return $response }
        }
        catch {
            $status = $null
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
            if ($status -and ($ExpectedStatus -contains $status)) { return $_.Exception.Response }
        }
        Start-Sleep -Milliseconds 600
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Timed out waiting for $Uri."
}

function Initialize-MacroWorkerCache {
    param([int]$Port)

    # Each Uvicorn worker owns an isolated in-memory cache. Warm every replica
    # before Nginx can route public traffic to prevent first-request provider
    # latency from surfacing as a gateway timeout.
    $endpoints = @(
        "/api/macro/cot-positioning?symbol=XAUUSD",
        "/api/macro/liquidity-index",
        "/api/macro/yield-spreads",
        "/api/macro/yield-curve",
        "/api/macro/validation-suite?symbol=XAUUSD"
    )

    foreach ($endpoint in $endpoints) {
        Wait-HttpEndpoint "http://127.0.0.1:${Port}${endpoint}" -TimeoutSeconds 75 | Out-Null
    }

    Write-Host "Macro cache warmed on worker port $Port." -ForegroundColor DarkGray
}

function Assert-PublicWriteBlocked {
    param([string]$Uri)

    try {
        Invoke-WebRequest -Uri $Uri -Method Post -Body '{}' -ContentType "application/json" `
            -UseBasicParsing -TimeoutSec 5 | Out-Null
        throw "Public gateway unexpectedly accepted POST $Uri."
    }
    catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 403) { return }
        if ($_.Exception.Message -like "Public gateway unexpectedly*") { throw }
        throw "Could not verify the public write guardrail at ${Uri}: $($_.Exception.Message)"
    }
}

function Stop-StartedProcesses {
    param([object[]]$Started)

    for ($index = $Started.Count - 1; $index -ge 0; $index--) {
        $entry = $Started[$index]
        try { Stop-Process -Id $entry.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}

$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$featureFactory = Join-Path $RepositoryRoot "FeatureFactory"
$signalRDirectory = Join-Path $RepositoryRoot "SignalRFeatureStore"
$signalRProject = Join-Path $signalRDirectory "SignalRFeatureStore.csproj"
$signalRExecutable = Join-Path $signalRDirectory "bin\Release\net10.0\SignalRFeatureStore.exe"
$nginxConfig = Join-Path $RepositoryRoot "infra\nginx\tbbfx-production.conf"
$logDirectory = Join-Path $RepositoryRoot "tmp-runtime-logs\production"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

$python = Resolve-CommandPath "" "python"
$dotnet = Resolve-CommandPath "" "dotnet"
$nginx = Resolve-CommandPath $NginxPath "nginx" @(
    (Join-Path $env:LOCALAPPDATA "TBBFX\nginx-1.31.3\nginx.exe"),
    "C:\nginx\nginx.exe",
    "C:\Program Files\nginx\nginx.exe"
)
$cloudflared = $null
if (-not $SkipTunnel) {
    $cloudflared = Resolve-CommandPath "" "cloudflared" @(
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe"
    )
}

$blockers = [System.Collections.Generic.List[string]]::new()
if (-not (Test-Path -LiteralPath $featureFactory)) { $blockers.Add("FeatureFactory directory is missing.") }
if (-not (Test-Path -LiteralPath $signalRProject)) { $blockers.Add("SignalRFeatureStore project is missing.") }
if (-not (Test-Path -LiteralPath $nginxConfig)) { $blockers.Add("Active Nginx config is missing: $nginxConfig") }
if (-not $python) { $blockers.Add("Python was not found on PATH.") }
if (-not $dotnet) { $blockers.Add("The .NET SDK was not found on PATH.") }
if (-not $nginx) { $blockers.Add("Nginx was not found. Install it or set TBBFX_NGINX_PATH to nginx.exe.") }
if (-not $SkipTunnel) {
    if (-not $cloudflared) { $blockers.Add("cloudflared was not found.") }
    foreach ($issue in (Test-NamedTunnelConfiguration $CloudflaredConfig)) { $blockers.Add($issue) }
}

if ($python) {
    try {
        Write-Host "Running repository secret gates..." -ForegroundColor Cyan
        Invoke-SecurityScan $python $RepositoryRoot
    }
    catch {
        $blockers.Add($_.Exception.Message)
    }
}

$nginxRuntime = $null
if ($nginx -and (Test-Path -LiteralPath $nginxConfig)) {
    try {
        $nginxRuntime = Initialize-NginxRuntime $nginx $RepositoryRoot
        Test-NginxConfiguration $nginx $nginxRuntime $nginxConfig
    }
    catch {
        $blockers.Add($_.Exception.Message)
    }
}

if (-not $PreflightOnly) {
    if (-not $env:TBBFX_FEATURE_UPDATE_KEY -or $env:TBBFX_FEATURE_UPDATE_KEY.Length -lt 32) {
        $blockers.Add("Set TBBFX_FEATURE_UPDATE_KEY to a random value of at least 32 characters in this PowerShell process. Never save it in the repository.")
    }
    foreach ($port in @(5000, 8100, 8101, 8102, 8787)) {
        if (-not (Test-TcpPortAvailable $port)) {
            $blockers.Add("Loopback port $port is already in use. Stop the existing development service before launching this isolated production stack.")
        }
    }
}

if ($blockers.Count -gt 0) {
    Write-Host "TBBFX production preflight blocked safely:" -ForegroundColor Yellow
    foreach ($blocker in $blockers) { Write-Host " - $blocker" -ForegroundColor Yellow }
    throw "Production preflight failed with $($blockers.Count) blocker(s). No public tunnel was launched."
}

Write-Host "Compiling the production feature store..." -ForegroundColor Cyan
& $dotnet build $signalRProject --configuration Release --no-restore
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $signalRExecutable)) {
    throw "SignalRFeatureStore Release build failed. No service or public tunnel was launched."
}

Write-Host "Security, dependency, Nginx, and tunnel preflight checks passed." -ForegroundColor Green
if ($PreflightOnly) {
    Write-Host "Preflight-only mode completed. No services or tunnel were started." -ForegroundColor Cyan
    exit 0
}

$baseEnvironment = @{
    TBBFX_PRODUCTION_MODE = "1"
    TBBFX_PUBLIC_READONLY = "1"
    TBBFX_PUBLIC_RATE_LIMIT_PER_MINUTE = "60"
    TBBFX_FEATURE_UPDATE_KEY = $env:TBBFX_FEATURE_UPDATE_KEY
    SIGNALR_URL = "http://127.0.0.1:5000/features/update"
    ASPNETCORE_URLS = "http://127.0.0.1:5000"
    ENABLE_STARTUP_SCANNER = "0"
}

$processes = @()
try {
    $processes += Start-HiddenProcess -Name "signalr-store" -FilePath $signalRExecutable `
        -Arguments @() -WorkingDirectory (Split-Path -Parent $signalRExecutable) `
        -Environment $baseEnvironment -LogDirectory $logDirectory
    Wait-HttpEndpoint "http://127.0.0.1:5000/" | Out-Null

    $roles = @(
        @{ Name = "feature-leader"; Port = "8100"; Role = "leader"; Streams = "1"; News = "1" },
        @{ Name = "feature-macro-replica"; Port = "8101"; Role = "macro-replica"; Streams = "0"; News = "1" },
        @{ Name = "feature-api-replica"; Port = "8102"; Role = "api-replica"; Streams = "0"; News = "0" }
    )

    foreach ($role in $roles) {
        $environment = @{} + $baseEnvironment
        $environment.TBBFX_PROCESS_ROLE = $role.Role
        $environment.TBBFX_RUN_STREAM_PROCESSORS = $role.Streams
        $environment.TBBFX_RUN_NEWS_AGGREGATOR = $role.News
        $processes += Start-HiddenProcess -Name $role.Name -FilePath $python `
            -Arguments @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", $role.Port) `
            -WorkingDirectory $featureFactory -Environment $environment -LogDirectory $logDirectory
        Wait-HttpEndpoint "http://127.0.0.1:$($role.Port)/" | Out-Null
    }

    foreach ($role in $roles) {
        Initialize-MacroWorkerCache -Port ([int]$role.Port)
    }

    $nginxPrefix = ($nginxRuntime -replace '\\', '/').TrimEnd('/') + '/'
    $processes += Start-HiddenProcess -Name "nginx" -FilePath $nginx `
        -Arguments @("-p", $nginxPrefix, "-c", "conf/tbbfx-production.conf") `
        -WorkingDirectory $nginxRuntime -Environment @{} -LogDirectory $logDirectory
    Wait-HttpEndpoint "http://127.0.0.1:8787/healthz" | Out-Null
    Wait-HttpEndpoint "http://127.0.0.1:8787/api/macro/validation-suite?symbol=XAUUSD" | Out-Null
    Assert-PublicWriteBlocked "http://127.0.0.1:8787/api/macro/validation-suite?symbol=XAUUSD"

    if (-not $SkipTunnel) {
        $processes += Start-HiddenProcess -Name "cloudflared" -FilePath $cloudflared `
            -Arguments @("tunnel", "--config", $CloudflaredConfig, "run") `
            -WorkingDirectory $RepositoryRoot -Environment @{} -LogDirectory $logDirectory
        Start-Sleep -Seconds 2
        $tunnelProcess = Get-Process -Id $processes[-1].Id -ErrorAction SilentlyContinue
        if (-not $tunnelProcess) {
            throw "Named Cloudflare Tunnel exited during startup. Review cloudflared.err.log."
        }
    }

    $manifest = Join-Path $logDirectory "processes.json"
    $processes | ConvertTo-Json | Set-Content -LiteralPath $manifest -Encoding utf8

    Write-Host "TBBFX public read-only gateway is healthy at http://127.0.0.1:8787" -ForegroundColor Green
    if ($SkipTunnel) {
        Write-Host "Tunnel launch was skipped; the gateway remains loopback-only for local verification." -ForegroundColor Yellow
    }
    else {
        Write-Host "Named Cloudflare Tunnel process is running behind the validated read-only gateway." -ForegroundColor Green
    }
    Write-Host "Process manifest: $manifest" -ForegroundColor DarkGray
    Write-Host "Secrets remain process-local and were not written to disk or command arguments." -ForegroundColor Yellow
}
catch {
    Stop-StartedProcesses $processes
    throw
}
