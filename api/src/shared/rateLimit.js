const windows = new Map();

function clientKey(request) {
  return request.headers.get("x-forwarded-for")?.split(",")[0]?.trim()
    || request.headers.get("x-client-ip")
    || "anonymous";
}

function checkRateLimit(request, options = {}) {
  const limit = options.limit || 60;
  const windowMs = options.windowMs || 60_000;
  const now = Date.now();
  const key = `${options.name || "default"}:${clientKey(request)}`;
  const bucket = windows.get(key);

  if (!bucket || bucket.resetAt <= now) {
    windows.set(key, { count: 1, resetAt: now + windowMs });
    return { allowed: true, remaining: limit - 1, resetAt: now + windowMs };
  }

  bucket.count += 1;
  return {
    allowed: bucket.count <= limit,
    remaining: Math.max(0, limit - bucket.count),
    resetAt: bucket.resetAt
  };
}

module.exports = { checkRateLimit };
