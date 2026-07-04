const { app, input } = require("@azure/functions");
const { checkRateLimit } = require("../shared/rateLimit");

const signalRConnectionInfo = input.generic({
  type: "signalRConnectionInfo",
  name: "connectionInfo",
  hubName: "marketpulse",
  connectionStringSetting: "AzureSignalRConnectionString"
});

app.http("negotiate", {
  methods: ["GET", "POST"],
  authLevel: "anonymous",
  route: "negotiate",
  extraInputs: [signalRConnectionInfo],
  handler: async (request, context) => {
    const rate = checkRateLimit(request, { name: "negotiate", limit: 60, windowMs: 60_000 });
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

    const connectionInfo = context.extraInputs.get(signalRConnectionInfo);
    return {
      status: 200,
      headers: {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"
      },
      jsonBody: connectionInfo
    };
  }
});
