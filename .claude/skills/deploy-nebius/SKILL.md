---
name: deploy-nebius
description: Build Leaf's container and deploy it to a public Nebius Serverless Endpoint, then verify it. User-invoked only because it costs money and creates cloud resources.
disable-model-invocation: true
---
Deploy Leaf to Nebius AI Cloud. Stop and ask before any step marked (ask).

1. `make check` must pass. Ask the `reviewer` subagent for a verdict; fix any BLOCKER first.
2. Confirm the user has: the nebius CLI logged in (`nebius profile create`), Docker running, and a card on the Nebius account (minimum first payment $25).
3. Ask the user for `IMAGE` (Docker Hub `docker.io/<user>/leaf:vN`, or Nebius Container Registry `cr.<region>.nebius.cloud/<registry>/leaf:vN`) and `SUBNET_ID`. If they don't know the subnet, suggest `nebius vpc subnet list` and check `--help` if the syntax differs.
4. `make docker-build`, then `docker run -d --rm --name leaf-local -p 8000:8000 --env-file .env leaf:dev`, confirm `curl -s localhost:8000/api/health` shows `"search": "TavilySearcher"`, then `docker stop leaf-local`.
5. (ask) Run `IMAGE=... SUBNET_ID=... make deploy`.
6. Verify: `curl -s <URL>/api/health` returns ok, lists the Nemotron models and shows `"search": "TavilySearcher"`; open the URL and run one real diagnosis.
7. Put the public URL in README.md ("Try it") and docs/PROGRESS.md, commit and push.
8. Remind the user: the endpoint bills while it exists; keep it up until Dec 15 (end of judging), then delete it.
