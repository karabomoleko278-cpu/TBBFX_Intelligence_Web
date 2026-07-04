const { app } = require("@azure/functions");
const { checkRateLimit } = require("../shared/rateLimit");

app.http("health", {
  methods: ["GET"],
  authLevel: "anonymous",
  route: "health",
  handler: async (request) => {
    const rate = checkRateLimit(request, { name: "health", limit: 60, windowMs: 60_000 });
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

    return {
      status: 200,
      headers: {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"
      },
      jsonBody: {
        status: "online",
        service: "TBBFX Public Terminal API",
        mode: "public-read-only",
        trading: "disabled",
        validation: "disabled",
        privateEndpoints: [
          "/features/update",
          "/features/all",
          "/api/live-scanner/*",
          "/api/validation/*",
          "/hub/*"
        ],
        timestamp: new Date().toISOString()
      }
    };
  }
});
