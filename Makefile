.PHONY: demo test eval lint up down logs refresh psql

demo:
	docker compose up -d
	docker compose run --rm api python -m app.ingestion.cli --force

test:
	docker compose run --rm api pytest -v

eval:
	docker compose run --rm -v $(PWD)/docs:/app/docs -v $(PWD)/scripts:/app/scripts api python scripts/eval_heldout.py

lint:
	docker compose run --rm api ruff check .
	docker compose run --rm api ruff format --check .

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

refresh:
	docker compose run --rm api python -m app.ingestion.cli

psql:
	docker compose exec db psql -U postgres -d property_assistant
