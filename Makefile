.PHONY: up down logs refresh test lint psql

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

refresh:
	docker compose exec api python -m app.ingestion.cli

test:
	docker compose run --rm api pytest -v

lint:
	docker compose run --rm api ruff check .

psql:
	docker compose exec db psql -U postgres -d property_assistant
