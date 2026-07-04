// Public runtime configuration for Azure Static Web Apps.
// This file is intentionally safe to publish: it contains no secrets and no private MT5 endpoints.
window.TBBFX_PUBLIC_CONFIG = Object.freeze({
  publicMode: true,
  allowTrading: false,
  allowValidation: false,
  apiBase: "",
  featureFactoryBase: "",
  featureWsUrl: "",
  signalRUrl: "/api",
  signalRMode: "azure-signalr-serverless",
  signalRBase: "",
  statusLabel: "PUBLIC READ-ONLY"
});
