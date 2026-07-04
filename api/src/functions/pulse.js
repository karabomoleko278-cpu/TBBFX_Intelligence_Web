const { app, output } = require("@azure/functions");
const { checkRateLimit } = require("../shared/rateLimit");

const signalRMessages = output.generic({
  type: "signalR",
  name: "signalRMessages",
  hubName: "marketpulse",
  connectionStringSetting: "AzureSignalRConnectionString"
});

function normalizePayload(payload) {
  const symbol = String(payload?.symbol || payload?.Symbol || "").trim().toUpperCase();
  if (!symbol || !/^[A-Z0-9]{2,12}$/.test(symbol)) {
    return null;
  }

  return {
    ...payload,
    symbol,
    timestamp: payload?.timestamp || payload?.Timestamp || new Date().toISOString()
  };
}

app.http("pulse", {
  methods: ["POST"],
  authLevel: "function",
  route: "pulse",
  extraOutputs: [signalRMessages],
  handler: async (request, context) => {
    const rate = checkRateLimit(request, { name: "pulse", limit: 240, windowMs: 60_000 });
    if (!rate.allowed) {
      return {
        status: 429,
        headers: {
          "Cache-Control": "no-store",
          "Retry-After": String(Math.ceil((rate.resetAt - Date.now()) / 1000))
        },
        jsonBody: { error: "Rate limit exceeded" }
      };
    }

    let payload;
    try {
      payload = normalizePayload(await request.json());
    } catch (_) {
      payload = null;
    }

    if (!payload) {
      return {
        status: 400,
        headers: { "Cache-Control": "no-store" },
        jsonBody: { error: "A valid symbol payload is required." }
      };
    }

    context.extraOutputs.set(signalRMessages, [
      { target: "ReceiveAnyPulse", arguments: [payload] },
      { target: "ReceivePulse", arguments: [payload] },
      { target: "ReceiveFeatureUpdate", arguments: [payload] },
      { target: "ReceiveAnyUpdate", arguments: [payload] }
    ]);

    return {
      status: 202,
      headers: {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"
      },
      jsonBody: {
        accepted: true,
        symbol: payload.symbol,
        broadcastTargets: ["ReceiveAnyPulse", "ReceivePulse", "ReceiveFeatureUpdate", "ReceiveAnyUpdate"]
      }
    };
  }
});
