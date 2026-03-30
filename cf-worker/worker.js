/**
 * openOcto Relay Proxy - Cloudflare Worker + Durable Object
 *
 * Forwards Redis REST API requests to Upstash, keeping the token server-side.
 *
 * Security:
 *   - Rate limit via Durable Object (0 Upstash commands for counting)
 *   - Namespace isolation (workspace header, keys restricted)
 *   - Dangerous Redis commands blocked (whitelist mode)
 *
 * Cost per proxy request:
 *   - Normal: 1 Upstash command (pure forward, counting is free)
 *   - Over limit: 0 Upstash commands (rejected at CF edge)
 *
 * Environment variables (set via `wrangler secret put`):
 *   UPSTASH_URL   - Upstash Redis REST URL
 *   UPSTASH_TOKEN - Upstash Redis REST token
 *   DAILY_LIMIT   - (optional) Max proxy requests/day, default 4000
 *
 * Requires: Cloudflare Workers Paid plan ($5/month) for Durable Objects
 *
 * Deploy:
 *   npx wrangler deploy
 */

const BLOCKED_COMMANDS = new Set([
  "KEYS", "SCAN", "FLUSHDB", "FLUSHALL", "CONFIG", "DEBUG",
  "SHUTDOWN", "SLAVEOF", "REPLICAOF", "CLUSTER", "MIGRATE",
  "DUMP", "RESTORE", "OBJECT", "CLIENT", "MONITOR", "SUBSCRIBE",
  "PSUBSCRIBE", "UNSUBSCRIBE", "PUNSUBSCRIBE", "WAIT", "SWAPDB",
]);

// ---- Durable Object: global atomic counter ----

export class RateLimiter {
  constructor(state) {
    this.state = state;
    this.count = 0;
    this.dateKey = "";
  }

  async fetch(request) {
    const url = new URL(request.url);
    const limit = parseInt(url.searchParams.get("limit")) || 4000;
    const today = new Date().toISOString().slice(0, 10);

    // Reset counter at midnight
    if (this.dateKey !== today) {
      this.dateKey = today;
      this.count = (await this.state.storage.get("count:" + today)) || 0;
    }

    this.count++;
    // Persist every 50 increments (batched writes for performance)
    if (this.count % 50 === 0) {
      await this.state.storage.put("count:" + today, this.count);
    }

    if (this.count > limit) {
      return new Response(JSON.stringify({
        error: `Public relay limit reached (${limit}/day). Set up your own Redis for unlimited use: https://upstash.com`,
        count: this.count,
      }), { status: 429 });
    }

    return new Response(JSON.stringify({ allowed: true, count: this.count }), { status: 200 });
  }
}

// ---- Worker: main handler ----

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }

    const url = new URL(request.url);

    if ((url.pathname === "/" || url.pathname === "/health") && request.method === "GET") {
      return jsonResponse({ status: "ok", service: "openocto-relay" });
    }

    // ---- ASR Proxy: /v1/audio/transcriptions ----
    if (url.pathname === "/v1/audio/transcriptions" && request.method === "POST") {
      return handleASR(request, env);
    }

    if (!env.UPSTASH_URL || !env.UPSTASH_TOKEN) {
      return jsonResponse({ error: "Worker not configured" }, 500);
    }

    // --- Parse command (GET path or POST JSON body) ---
    let parts;
    let postBody = null;
    if (request.method === "POST" && (url.pathname === "/" || url.pathname === "")) {
      // POST with JSON array body: ["CMD", "arg1", "arg2", ...]
      try {
        const bodyText = await request.text();
        const bodyArr = JSON.parse(bodyText);
        if (!Array.isArray(bodyArr) || bodyArr.length === 0) {
          return jsonResponse({ error: "POST body must be a JSON array" }, 400);
        }
        parts = bodyArr.map(String);
        postBody = bodyText;
      } catch (e) {
        return jsonResponse({ error: "Invalid JSON body" }, 400);
      }
    } else {
      parts = url.pathname.split("/").filter(Boolean);
    }
    if (parts.length === 0) {
      return jsonResponse({ error: "No command" }, 400);
    }

    const command = parts[0].toUpperCase();

    if (command === "PING") {
      return forward(env, url.pathname);
    }

    // --- Block dangerous commands ---
    if (BLOCKED_COMMANDS.has(command)) {
      return jsonResponse({ error: `Command '${command}' is not allowed` }, 403);
    }

    // --- Namespace isolation ---
    const workspace = request.headers.get("X-Octo-Workspace");
    if (!workspace) {
      return jsonResponse({ error: "Missing X-Octo-Workspace header" }, 400);
    }

    // Block short/guessable workspace names on public relay
    if (workspace.length < 10 && workspace !== "default") {
      return jsonResponse({ error: "Workspace name too short (min 10 chars). Run 'octo init' to generate a secure ID." }, 403);
    }

    const prefix = `octo:${workspace}:`;
    const violation = checkNamespace(command, parts.slice(1), prefix);
    if (violation) {
      return jsonResponse({ error: violation }, 403);
    }

    // --- Per-IP rate limit (anti brute-force) ---
    const clientIp = request.headers.get("CF-Connecting-IP") || "unknown";
    const ipLimitId = env.RATE_LIMITER.idFromName(`ip:${clientIp}`);
    const ipLimitStub = env.RATE_LIMITER.get(ipLimitId);
    try {
      const ipCheckUrl = new URL("https://dummy/check");
      ipCheckUrl.searchParams.set("limit", "10000"); // 10k req/day per IP
      const ipResp = await ipLimitStub.fetch(ipCheckUrl.toString());
      if (ipResp.status === 429) {
        return jsonResponse({ error: "Too many requests from this IP. Try again tomorrow." }, 429);
      }
    } catch (e) { /* fail-open */ }

    // --- Global rate limit via Durable Object (0 Upstash commands) ---
    const dailyLimit = parseInt(env.DAILY_LIMIT) || 4000;
    const id = env.RATE_LIMITER.idFromName("global");
    const stub = env.RATE_LIMITER.get(id);
    try {
      const checkUrl = new URL("https://dummy/check");
      checkUrl.searchParams.set("limit", String(dailyLimit));
      const rateLimitResp = await stub.fetch(checkUrl.toString());
      if (rateLimitResp.status === 429) {
        const body = await rateLimitResp.json();
        return jsonResponse(body, 429);
      }
    } catch (e) {
      // fail-open: if DO is unavailable, allow the request
    }

    // --- Forward to Upstash (1 Upstash command) ---
    if (postBody) {
      return forwardPost(env, postBody);
    }
    return forward(env, url.pathname);
  },
};

async function handleASR(request, env) {
  const asrUrl = (env.ASR_URL || "https://api.siliconflow.cn/v1").replace(/\/$/, "");
  const asrToken = env.ASR_TOKEN || "";
  if (!asrToken) {
    return jsonResponse({ error: "ASR not configured on this relay" }, 503);
  }

  // Per-IP rate limit: 50 ASR requests/day
  const clientIp = request.headers.get("CF-Connecting-IP") || "unknown";
  const ipLimitId = env.RATE_LIMITER.idFromName(`asr:${clientIp}`);
  const ipLimitStub = env.RATE_LIMITER.get(ipLimitId);
  try {
    const checkUrl = new URL("https://dummy/check");
    checkUrl.searchParams.set("limit", "50");
    const resp = await ipLimitStub.fetch(checkUrl.toString());
    if (resp.status === 429) {
      return jsonResponse({ error: "ASR daily limit reached (50/day per device)" }, 429);
    }
  } catch (e) { /* fail-open */ }

  // Forward multipart body to SiliconFlow
  try {
    const contentType = request.headers.get("Content-Type") || "";
    const resp = await fetch(`${asrUrl}/audio/transcriptions`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${asrToken}`,
        "Content-Type": contentType,
      },
      body: request.body,
      duplex: "half",
    });

    // Need to copy the content-type from original request for multipart boundary
    const body = await resp.text();
    return new Response(body, {
      status: resp.status,
      headers: { "Content-Type": "application/json", ...corsHeaders() },
    });
  } catch (err) {
    return jsonResponse({ error: `ASR upstream error: ${err.message}` }, 502);
  }
}

async function forwardPost(env, body) {
  const upstashUrl = env.UPSTASH_URL.replace(/\/$/, "") + "/";
  try {
    const resp = await fetch(upstashUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.UPSTASH_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: body,
    });
    const respBody = await resp.text();
    return new Response(respBody, {
      status: resp.status,
      headers: { "Content-Type": "application/json", ...corsHeaders() },
    });
  } catch (err) {
    return jsonResponse({ error: `Upstream error: ${err.message}` }, 502);
  }
}

async function forward(env, pathname) {
  const upstashUrl = env.UPSTASH_URL.replace(/\/$/, "") + pathname;
  try {
    const resp = await fetch(upstashUrl, {
      method: "GET",
      headers: { "Authorization": `Bearer ${env.UPSTASH_TOKEN}` },
    });
    const body = await resp.text();
    return new Response(body, {
      status: resp.status,
      headers: { "Content-Type": "application/json", ...corsHeaders() },
    });
  } catch (err) {
    return jsonResponse({ error: `Upstream error: ${err.message}` }, 502);
  }
}

function checkNamespace(command, args, prefix) {
  const keyPositions = {
    GET: [0], SET: [0], DEL: [0], EXPIRE: [0], TTL: [0],
    INCR: [0], DECR: [0], APPEND: [0], STRLEN: [0],
    GETDEL: [0], GETSET: [0],
    HSET: [0], HGET: [0], HDEL: [0], HGETALL: [0], HSETNX: [0],
    HEXISTS: [0], HLEN: [0], HKEYS: [0], HVALS: [0],
    LPUSH: [0], RPUSH: [0], LPOP: [0], RPOP: [0],
    LRANGE: [0], LLEN: [0], LTRIM: [0], LINDEX: [0],
    EVAL: "eval",
  };

  const spec = keyPositions[command];
  if (spec === undefined) {
    return `Command '${command}' is not supported through proxy`;
  }

  if (spec === "eval") {
    const numKeys = parseInt(args[1]) || 0;
    for (let i = 0; i < numKeys; i++) {
      const key = decodeURIComponent(args[2 + i] || "");
      if (!key.startsWith(prefix)) {
        return `Key '${key}' outside workspace '${prefix}'`;
      }
    }
    return null;
  }

  for (const pos of spec) {
    const key = decodeURIComponent(args[pos] || "");
    if (!key.startsWith(prefix)) {
      return `Key '${key}' outside workspace '${prefix}'`;
    }
  }
  return null;
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Octo-Workspace",
  };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}
