---
name: deploy-nebius
description: Deploy Leaf to Cloudflare Containers with make deploy-cloudflare and verify it once. (Name kept for habit; Leaf is no longer hosted on Nebius.) User-invoked only because it changes production.
disable-model-invocation: true
---
Leaf is hosted on Cloudflare Containers. Never try Nebius AI Cloud hosting (billing suspended on this account); model calls still go only to Token Factory.

1. `make check` must pass. Ask the `reviewer` subagent for a verdict on big changes; fix any BLOCKER first.
2. Confirm Docker is running and `npx --prefix cloudflare wrangler whoami` shows the account. `.env` must hold NEBIUS_API_KEY, TAVILY_API_KEY and the R2 keys (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY); never read or print them.
3. `DRY_RUN=1 make deploy-cloudflare` to validate, then `make deploy-cloudflare`. Secrets ship with the version (`--secrets-file`).
4. Wait for `npx --prefix cloudflare wrangler containers list` to show `ready` (poll the platform, never the site). If the image changed, wait for `containers info <ID>` to show the new version.
5. Check once in a headless browser: the page (or the "Leaf is waking up" page, which reloads itself), `/api/health` (ok, `"search": "TavilySearcher"`), and one real diagnosis.
6. Update docs/PROGRESS.md, commit and push.
7. After Dec 15: `npx --prefix cloudflare wrangler delete leaf`.
