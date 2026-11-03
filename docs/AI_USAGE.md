# AI Usage Log

## Step 1: Foundation
### Date: 2026-11-03
### Components Generated
- Repo skeleton, `.gitignore`, `.env.example`, `Makefile`, `Dockerfile`, `docker-compose.yml`, `initdb.sql`.
- Database models: `Property`, `IngestionRun`, `IngestionError`, `PropertyChange`.
- CSV ingestion and synchronization pipeline with `pg_advisory_xact_lock`.
- Health check and administrative refresh endpoints.
- Tests (19/19 passing).

## Step 2: Deterministic Search Engine and Dialogue Policy
### Date: 2026-11-03
### Components Generated
1. **Reference Catalog (`app/domain/catalog.py`)**:
   - `Catalog` dataclass dynamically populated from active database records (cities, sectors by city, project names, BHK values, price/floor/area ranges).
   - Invalidation based on maximum successful `IngestionRun.id`.
2. **Normalizers & Aliases (`app/domain/aliases.py`, `app/domain/normalizers.py`)**:
   - `resolve_city`: Exact matching against catalog, alias mapping (`CITY_ALIASES`), and rapidfuzz fuzzy matching with score threshold >= 88. Enforces strict isolation between "Noida" and "Greater Noida West".
   - `normalize_sector`: Regex-based exact uppercase sector normalization (`150`, `16B`), rejecting partial substring matches.
   - `resolve_project`: Case-insensitive and fuzzy matching against catalog project names with score >= 85.
   - `parse_budget` & `parse_budget_string`: Handles crore and lakh units, bare numbers (>= 20 as lakh, < 20 as crore), qualifiers (`under`, `above`, `around` using `PRICE_AROUND_PCT`, `between`, `exact`), producing plain-English assumption logs.
   - `parse_bhk`: Validates integers 1-10 and checks presence against catalog BHKs.
   - Floor and area quartile helpers.
3. **Filters & Search Engine (`app/domain/filters.py`, `app/domain/search.py`)**:
   - `SearchFilters`: Pydantic model with slot status tracking (`specified`, `unsure`, `unspecified`) and JSON serialization.
   - `search_properties`: Bound SQLAlchemy queries, whitelisted fields, `is_active=True`, strict exclusion of sold units, and dynamic `unverifiable_ids` detection for rows with NULL in filtered fields.
   - Deterministic sorting rules: price ascending default if no sector filter; newest first if sector filter is active; override support (`price_asc`, `price_desc`, `newest`, `area_desc`). NULL price placed last.
   - `get_property`: Direct truthful lookup by ID.
4. **No-Match Explainer (`app/domain/relax.py`)**:
   - `explain_no_results`: Returns structured diagnostic facts (dropped-constraint match counts with up to 3 example IDs, sector presence in other cities, reserved/sold matching units, and smallest budget gap).
5. **Dialogue Policy (`app/domain/policy.py`)**:
   - `decide_next_action`: Evaluates `DialogueState` and candidate set.
   - Supports `AskCity` when a sector spans multiple cities, `RunSearch` when candidate count <= threshold (`FOLLOWUP_SKIP_THRESHOLD`) or `followups_asked >= 2`, and `AskFollowups` asking at most 2 prioritized slots (`city`, `budget`, `bhk`, `sector`, `floor`, `area`) with data-driven options and single unsure hint.
6. **Dev CLI (`app/domain/demo.py`)**:
   - Structured command-line interface for testing normalizers, search, relaxation, and policy without LLM dependencies.
7. **Automated Tests**:
   - `tests/test_normalizers.py`: Pure unit tests without database.
   - `tests/test_search.py`: Search execution, sort ordering, unverifiable IDs, and availability restrictions.
   - `tests/test_relax.py`: Diagnostics on zero-match searches.
   - `tests/test_policy.py`: Dialogue policy slot selection and escalation flow.

### Verification
- `docker compose run --rm api ruff check .`: Clean, 0 errors.
- `docker compose run --rm api pytest -v`: 36/36 tests passing (0.73s).
- Demo CLI verified with 3 live queries (Gurugram 3 BHK around 1 cr, Noida 3 BHK around 1 cr, and Gurugram Sector 79 relaxation).
