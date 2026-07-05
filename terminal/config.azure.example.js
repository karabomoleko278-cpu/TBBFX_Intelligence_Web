// Azure Static Web Apps runtime config example.
// Rename or copy this over config.public.js when you move back to Azure SignalR serverless hosting.
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
