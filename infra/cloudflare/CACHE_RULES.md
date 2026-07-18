# Cloudflare Cache Rules

Create these rules only on the dedicated read-only API hostname. Never cache any
request containing `Authorization`, `X-TBBFX-KEY`, `X-TBBFX-FEATURE-KEY`, or a
`key` query parameter.

## Low-Frequency Macro

- Match paths `/api/macro/liquidity-index`, `/api/macro/cot-positioning`, and `/api/macro/yield-curve`.
- Eligible for cache: yes.
- Edge TTL: 12 hours.
- Browser TTL: respect origin (`max-age=300`).
- Cache key: include full query string.

## Current Macro

- Match `/api/macro/yield-spreads`, `/api/macro/sentiment`, `/api/macro/calendar`, `/api/macro/geopolitical-feed`, and `/api/macro/geospatial-nodes`.
- Eligible for cache: yes.
- Edge TTL: 5 minutes.
- Browser TTL: respect origin (`max-age=30`).
- Cache key: include full query string.

## Live and Private Paths

- Bypass cache for `/features/*`, `/hub/*`, `/api/orderflow/*`, `/api/market/*`,
  validation, optimization, profile, write, WebSocket, and MessagePack live-feed paths.
- Respect `Cache-Control: private, no-store` and `CDN-Cache-Control: no-store`.
- Keep authenticated and key-bearing requests out of shared cache storage.

The FastAPI and SignalR application rate limiters remain authoritative even when
Cloudflare rate-limiting features are unavailable on the selected account plan.
