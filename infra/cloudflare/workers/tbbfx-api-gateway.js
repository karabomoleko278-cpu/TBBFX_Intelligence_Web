const PRODUCTION_ORIGIN = "https://tbbfx-intelligence-web.pages.dev";
const PREVIEW_ORIGIN_PATTERN =
  /^https:\/\/[a-z0-9-]+\.tbbfx-intelligence-web\.pages\.dev$/i;
const READ_ONLY_METHODS = new Set(["GET", "HEAD"]);

function isAllowedOrigin(origin) {
  return origin === PRODUCTION_ORIGIN || PREVIEW_ORIGIN_PATTERN.test(origin);
}

function applyPublicHeaders(headers, origin) {
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  headers.append("Vary", "Origin");

  if (origin && isAllowedOrigin(origin)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Accept, Content-Type");
    headers.set("Access-Control-Max-Age", "86400");
  }
}

function envelopeResponse(status, warning, request) {
  const origin = request.headers.get("Origin") || "";
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  applyPublicHeaders(headers, origin);

  return new Response(
    JSON.stringify({
      id: crypto.randomUUID(),
      results: [],
      provider: "cloudflare_worker_vpc_gateway",
      warnings: [warning],
      extra: {
        route: new URL(request.url).pathname,
        timestamp: new Date().toISOString(),
        mode: "public_read_only",
      },
    }),
    { status, headers },
  );
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";

    if (request.method === "OPTIONS") {
      if (!origin || !isAllowedOrigin(origin)) {
        return envelopeResponse(403, "Origin is not permitted.", request);
      }

      const headers = new Headers();
      applyPublicHeaders(headers, origin);
      return new Response(null, { status: 204, headers });
    }

    if (!READ_ONLY_METHODS.has(request.method)) {
      return envelopeResponse(
        405,
        "Public gateway is read-only. State-changing methods are disabled.",
        request,
      );
    }

    const sourceUrl = new URL(request.url);
    const upstreamUrl = new URL(
      `${sourceUrl.pathname}${sourceUrl.search}`,
      "http://localhost:8787",
    );
    const upstreamHeaders = new Headers(request.headers);

    // Public callers cannot forward execution credentials into the private network.
    for (const header of [
      "Authorization",
      "Cookie",
      "X-TBBFX-FEATURE-KEY",
      "X-TBBFX-KEY",
      "CF-Access-Jwt-Assertion",
    ]) {
      upstreamHeaders.delete(header);
    }

    upstreamHeaders.set("Host", "localhost");
    upstreamHeaders.set("X-Forwarded-Host", sourceUrl.host);
    upstreamHeaders.set("X-Forwarded-Proto", "https");

    try {
      const upstream = await env.TBBFX_API.fetch(upstreamUrl.toString(), {
        method: request.method,
        headers: upstreamHeaders,
        redirect: "manual",
      });
      const responseHeaders = new Headers(upstream.headers);
      applyPublicHeaders(responseHeaders, origin);

      return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: responseHeaders,
      });
    } catch (error) {
      console.error("TBBFX VPC gateway failure", error);
      return envelopeResponse(
        502,
        "Private market-data gateway is temporarily unavailable.",
        request,
      );
    }
  },
};
