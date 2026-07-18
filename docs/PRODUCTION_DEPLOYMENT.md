# TBBFX Hardened Production Deployment

## Security Boundary

Cloudflare Pages hosts only the static terminal. MT5 credentials, execution
endpoints, broker details, database files, feature write keys, and `.env` files
remain private. The production backend refuses to start without an explicit
`TBBFX_FEATURE_UPDATE_KEY`.

The repository has three secret gates:

1. `.gitignore` denies environment files, certificates, tunnel credentials,
   private settings, databases, and common credential files.
2. `.githooks/pre-commit` scans staged content before every commit.
3. `.github/workflows/security-gate.yml` scans the worktree and complete reachable
   Git history on pushes and pull requests.

Install the local hook once per clone:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Install-GitHooks.ps1
```

Run the gate manually:

```powershell
python .\scripts\security\secret_scan.py --worktree --staged --history
```

If the scanner reports a secret, stop. Unstage it with `git restore --staged --
<path>` (or `git reset HEAD -- <path>`), remove it from tracking with `git rm
--cached -- <path>` where appropriate, rotate the exposed credential, then scan
again. Do not rewrite shared history automatically; coordinate a deliberate purge
when a secret has already been pushed.

## Request Controls

- FastAPI enforces 60 public analytical requests per minute per client IP.
- SignalRFeatureStore enforces 60 public reads per minute per client IP.
- Nginx adds a second 1 request/second layer with a controlled burst.
- Remote writes remain key-protected and are never cached.
- HTTP 429 responses use the `TbbFxObject` envelope in MessagePack or JSON.
- Forwarded client addresses are trusted only when the direct peer is a local or
  private reverse proxy, preventing arbitrary public header spoofing.

## Load-Balanced Runtime

The production launcher starts:

- one stateful SignalR feature store on port 5000;
- one FeatureFactory stream leader on port 8100;
- one macro replica on port 8101;
- one stateless API replica on port 8102;
- Nginx on loopback port 8787 using least-connections load balancing;
- an optional named Cloudflare Tunnel to the Nginx gateway.

Only the leader starts live stream processors. This avoids duplicate MT5 stream
consumption while still allowing analytical reads to fail over between replicas.

## Named Tunnel Requirement

Cloudflare Quick Tunnels are development-only and are intentionally rejected by
the production launcher. Create a named tunnel in the personal Cloudflare account,
copy `infra/cloudflare/tunnel-config.yml.example` to the user `.cloudflared`
directory, and keep the generated credentials JSON outside this repository.

## Start

Set the secret only in the current PowerShell process:

```powershell
$env:TBBFX_FEATURE_UPDATE_KEY = -join ((1..48) | ForEach-Object { [char](Get-Random -Minimum 33 -Maximum 126) })
$env:TBBFX_NGINX_PATH = "C:\path\to\nginx.exe"
powershell -ExecutionPolicy Bypass -File .\scripts\Start-TBBFX-Production.ps1
```

The launcher writes only PIDs and logs to the ignored `tmp-runtime-logs` folder.
It never persists the feature update key.

## Cache Policy

The origin emits `CDN-Cache-Control` for public analytical data:

- 12 hours: FRED liquidity, CFTC positioning, yield curve.
- 5 minutes: current yields, sentiment, calendar, geopolitical nodes.
- no-store: order flow, SignalR, feature writes, validation, authenticated or
  key-bearing requests, and errors.

Apply the matching Cloudflare rules from `infra/cloudflare/CACHE_RULES.md` on the
dedicated read-only API hostname. The app-level TTL cache and request limiter stay
active even if the Cloudflare account plan does not provide advanced WAF controls.

## Strategy Isolation

This deployment layer does not import or mutate execution strategy configuration.
Symbol risk tiers, Target R = 4.00, the one-trade-per-day policy, and H1 FVG/OTE
stop boundaries remain outside the proxy, cache, and rate-limiting code paths.
