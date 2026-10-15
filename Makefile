# Leaf task runner. Output is kept short on purpose (saves Claude Code context).
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help setup smoke smoke-all test lint fmt check worker-check migrate-local eval-router eval-plant-id run docker-build docker-run deploy deploy-cloudflare eval clean

help:  ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'

setup:  ## Create .venv, install deps, create .env from template
	python3 -m venv .venv
	$(PIP) install -q -e ".[dev]"
	@test -f .env || (cp .env.example .env && echo "Created .env: add NEBIUS_API_KEY and TAVILY_API_KEY")

smoke:  ## Check Token Factory key + models (costs < 1 cent)
	$(PY) scripts/smoke_test.py

smoke-all:  ## Smoke test including 1 Tavily credit
	$(PY) scripts/smoke_test.py --tavily

test:  ## Offline unit tests (no credits)
	.venv/bin/pytest

lint:  ## Ruff lint
	.venv/bin/ruff check . --output-format concise

fmt:  ## Ruff format + autofix
	.venv/bin/ruff format . -q && .venv/bin/ruff check . --fix -q

check: lint test worker-check  ## Lint + tests + Worker type check (run before every commit)

worker-check:  ## Type-check the Cloudflare Worker
	@test -d cloudflare/node_modules || (cd cloudflare && npm ci --no-fund --no-audit --silent)
	cd cloudflare && npx tsc --noEmit

migrate-local:  ## Apply platform migrations to the local SQLite database (data/platform.db)
	$(PY) -m backend.data.migrate data/platform.db

run:  ## Run locally at http://localhost:8000
	.venv/bin/uvicorn backend.app:app --reload --port 8000

docker-build:  ## Build the container
	docker build -t leaf:dev .

docker-run:  ## Run the container with your .env
	docker run --rm -p 8000:8000 --env-file .env -e LEAF_PLATFORM_URL= leaf:dev

deploy:  ## Deploy to a Nebius Serverless Endpoint (needs IMAGE; DRY_RUN=1 validates only)
	./scripts/deploy_nebius.sh

deploy-cloudflare:  ## Deploy to Cloudflare Containers (DRY_RUN=1 validates only)
	./scripts/deploy_cloudflare.sh

eval-router:  ## Score the front door on evals/front_door.jsonl, Lightning and Nano 3 (about half a cent)
	$(PY) -m scripts.eval_front_door

eval-plant-id:  ## Score photo identification on evals/plant_id.jsonl (about 16 cents, 40 Tavily credits)
	$(PY) -m scripts.eval_plant_id

eval:  ## Score Leaf on evals/cases.jsonl (spends credits)
	$(PY) scripts/eval.py

clean:  ## Remove caches
	rm -rf .pytest_cache .ruff_cache **/__pycache__
