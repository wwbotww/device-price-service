.PHONY: setup browser-install db-up db-down mysql57-up mysql57-down migrate seed seed-v2-fresh seed-v2-government seed-v2-devices catalog-sources catalog-smoke catalog-crawl test test-unit test-integration test-integration-mysql57 lint format db-check db-audit adapters crawl replay scheduler

setup:
	uv sync

browser-install:
	uv run playwright install chromium

db-up:
	docker compose up -d mysql

db-down:
	docker compose down

mysql57-up:
	docker compose -f docker-compose.mysql57.yml up -d mysql57

mysql57-down:
	docker compose -f docker-compose.mysql57.yml down

migrate:
	uv run alembic upgrade head

seed:
	uv run device-price db seed

seed-v2-fresh:
	uv run device-price db seed-v2-fresh

seed-v2-government:
	uv run device-price db seed-v2-government

seed-v2-devices:
	uv run device-price db seed-devices

catalog-sources:
	uv run device-price catalog sources

catalog-smoke:
	uv run device-price catalog smoke --channel "$(or $(CHANNEL),SH_FGW_FRESH_RETAIL)" $(if $(COMMODITY),--commodity "$(COMMODITY)",) $(if $(MAX_PRODUCTS),--max-products "$(MAX_PRODUCTS)",)

catalog-crawl:
	uv run device-price catalog crawl --channel "$(or $(CHANNEL),SH_FGW_FRESH_RETAIL)" $(if $(COMMODITY),--commodity "$(COMMODITY)",)

db-check:
	uv run device-price db check

db-audit:
	uv run device-price db audit

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

test-integration-mysql57:
	RUN_MYSQL_INTEGRATION=1 TEST_MYSQL_PORT=3308 uv run pytest -m integration

lint:
	uv run ruff check .
	uv run mypy src

format:
	uv run ruff format .
	uv run ruff check --fix .
