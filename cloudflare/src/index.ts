// Serves Leaf from one Cloudflare Container. The Worker forwards requests, runs the job
// schedule, and is the container's door to the platform data: the FastAPI app in
// ../Dockerfile does everything else, and its model calls go to Nebius Token Factory exactly
// as they do locally.
import { Container, getContainer, type OutboundHandlerContext } from "@cloudflare/containers";
import { WAKING_HTML } from "./waking";

// Outbound interception needs this export (the container library routes through it).
export { ContainerProxy } from "@cloudflare/containers";

// Worker secrets (deployed with `--secrets-file`), passed to the container and nowhere else.
const SECRETS = ["NEBIUS_API_KEY", "TAVILY_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"] as const;
// Plain settings from `vars` in wrangler.jsonc.
const SETTINGS = [
  "LEAF_DAILY_CAP",
  "LEAF_DEEP_DAILY_CAP",
  "LEAF_TAVILY_DAILY_CAP",
  "LEAF_TRUSTED_PROXY_HOPS",
  "LEAF_JOURNAL_BUCKET",
  "LEAF_PUBLIC_URL",
  "LEAF_PLATFORM_URL",
  "LEAF_SPEND_CAP_USD",
  "LEAF_BROWNOUT",
  "LEAF_NANO_MODEL",
  "AWS_ENDPOINT_URL",
  "AWS_REGION",
] as const;

// A job as the queue carries it (backend/jobs.py JobMessage).
type Job = { kind: string; key: string; payload?: Record<string, unknown> };

type Env = {
  LEAF: DurableObjectNamespace<LeafContainer>;
  DB: D1Database;
  KV: KVNamespace;
  JOBS: Queue<Job>;
} & Partial<Record<(typeof SECRETS)[number] | (typeof SETTINGS)[number], string>>;

// The container calls http://leaf-platform.internal/... (LEAF_PLATFORM_URL). Those requests
// never reach the network: the container library hands them to this handler, which answers
// with the Worker's own bindings. Nothing outside the container can call it.
const PLATFORM_HOST = "leaf-platform.internal";
const MAX_STATEMENTS = 200;
const KV_MIN_TTL_S = 60;

type Statement = { sql: string; params?: unknown[] };

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: { "cache-control": "no-store" } });
}

async function d1Batch(env: Env, req: Request): Promise<Response> {
  const body = (await req.json().catch(() => null)) as { statements?: Statement[] } | null;
  const statements = body?.statements;
  if (!Array.isArray(statements) || statements.length === 0 || statements.length > MAX_STATEMENTS) {
    return json({ error: `send 1-${MAX_STATEMENTS} statements` }, 400);
  }
  try {
    // A D1 batch runs as one transaction: all statements land, or none do.
    const prepared = statements.map((s) => env.DB.prepare(s.sql).bind(...(s.params ?? [])));
    const results = await env.DB.batch(prepared);
    return json({
      results: results.map((r) => ({ rows: r.results ?? [], changes: r.meta?.changes ?? 0 })),
    });
  } catch (err) {
    return json({ error: err instanceof Error ? err.message : "D1 error" }, 500);
  }
}

async function kv(env: Env, req: Request, url: URL): Promise<Response> {
  const key = decodeURIComponent(url.pathname.slice("/kv/".length));
  if (!key || key.length > 512) return json({ error: "bad key" }, 400);
  if (req.method === "GET") {
    const value = await env.KV.get(key);
    return value === null ? new Response(null, { status: 404 }) : new Response(value);
  }
  if (req.method === "PUT") {
    const ttl = Math.max(KV_MIN_TTL_S, Number(url.searchParams.get("ttl")) || KV_MIN_TTL_S);
    await env.KV.put(key, await req.text(), { expirationTtl: ttl });
    return new Response(null, { status: 204 });
  }
  if (req.method === "DELETE") {
    await env.KV.delete(key);
    return new Response(null, { status: 204 });
  }
  return json({ error: "method not allowed" }, 405);
}

async function enqueue(env: Env, req: Request): Promise<Response> {
  const body = (await req.json().catch(() => null)) as {
    messages?: { body: Job; delaySeconds?: number }[];
  } | null;
  const messages = body?.messages;
  if (!Array.isArray(messages) || messages.length === 0 || messages.length > 100) {
    return json({ error: "send 1-100 messages" }, 400);
  }
  await env.JOBS.sendBatch(
    messages.map((m) => ({ body: m.body, delaySeconds: Math.min(Math.max(m.delaySeconds ?? 0, 0), 86_400) })),
  );
  return json({ sent: messages.length });
}

async function platformBridge(req: Request, env: Env, _ctx: OutboundHandlerContext): Promise<Response> {
  const url = new URL(req.url);
  try {
    if (url.pathname === "/d1" && req.method === "POST") return await d1Batch(env, req);
    if (url.pathname.startsWith("/kv/")) return await kv(env, req, url);
    if (url.pathname === "/queue" && req.method === "POST") return await enqueue(env, req);
    return json({ error: "not found" }, 404);
  } catch (err) {
    return json({ error: err instanceof Error ? err.message : "bridge error" }, 500);
  }
}

export class LeafContainer extends Container<Env> {
  defaultPort = 8000;
  // Long enough that a judge's session, and a Deep case that is still running, never hits a
  // cold start; short enough that an idle day costs almost nothing.
  sleepAfter = "30m";

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    const vars: Record<string, string> = {};
    for (const key of [...SECRETS, ...SETTINGS]) {
      const value = env[key];
      if (value) vars[key] = value;
    }
    this.envVars = vars;
  }
}

// Assigned, not declared as a `static` field: a class field would define a new property and
// skip the library's static setter, so its handler registry stayed empty and the request went
// out to the internet instead (found in local testing).
LeafContainer.outboundByHost = { [PLATFORM_HOST]: platformBridge };

const NO_STORE = { "cache-control": "no-store" };

// A person opening a page (not a script calling the API), so a waking screen makes sense.
function isPageVisit(request: Request, url: URL): boolean {
  if (request.method !== "GET") return false;
  const page = ["/", "/app", "/plants"].includes(url.pathname) || url.pathname.startsWith("/c/");
  if (!page) return false;
  return (request.headers.get("accept") ?? "").includes("text/html");
}

// Jobs reach the app at /internal/jobs/run with this header. The Worker strips it from every
// public request and refuses /internal/ paths, so only the queue consumer below can send it.
const INTERNAL_HEADER = "x-leaf-internal";

// The app decodes percent-escapes, so /%69nternal/ must be refused too.
function isInternalPath(pathname: string): boolean {
  let decoded = pathname;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return true; // malformed escapes: refuse rather than guess
  }
  return pathname.toLowerCase().startsWith("/internal/") || decoded.toLowerCase().startsWith("/internal/");
}

// Cron Trigger -> the jobs it starts (docs/LEAF_PLATFORM.md, "Jobs"). UTC.
function scheduledJobs(cron: string, at: Date): Job[] {
  const day = at.toISOString().slice(0, 10);
  if (cron === "17 7 * * *") return [{ kind: "housekeeping", key: `housekeeping:${day}`, payload: {} }];
  return [];
}

// Backoff for a failed job: 30 s, 60 s, 120 s ... capped at an hour.
function retryDelay(attempts: number): number {
  return Math.min(3600, 30 * 2 ** Math.max(0, attempts - 1));
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    // One instance holds Leaf's in-memory state (running Deep cases, rate limits, daily caps).
    const url = new URL(request.url);
    if (url.hostname === "www.leafathon.site") { // one canonical address
      url.hostname = "leafathon.site";
      return Response.redirect(url.toString(), 301);
    }
    if (isInternalPath(url.pathname)) {
      return new Response("Not found", { status: 404, headers: NO_STORE });
    }
    const leaf = getContainer(env.LEAF, "leaf");

    if (url.pathname === "/__leaf/ready") {
      // Waits (up to ~25 s) until Leaf accepts connections, so the waking page polls rarely.
      // Always 200 (the body says whether Leaf is ready), so a waiting page logs no errors.
      try {
        await leaf.startAndWaitForPorts({ cancellationOptions: { portReadyTimeoutMS: 25_000 } });
        return Response.json({ ready: true }, { headers: NO_STORE });
      } catch {
        return Response.json({ ready: false }, { headers: NO_STORE });
      }
    }

    if (isPageVisit(request, url)) {
      const { status } = await leaf.getState();
      if (status !== "running" && status !== "healthy") {
        ctx.waitUntil(leaf.startAndWaitForPorts().catch(() => undefined));
        // 200, not 503: a 503 document is a console error in every browser, and the page is
        // a real answer ("waking up, one moment"). noindex keeps it out of search results.
        return new Response(WAKING_HTML, {
          headers: {
            "content-type": "text/html; charset=utf-8",
            "x-robots-tag": "noindex",
            ...NO_STORE,
          },
        });
      }
    }

    // Leaf limits requests per IP from the last X-Forwarded-For entry. Replace whatever the
    // client sent with the address Cloudflare saw, so the header cannot be spoofed.
    const headers = new Headers(request.headers);
    headers.set("x-forwarded-for", request.headers.get("cf-connecting-ip") ?? "unknown");
    headers.delete(INTERNAL_HEADER);
    return leaf.fetch(new Request(request, { headers }));
  },

  // Cron Triggers only enqueue; the queue consumer wakes the container for the work.
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    const jobs = scheduledJobs(controller.cron, new Date(controller.scheduledTime));
    if (jobs.length) await env.JOBS.sendBatch(jobs.map((body) => ({ body })));
  },

  // Each job runs in the container (backend/jobs.py), which answers how to settle it.
  async queue(batch: MessageBatch<Job>, env: Env): Promise<void> {
    const leaf = getContainer(env.LEAF, "leaf");
    for (const msg of batch.messages) {
      let status = 0;
      let retryAfter = 60;
      try {
        const res = await leaf.fetch(
          new Request("http://leaf/internal/jobs/run", {
            method: "POST",
            headers: { "content-type": "application/json", [INTERNAL_HEADER]: "1" },
            body: JSON.stringify(msg.body),
          }),
        );
        status = res.status;
        const body = (await res.json().catch(() => ({}))) as { retry_after?: number };
        if (typeof body.retry_after === "number") retryAfter = body.retry_after;
      } catch (err) {
        console.error("job delivery failed", msg.body?.key, err);
      }
      if (status === 200) msg.ack();
      else if (status === 422) {
        console.error("job rejected, not retried", msg.body?.key);
        msg.ack();
      } else if (status === 409) msg.retry({ delaySeconds: Math.min(Math.max(retryAfter, 30), 3600) });
      else msg.retry({ delaySeconds: retryDelay(msg.attempts) });
    }
  },
} satisfies ExportedHandler<Env, Job>;
