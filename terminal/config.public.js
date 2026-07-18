// Public runtime configuration for free static hosting such as Cloudflare Pages.
// This file is intentionally safe to publish: it contains no secrets and no private MT5 endpoints.
//
// Hosted HTTPS pages use the read-only Cloudflare Worker/VPC gateway below.
// Private execution credentials and state-changing methods never cross this route.
// An explicit bridge query remains available for authenticated operator sessions:
//   ?bridge=https://your-secure-operator-bridge.example
// Optional FeatureFactory tunnel override:
//   &featureBridge=https://your-feature-tunnel.trycloudflare.com
const TBBFX_SECURE_BRIDGE = Object.freeze({
  queryParam: "bridge",
  featureQueryParam: "featureBridge",
  storageKey: "tbbfx.secureBridgeUrl",
  featureStorageKey: "tbbfx.featureBridgeUrl",
  statusLabel: "SECURE TERMINAL BRIDGE"
});

const TBBFX_LOCAL_BRIDGE = Object.freeze({
  enabledByQuery: true,
  autoDetect: false,
  queryParam: "local",
  queryValue: "1",
  featureFactoryBase: "http://127.0.0.1:8000",
  featureWsUrl: "ws://127.0.0.1:8000/ws/features",
  signalRUrl: "http://127.0.0.1:5000/hub/marketpulse",
  apiBase: "http://127.0.0.1:5000",
  signalRMode: "aspnetcore-hub",
  allowTrading: false,
  allowValidation: false,
  statusLabel: "LOCAL TERMINAL BRIDGE"
});

const isLocal = window.location.hostname === "localhost" || 
                window.location.hostname === "127.0.0.1" || 
                window.location.protocol === "file:";

const TBBFX_PUBLIC_API_BASE =
  "https://tbbfx-production-api.karabomoleko278.workers.dev";

window.TBBFX_PUBLIC_CONFIG = Object.freeze({
  publicMode: !isLocal,
  allowTrading: isLocal,
  allowValidation: isLocal,
  apiBase: TBBFX_PUBLIC_API_BASE,
  featureFactoryBase: TBBFX_PUBLIC_API_BASE,
  featureWsUrl: "",
  signalRUrl: "",
  signalRMode: isLocal ? "aspnetcore-hub" : "static-readonly",
  signalRBase: "",
  statusLabel: isLocal ? "LOCAL DEV SYSTEM" : "PUBLIC READ-ONLY",
  secureBridge: TBBFX_SECURE_BRIDGE,
  localBridge: TBBFX_LOCAL_BRIDGE
});
