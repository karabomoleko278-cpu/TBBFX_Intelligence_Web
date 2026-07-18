[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Push-Location $repo
try {
    git config core.hooksPath .githooks
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to configure the repository hook path.'
    }

    & python (Join-Path $repo 'scripts\security\secret_scan.py') --worktree
    if ($LASTEXITCODE -ne 0) {
        throw 'The initial secret scan failed. Git hooks remain configured, but commits are blocked.'
    }

    Write-Host '[SECURITY] TBBFX pre-commit secret gate installed.' -ForegroundColor Green
}
finally {
    Pop-Location
}
