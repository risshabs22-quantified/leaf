// Serves Leaf from one Cloudflare Container. The Worker only forwards requests; the
// FastAPI app in ../Dockerfile does everything else, and its model calls go to Nebius
// Token Factory exactly as they do locally.
import { Container, getContainer } from "@cloudflare/containers";

// Worker secrets (`wrangler secret bulk`), passed to the container and nowhere else.
const SECRETS = ["NEBIUS_API_KEY", "TAVILY_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"] as const;
// Plain settings from `vars` in wrangler.jsonc.
const SETTINGS = [
  "LEAF_DAILY_CAP",
  "LEAF_DEEP_DAILY_CAP",
  "LEAF_TAVILY_DAILY_CAP",
  "LEAF_TRUSTED_PROXY_HOPS",
  "LEAF_JOURNAL_BUCKET",
  "AWS_ENDPOINT_URL",
  "AWS_REGION",
] as const;

type Env = { LEAF: DurableObjectNamespace<LeafContainer> } & Partial<
  Record<(typeof SECRETS)[number] | (typeof SETTINGS)[number], string>
>;

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

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Leaf limits requests per IP from the last X-Forwarded-For entry. Replace whatever the
    // client sent with the address Cloudflare saw, so the header cannot be spoofed.
    const headers = new Headers(request.headers);
    headers.set("x-forwarded-for", request.headers.get("cf-connecting-ip") ?? "unknown");
    // One instance holds Leaf's in-memory state (running Deep cases, rate limits, daily caps).
    return getContainer(env.LEAF, "leaf").fetch(new Request(request, { headers }));
  },
} satisfies ExportedHandler<Env>;
