function send(res, status, payload) {
  res.status(status).json(payload);
}

function normalizeUserId(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 80);
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

module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(204).end();

  const kvUrl = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!kvUrl || !token) {
    return send(res, 500, {
      ok: false,
      error: "Stats backend is not configured. Add Vercel KV env vars.",
    });
  }

  try {
    if (req.method === "GET") {
      const userId = normalizeUserId(req.query?.userId);
      if (!userId) return send(res, 400, { ok: false, error: "Missing userId" });
      const key = `weekwindle:user:${userId}:stats`;
      const record = await kvGet(kvUrl, token, key);
      return send(res, 200, { ok: true, stats: computeStats(record) });
    }

    if (req.method === "POST") {
      const body = req.body && typeof req.body === "object" ? req.body : {};
      const userId = normalizeUserId(body.userId);
      const dailyKey = String(body.dailyKey || "").trim();
      const isPractice = Boolean(body.isPractice);
      const didWin = Boolean(body.didWin);

      if (!userId) return send(res, 400, { ok: false, error: "Missing userId" });
      if (!dailyKey) return send(res, 400, { ok: false, error: "Missing dailyKey" });

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
  } catch (err) {
    return send(res, 500, { ok: false, error: err?.message || "Unknown error" });
  }
};
