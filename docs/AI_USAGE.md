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

## Step 3: Session State, Gemini LLM Layer, Chat Orchestration, and `/chat` API
### Date: 2026-11-03
### Components Generated
1. **Gemini Client behind `LLMClient` Protocol (`app/llm/client.py`)**:
   - `LLMClient` protocol defining `generate_structured` and `generate_text`.
   - `GeminiClient` implementing the official `google-genai` SDK using `client.models.generate_content`.
   - Hard constraints respected: no temperature, top_p or top_k sent (deprecated in Gemini 3.5 Flash-Lite / 3.6 Flash); structured JSON schema passed via `config.response_schema`; thinking level configured via `types.ThinkingConfig`.
   - Exponential backoff retry on HTTP 429, 5xx, and timeouts; single retry on schema-invalid outputs; latency and token logging; typed `LLMError` isolation preventing stack trace leaks.
   - `FakeLLMClient` providing scripted queues, custom handlers, and deterministic fallback templates for hermetic testing.
2. **NLU Layer & Schemas (`app/llm/schema.py`, `app/llm/nlu.py`, `app/llm/prompts/nlu.md`, `app/chat/apply.py`)**:
   - Pydantic schema `NLUResult` capturing intent, new-search flag, results-now flag, per-slot filter actions (`set`, `keep`, `clear`, `unsure`), sort preference, reference resolution (ordinal, property ID, superlative), and asked attributes.
   - Few-shot system prompt `app/llm/prompts/nlu.md` containing 20 curated examples covering Hinglish, typo resilience, acronyms, slot actions, and prompt injection defense.
   - `apply_nlu`: Integrates Step 2 normalizers (`resolve_city`, `normalize_sector`, `parse_budget`, `parse_bhk`, floor/area band lookups) to update `ChatState` with deterministic assumptions and typed issues (`NLUIssue`).
3. **Session State & Persistence (`app/chat/models.py`, `app/chat/state.py`)**:
   - SQLAlchemy models: `SessionModel`, `ConversationTurn` (with structured debug metadata), `SearchLog`.
   - `ChatState` and `LastSearchState` tracking cumulative pagination offsets and shown property positions across turns.
4. **Chat Orchestration & Grounding (`app/chat/service.py`, `app/chat/grounding.py`, `app/llm/prompts/responder.md`)**:
   - Response plan generator covering `ask_city`, `ask_followups`, `show_results`, `no_results`, `property_answer`, `compare`, `clarify_reference`, `smalltalk`, `reset`, `issue`, and `nlu_failed`.
   - Phrasing with Responder using `app/llm/prompts/responder.md` enforcing empathy, max 3 short sentences, max 2 questions, conditional unsure hint, and explicit assumptions.
   - Grounding verifier `verify_grounding` strictly checking all mentioned `PROP-###` IDs and key figures against values present in the response plan; automated retry on violation and fallback to deterministic template `render_fallback_template`.
   - Helper formatters: `format_cards` (with badges: "cheapest", "largest", "best price/sqft"), `build_filter_chips`, and `format_display_price`.
5. **FastAPI Endpoints (`app/main.py`)**:
   - `POST /chat`: Multi-turn conversational endpoint returning reply, cards, filter chips, quick replies, and pagination flags.
   - `POST /chat/remove-filter`: Deterministic chip dismissal that recalculates active search results.
   - `GET /chat/{session_id}`: Full session state and message history retrieval.
   - Friendly validation error handler returning 400 Bad Request with customer-facing messages.
6. **Automated Testing Suite**:
   - `tests/test_apply_nlu.py`: Unit tests for normalizer integration and filter mutation.
   - `tests/test_chat_service.py`: 10 comprehensive flow tests covering all required customer journeys (a through l), grounding retry/fallback, and error isolation.
   - `tests/test_chat_api.py`: FastAPI endpoints integration tests verifying session continuity and filter removal.
7. **Smoke Test & Golden Dataset (`tests/golden_nlu.json`, `app/llm/smoke.py`)**:
   - 30 curated golden test cases evaluating field-by-field accuracy.
   - CLI evaluator executing against live `gemini-3.5-flash-lite` or fake client, demonstrating 3 end-to-end sample conversations.

### Verification
- `docker compose run --rm api ruff check .`: Clean, 0 errors.
- `docker compose run --rm api ruff format --check .`: Clean, 45 files formatted.
- `docker compose run --rm api pytest -v`: 55/55 tests passing (1.51s).
- Live Gemini evaluation of 30 golden cases: 100.0% field accuracy.
- Live 2-turn curl verification against `localhost:8000/chat`.
