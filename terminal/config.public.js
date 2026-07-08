// Public runtime configuration for free static hosting such as Cloudflare Pages.
// This file is intentionally safe to publish: it contains no secrets and no private MT5 endpoints.
//
// Hosted HTTPS pages cannot reliably connect straight to http://127.0.0.1 because
// modern browsers block public-to-private network access. For live terminal data,
// run a secure tunnel to SignalRFeatureStore and open the site with:
//   ?bridge=https://your-tunnel.trycloudflare.com
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

window.TBBFX_PUBLIC_CONFIG = Object.freeze({
  publicMode: true,
  allowTrading: false,
  allowValidation: false,
  apiBase: "",
  featureFactoryBase: "",
  featureWsUrl: "",
  signalRUrl: "",
  signalRMode: "static-readonly",
  signalRBase: "",
  statusLabel: "PUBLIC READ-ONLY",
  secureBridge: TBBFX_SECURE_BRIDGE,
  localBridge: TBBFX_LOCAL_BRIDGE
});
