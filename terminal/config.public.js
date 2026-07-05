// Public runtime configuration for free static hosting such as Cloudflare Pages.
// This file is intentionally safe to publish: it contains no secrets and no private MT5 endpoints.
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
  statusLabel: "PUBLIC READ-ONLY"
});
