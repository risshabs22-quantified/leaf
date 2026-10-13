---
name: deploy-nebius
description: Build Leaf's container and deploy it to a public Nebius Serverless Endpoint, then verify it. User-invoked only because it costs money and creates cloud resources.
disable-model-invocation: true
---
Deploy Leaf to Nebius AI Cloud. Stop and ask before any step marked (ask).

1. `make check` must pass. Ask the `reviewer` subagent for a verdict; fix any BLOCKER first.
2. Confirm the user has: the nebius CLI logged in (`nebius profile create`), Docker running, and a card on the Nebius account (minimum first payment $25).
3. Ask the user for `IMAGE` (Docker Hub `docker.io/<user>/leaf:vN`, or Nebius Container Registry `cr.<region>.nebius.cloud/<registry>/leaf:vN`). The endpoint uses the project's default subnet. Quote the cost first (smallest preset `2vcpu-8gb` on `cpu-d3` at $0.066/h or `cpu-e2` at $0.060/h in eu-north1, plus a 32 GiB disk) and get approval. If the journal bucket is wanted, give the user the bucket steps and env lines from docs/PROGRESS.md before deploying.
4. `make docker-build`, then `docker run -d --rm --name leaf-local -p 8000:8000 --env-file .env leaf:dev`, confirm `curl -s localhost:8000/api/health` shows `"search": "TavilySearcher"`, then `docker stop leaf-local`.
5. Run `IMAGE=... DRY_RUN=1 make deploy` (validates the endpoint request, creates nothing). Then (ask) run `IMAGE=... make deploy`: it stores the API keys in SecretStash and creates the endpoint.
6. Verify once, never in a loop: wait for `nebius ai endpoint get` to report it running, then one request to `<URL>/api/health` (ok, Nemotron models, `"search": "TavilySearcher"`), then one real diagnosis in a headless browser.
7. Put the public URL in README.md ("Try it") and docs/PROGRESS.md, commit and push.
8. Remind the user: the endpoint bills while it exists; keep it up until Dec 15 (end of judging), then delete it.
