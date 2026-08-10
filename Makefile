.PHONY: setup browser-install db-up db-down migrate seed test test-unit test-integration lint format db-check adapters crawl replay scheduler

setup:
	uv sync

browser-install:
	uv run playwright install chromium

db-up:
	docker compose up -d mysql

db-down:
	docker compose down

migrate:
	uv run alembic upgrade head

seed:
	uv run device-price db seed

db-check:
	uv run device-price db check

adapters:
	uv run device-price adapters

crawl:
	uv run device-price crawl --brand "$(BRAND)" --mode "$(or $(MODE),full)"

replay:
	uv run device-price replay --record-id "$(RECORD_ID)"

scheduler:
	uv run device-price scheduler

test:
	uv run pytest

test-unit:
	uv run pytest -m "not integration"

test-integration:
	uv run pytest -m integration

lint:
	uv run ruff check .
	uv run mypy src

format:
	uv run ruff format .
	uv run ruff check --fix .
