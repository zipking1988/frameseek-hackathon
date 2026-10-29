.PHONY: setup test check preflight run run-tokenfactory-prepare tokenfactory-check demo eval-fixture eval-check agent-check schema

setup:
	uv sync --python 3.13

test:
	uv run --frozen pytest

check:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .
	uv run --frozen python scripts/export_contracts.py --check

schema:
	uv run --frozen python scripts/export_contracts.py

preflight:
	uv run --frozen python -m frameseek.preflight

run:
	uv run --frozen uvicorn frameseek.api:create_app --factory --host 127.0.0.1 --port 8765

# Explicit local secret loading; preparation never enables paid dispatch.
run-tokenfactory-prepare:
	FS_RUNTIME_FILE="$(CURDIR)/.runtime.tokenfactory.local.json" FRAMESEEK_MODE=fixture uv run --frozen --env-file .env.tokenfactory uvicorn frameseek.api:create_app --factory --host 127.0.0.1 --port 8765

tokenfactory-check:
	FS_RUNTIME_FILE="$(CURDIR)/.runtime.tokenfactory.local.json" FRAMESEEK_MODE=fixture uv run --frozen --env-file .env.tokenfactory python -m frameseek.readiness

demo: run

eval-fixture:
	uv run --frozen python -m frameseek.fixtures

eval-check:
	uv run --frozen python -m frameseek.evaluation validate --dataset eval/diagnostics.template.json
	uv run --frozen python -m frameseek.evaluation score --dataset eval/diagnostics.template.json --run eval/run.template.json

agent-check:
	uv run --frozen python -m frameseek.readiness
