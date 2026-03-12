function send(res, status, payload) {
  res.status(status).json(payload);
}

function setCommonHeaders(res) {
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "no-referrer");
  res.setHeader("X-Frame-Options", "DENY");
  res.setHeader("Cross-Origin-Resource-Policy", "same-origin");
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  res.setHeader("Origin-Agent-Cluster", "?1");
}

function normalizeUserId(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 80);
}

function isValidDailyKey(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(String(value || "").trim());
}

function getClientIp(req) {
  const xff = String(req.headers["x-forwarded-for"] || "").trim();
  if (xff) return xff.split(",")[0].trim();
  return String(req.headers["x-real-ip"] || "unknown").trim();
}

function normalizeOrigin(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const u = new URL(raw);
    return `${u.protocol}//${u.host}`;
  } catch {
    return "";
  }
}

function expectedOriginsFromHost(host) {
  const cleaned = String(host || "").trim().toLowerCase();
  if (!cleaned) return [];
  return [`https://${cleaned}`];
}

function isSameSiteRequest(req) {
  const host = String(req.headers.host || "").trim();
  const allowed = expectedOriginsFromHost(host);
  if (!allowed.length) return false;
  const origin = normalizeOrigin(req.headers.origin);
  if (origin) return allowed.includes(origin);
  const referer = normalizeOrigin(req.headers.referer);
  if (referer) return allowed.includes(referer);
  return false;
}

function computeStats(record) {
  const completed = (record && typeof record === "object" && record.completed && typeof record.completed === "object")
    ? record.completed
    : {};
  const keys = Object.keys(completed).sort();
  const gamesPlayed = keys.length;
  const wins = keys.reduce((acc, key) => acc + (completed[key]?.win ? 1 : 0), 0);
  const winPct = gamesPlayed ? Number(((wins / gamesPlayed) * 100).toFixed(1)) : 0;

  let currentStreak = 0;
  for (let i = keys.length - 1; i >= 0; i--) {
    if (completed[keys[i]]?.win) currentStreak += 1;
    else break;
  }

  let longestStreak = 0;
  let streak = 0;
  for (const key of keys) {
    if (completed[key]?.win) {
      streak += 1;
      if (streak > longestStreak) longestStreak = streak;
    } else {
      streak = 0;
    }
  }

  return {
    gamesPlayed,
    winPct,
    winStreak: currentStreak,
    longestStreak,
  };
}

async function kvGet(kvUrl, token, key) {
  const res = await fetch(`${kvUrl}/get/${encodeURIComponent(key)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("KV get failed");
  const payload = await res.json();
  const raw = payload?.result;
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

async function kvSet(kvUrl, token, key, value) {
  const raw = JSON.stringify(value);
  const res = await fetch(`${kvUrl}/set/${encodeURIComponent(key)}/${encodeURIComponent(raw)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("KV set failed");
}

async function kvIncr(kvUrl, token, key) {
  const res = await fetch(`${kvUrl}/incr/${encodeURIComponent(key)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("KV incr failed");
  const payload = await res.json();
  return Number(payload?.result || 0);
}

async function kvExpire(kvUrl, token, key, ttlSeconds) {
  const ttl = Math.max(1, Number(ttlSeconds || 1));
  const res = await fetch(`${kvUrl}/expire/${encodeURIComponent(key)}/${ttl}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("KV expire failed");
}

async function enforceRateLimit(req, kvUrl, token, limit) {
  const bucket = Math.floor(Date.now() / 60000);
  const ip = getClientIp(req);
  const method = String(req.method || "GET").toUpperCase();
  const key = `weekwindle:ratelimit:user-stats:${method}:${ip}:${bucket}`;
  const count = await kvIncr(kvUrl, token, key);
  if (count === 1) {
    await kvExpire(kvUrl, token, key, 70);
  }
  return count <= limit;
}

module.exports = async function handler(req, res) {
  setCommonHeaders(res);
  const origin = normalizeOrigin(req.headers.origin);
  const host = String(req.headers.host || "");
  const expectedOrigin = host ? `https://${host}` : "";
  if (origin && origin === expectedOrigin) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
  }
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(204).end();

  const kvUrl = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!kvUrl || !token) {
    return send(res, 500, {
      ok: false,
      error: "Service unavailable.",
    });
  }

  try {
    if (req.method === "GET") {
      const allowed = await enforceRateLimit(req, kvUrl, token, 120);
      if (!allowed) return send(res, 429, { ok: false, error: "Too many requests." });
      const userId = normalizeUserId(req.query?.userId);
      if (!userId) return send(res, 400, { ok: false, error: "Invalid userId." });
      const key = `weekwindle:user:${userId}:stats`;
      const record = await kvGet(kvUrl, token, key);
      return send(res, 200, { ok: true, stats: computeStats(record) });
    }

    if (req.method === "POST") {
      if (!isSameSiteRequest(req)) {
        return send(res, 403, { ok: false, error: "Forbidden." });
      }
      const allowed = await enforceRateLimit(req, kvUrl, token, 40);
      if (!allowed) return send(res, 429, { ok: false, error: "Too many requests." });
      const lengthHeader = Number(req.headers["content-length"] || 0);
      if (Number.isFinite(lengthHeader) && lengthHeader > 2048) {
        return send(res, 413, { ok: false, error: "Payload too large." });
      }
      const body = req.body && typeof req.body === "object" ? req.body : {};
      if (JSON.stringify(body).length > 4096) {
        return send(res, 413, { ok: false, error: "Payload too large." });
      }
      const userId = normalizeUserId(body.userId);
      const dailyKey = String(body.dailyKey || "").trim();
      const isPractice = body.isPractice;
      const didWin = body.didWin;

      if (!userId) return send(res, 400, { ok: false, error: "Invalid userId." });
      if (!isValidDailyKey(dailyKey)) return send(res, 400, { ok: false, error: "Invalid dailyKey." });
      if (typeof isPractice !== "boolean") return send(res, 400, { ok: false, error: "Invalid isPractice." });
      if (typeof didWin !== "boolean") return send(res, 400, { ok: false, error: "Invalid didWin." });

      const key = `weekwindle:user:${userId}:stats`;
      const record = await kvGet(kvUrl, token, key);
      const next = record && typeof record === "object" ? record : {};
      if (!next.completed || typeof next.completed !== "object") next.completed = {};

      if (!isPractice && !next.completed[dailyKey]) {
        next.completed[dailyKey] = {
          win: didWin,
          at: new Date().toISOString(),
        };
      }
      next.updatedAt = new Date().toISOString();

      await kvSet(kvUrl, token, key, next);
      return send(res, 200, { ok: true, stats: computeStats(next) });
    }

    return send(res, 405, { ok: false, error: "Method not allowed" });
  } catch {
    return send(res, 500, { ok: false, error: "Internal server error." });
  }
};
