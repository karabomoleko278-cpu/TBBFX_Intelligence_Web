// Public runtime configuration for free static hosting such as Cloudflare Pages.
// This file is intentionally safe to publish: it contains no secrets and no private MT5 endpoints.
//
// The local bridge is opt-in: add ?local=1 to the public URL while the local
// SignalRFeatureStore and FeatureFactory terminals are running on this laptop.
// Without that query flag the hosted site remains public read-only.
const TBBFX_LOCAL_BRIDGE = Object.freeze({
  enabledByQuery: true,
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
  localBridge: TBBFX_LOCAL_BRIDGE
});
