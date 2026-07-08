# TBBFX Cloudflare Pages Deployment Runbook

## Why Cloudflare Pages

Cloudflare Pages is the zero-budget public hosting path for the TBBFX terminal. It hosts the static `terminal/` folder for free while keeping MT5 execution, validation compute, local feature ingestion, and private broker details off the public internet.

Azure Static Web Apps support remains in the repository for a later move back to Azure when a subscription is available.

## Cloudflare Pages Settings

Use the GitHub repo:

- `karabomoleko278-cpu/TBBFX_Intelligence_Web`

Set the Pages project like this:

- Framework preset: `None`
- Build command: leave blank
- Build output directory: `terminal`
- Root directory: repository root
- Node version: not required for the static deployment

Routes included in `terminal/_redirects`:

- `/terminal` serves `index.html`
- `/orderflow` serves `orderflow/index.html`
- `/TBBFX*` serves `index.html` for legacy bookmarked links

Headers included in `terminal/_headers`:

- Content Security Policy
- frame blocking
- no MIME sniffing
- strict referrer policy
- disabled camera, microphone, geolocation, payment, and USB permissions
- no-store caching for runtime config and HTML

## Public Runtime Mode

`terminal/config.public.js` is configured as static read-only:

- `allowTrading: false`
- `allowValidation: false`
- no local MT5 endpoints
- no private feature WebSocket endpoint
- no public SignalR hub endpoint

The hosted terminal can demonstrate the public interface and simulated/read-only analytics without exposing execution controls.

## Secure Live Bridge For Your Own Devices

Cloudflare Pages is public HTTPS, so browsers will not reliably let it talk straight to `http://127.0.0.1:5000` on your laptop. To view your private running terminal data from the hosted site, run a secure tunnel to `SignalRFeatureStore` and open the hosted terminal with the tunnel URL:

```powershell
cloudflared tunnel --url http://127.0.0.1:5000
```

Then open:

```text
https://tbbfx-intelligence-web.pages.dev/?bridge=https://YOUR-TUNNEL.trycloudflare.com
https://tbbfx-intelligence-web.pages.dev/orderflow/?bridge=https://YOUR-TUNNEL.trycloudflare.com
```

The bridge URL is stored locally in the browser so refreshes keep using it. Use a fresh tunnel URL whenever you restart a quick tunnel.

Optional: if you also expose `FeatureFactory` separately, add `featureBridge`:

```text
https://tbbfx-intelligence-web.pages.dev/?bridge=https://SIGNALR-TUNNEL.trycloudflare.com&featureBridge=https://FEATUREFACTORY-TUNNEL.trycloudflare.com
```

Safety rules for live bridge mode:

- Only tunnel sanitized read-only market telemetry.
- Do not tunnel trade execution endpoints.
- Do not tunnel MT5 credentials, account details, or private settings.
- Keep `TBBFX_FEATURE_UPDATE_KEY` configured before allowing browser-origin writes through any public tunnel.

## What Stays Private

Keep these off Cloudflare Pages:

- MT5 credentials and broker account data
- trade open, close, or modify endpoints
- validation/backtest runner endpoints
- local feature ingestion endpoints
- Azure SignalR connection strings
- Function keys or API tokens
- `.env` and local settings files

## Optional Live Upgrade Later

When budget or infrastructure is available, add a separate backend that publishes sanitized read-only data to the public UI. Do not connect anonymous browser users directly to MT5 or the EA.

The existing Azure files are intentionally still present:

- `.github/workflows/azure-static-web-apps.yml`
- `terminal/staticwebapp.config.json`
- `terminal/config.azure.example.js`
- `api/`
- `docs/AZURE_DEPLOYMENT_SECURITY.md`

Those files are the path back to Azure Static Web Apps plus managed Functions later.
