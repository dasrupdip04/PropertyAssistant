# AI Usage Log - Step 1: Foundation

## Overview
Log of AI assistance, generated components, and verification details for Step 1: Foundation of PropertyAssistant.

## Date
2026-11-03

## Components Generated

### 1. Repository & Project Skeleton
- `pyproject.toml`: Modern Python project specification with FastAPI, SQLAlchemy 2.0, psycopg 3, Pydantic v2, pydantic-settings, pytest, and ruff.
- `.gitignore`: Configured to exclude virtual environments, cache directories, `.env`, SQLite/Postgres data, logs, and artifacts.
- `.env.example`: Template for environment variables and secrets.
- `Makefile`: Convenient developer targets (`up`, `down`, `logs`, `refresh`, `test`, `lint`, `psql`).
- `README.md`: Minimal project documentation stub.
- `data/property_inventory.csv`: Copy of the baseline property dataset (original root file preserved untouched).

### 2. Docker & Infrastructure
- `Dockerfile`: Multi-stage Python 3.12-slim container with preinstalled development dependencies, curl, and PostgreSQL client.
- `docker-compose.yml`: Services for `db` (PostgreSQL 16 Alpine with healthcheck, named volume `pgdata`, host port mapping 5432) and `api` (FastAPI with reload and live bind-mounts).
- `docker/initdb/01-init-test-db.sql`: Automatically provisions the isolated `property_assistant_test` database for automated test suites.

### 3. Application Core & Database Models
- `app/config.py`: Configuration management using `pydantic-settings` with type-safe settings for database URLs, inventory path, admin token, and rejection ratio.
- `app/db.py`: Database engine, connection pooling, session factory, and startup schema initialization.
- `app/models.py`: SQLAlchemy 2.0 declarative models:
  - `Property`: Canonical properties with composite index on `(city, sector)`, individual indexes on `bhk`, `price_lakh`, `availability`, and `is_active`.
  - `IngestionRun`: Ingestion pipeline execution history tracking status, counts, file hashes, and error summaries.
  - `IngestionError`: Individual row-level parsing/validation errors with raw payload.
  - `PropertyChange`: Audit log recording field-level before/after diffs for modified properties.

### 4. Ingestion Pipeline & Synchronization
- `app/ingestion/parser.py`: Pure parsing functions with BOM (`utf-8-sig`) handling, case-insensitive headers, canonical city mapping (`noida` -> `Noida`, `gurgaon`/`gurugram` -> `Gurugram`, `greater noida west` -> `Greater Noida West`), uppercase sector normalization, nullable blank fields, and SHA-256 row hashing.
- `app/ingestion/sync.py`: Single-transaction synchronization guarded by `pg_advisory_xact_lock` to prevent concurrent collisions. Accurately categorizes inserted, updated (logging field changes), unchanged, deactivated, and reactivated rows, with rejection thresholds and unchanged file skipping.
- `app/ingestion/cli.py`: Standalone CLI supporting `--path`, `--dry-run`, and `--force`.
- `app/main.py`: FastAPI server with `/health`, `/admin/refresh` (authenticated via `X-Admin-Token`), and ingestion run inspection endpoints.

### 5. Automated Tests
- `tests/test_parser.py`: Unit tests for regex location parsing, blank/null handling, duplicate IDs, bad availability rejection, trailing blank lines, and BOM.
- `tests/test_sync.py`: Integration tests against PostgreSQL verifying full ingestion, unchanged second pass, property updates with audit diffs, deactivation/reactivation lifecycle, empty file aborts, missing header aborts, dry run immutability, and concurrent refresh locking.
- `tests/test_api.py`: API tests for `/health`, token-based authentication (valid, wrong, and missing), and run listing.

## Verification

1. **Static Analysis & Linting**:
   - `ruff check .` executed inside container: all checks passed.
2. **Automated Test Suite**:
   - `pytest -v` executed inside container: 19/19 tests passed (0.41s).
3. **Live Service Verification**:
   - `docker compose up -d --build`: Services healthy.
   - `curl http://localhost:8000/health`: `{"status":"healthy","database":"connected"}`.
   - `POST /admin/refresh`: First load inserted 36 rows; second load returned `unchanged_file` with 0 changes.
   - Database queries verified 36 active properties, PROP-008 NULL floor with price 95.00, and PROP-009 NULL price with floor 5.
