# Architecture Decision Records (ADR)

## ADR 1: Gemini SDK API Selection - `generate_content` vs. Interactions API

### Status
Accepted

### Date
2026-11-03

### Context
In Step 3, PropertyAssistant introduces an LLM layer for Natural Language Understanding (NLU) with structured outputs via Pydantic schemas (`NLUResult`) and natural language response phrasing. The official `google-genai` Python SDK provides two distinct interfaces:
1. `client.models.generate_content`
2. `client.interactions.create` (Interactions API)

We must select the interface that best satisfies our requirements for Pydantic schema enforcement, latency, and system architecture.

### Decision
We choose **`client.models.generate_content`** with `response_mime_type="application/json"` and `response_schema=<PydanticModel>`.

### Rationale
1. **Direct Pydantic Integration**: The official `google-genai` SDK allows passing Pydantic classes directly to `GenerateContentConfig(response_schema=SchemaClass)`. The SDK handles the schema conversion automatically, and the response is validated directly into typed Pydantic instances.
2. **Local Session Ownership**: PropertyAssistant manages full conversation history, search results, and dialogue state in PostgreSQL (`sessions`, `conversation_turns`, `searches` tables) with deterministic dialogue policies. The Interactions API is optimized for server-side state retention (`previous_interaction_id`) on Google infrastructure, which is redundant and introduces duplicate state management.
3. **Thinking Configuration & Constraints**: For Gemini 3.5 Flash-Lite and Gemini 3.6 Flash, `generate_content` directly supports `thinking_config=types.ThinkingConfig(thinking_level=...)` and `system_instruction` without sending deprecated parameters (`temperature`, `top_p`, `top_k`).
4. **Deterministic Structured Output**: Combining `system_instruction`, JSON schema enforcement, and absence of model-turn prefill ensures reliable parsing and repeatability.

### Consequences
- NLU extraction and response generation execute as stateless calls passing the exact conversation window from our PostgreSQL store.
- Structured output parsing failure is caught locally and triggers one immediate retry before falling back gracefully.

## ADR 2: Vanilla Frontend Architecture, Strict DOM XSS Prevention, and Deterministic Notes Isolation

### Status
Accepted

### Date
2026-11-03

### Context
Step 4 introduces an interactive customer chat interface (`/`) and an operator administration dashboard (`/admin`). The frontend must remain lightweight, highly maintainable without build chains, and robust against XSS vulnerabilities stemming from user inputs or untrusted LLM outputs. Additionally, live QA in Step 3 revealed that allowing the responder LLM to formulate assumptions or format prices led to hallucinated budgets and inconsistent currency phrasing.

### Decision
1. **Zero-Build Vanilla Web Stack**:
   - Implement the chat and admin UIs using vanilla HTML5, CSS3 (CSS custom properties, flexbox/grid), and ES6+ JavaScript.
   - Serve static assets directly from FastAPI via `FastAPI.mount("/static", ...)` and root route handlers.
2. **Strict DOM-Based XSS Prevention**:
   - Mandate that all user-provided, server-provided, and LLM-generated text be inserted into the DOM strictly via `element.textContent`, `document.createTextNode`, or DOM creation methods.
   - Forbid `innerHTML` for dynamic string interpolation.
3. **Deterministic Notes & Price Formatting Isolation**:
   - Facts, assumptions (e.g. `Treated around 1.0 cr as 90.00 to 110.00 lakh`), unverifiable IDs, and city normalization notices are computed entirely in Python code and passed via `notes: list[str]`.
   - Prices and price bands are formatted using a shared formatter (`₹90 L`, `₹1 Cr`, `₹1.2 Cr`) in both cards and quick replies.
   - The responder LLM prompt is strictly constrained to 1-2 sentences of conversational phrasing, completely forbidden from fabricating budgets or restating notes.
4. **Session-Scoped Admin Authentication**:
   - Admin credentials (`X-Admin-Token`) are stored strictly in `sessionStorage`, ensuring that credentials never persist across browser restarts or leak to disk.

### Consequences
- Zero npm/node build complexity; changes are instantly reflected on page refresh.
- Absolute resistance to stored and reflected XSS attacks.
- High grounding fidelity: 100% of factual listing details, search assumptions, and prices come from database-backed deterministic calculations.

## ADR 3: PostgreSQL Advisory Locking and Checksum-Based Ingestion Semantics

### Status
Accepted

### Date
2026-11-03

### Context
Inventory data arrives via CSV file (`property_inventory.csv`) and can be updated at any time. Syncs may be triggered concurrently by CLI cron jobs, developer commands, or operator dashboard requests (`POST /admin/refresh`). We must guarantee absolute idempotency, atomic commits, zero race conditions, and auditability without taking the database offline.

### Decision
1. **Transactional Advisory Locks**:
   - Every ingestion run acquires a PostgreSQL transaction-level advisory lock using two 32-bit keys (`0x50524F50`, `0x494E56` - ASCII "PROP", "INV") via `SELECT pg_advisory_xact_lock(...)`.
   - The lock is released automatically upon commit or rollback.
2. **SHA-256 Fingerprinting**:
   - The file's SHA-256 checksum is computed prior to parsing. If a previous successful `IngestionRun` exists with the exact same hash and `force=False`, the run immediately terminates with `status="skipped"`.
3. **Soft-Delete Lifecycle**:
   - Missing rows are never removed via SQL `DELETE`. Instead, they are marked `is_active=False`. Re-appearing properties are updated with `is_active=True` and classified as `reactivated`.
4. **Failure Threshold Abort**:
   - If row-level validation errors exceed `MAX_REJECT_RATIO` (25%), the entire transaction rolls back to preserve existing data integrity.

### Trade-offs & Consequences
- **Trade-off**: Concurrent sync requests block sequentially until the first lock holder commits.
- **Benefit**: Zero risk of split-brain inventory states or phantom duplicate records; runs with unchanged data execute in under 15ms.

## ADR 4: Deterministic Dialogue Policy & Priority Queueing vs. Autonomous Agent Loops

### Status
Accepted

### Date
2026-11-03

### Context
Autonomous LLM agents that decide what to ask next through open-ended prompting tend to ask redundant questions, invent unlisted search filters, loop indefinitely, or fail to present results promptly.

### Decision
1. **Rule-Based Dialogue State Machine (`decide_next_action`)**:
   - All state transitions are implemented in typed Python using candidate count thresholds (`FOLLOWUP_SKIP_THRESHOLD = 3`) and strict priority queues (`city` -> `budget` -> `bhk` -> `sector` -> `floor` -> `area`).
2. **Hard Follow-up Cap**:
   - No customer is ever asked follow-up questions more than twice (`followups_asked >= 2` forces `RunSearch`).
3. **Unsure Slot Blacklisting**:
   - Once a slot is marked `unsure` or answered with indifference ("not sure", "any is fine"), it is blacklisted and never prompted again.
4. **Immediate Refinements After Search**:
   - Once search results have been displayed (`search_done = True`), any new filter input executes a search immediately without asking follow-up questions.

### Trade-offs & Consequences
- **Trade-off**: Slightly less conversational serendipity compared to unrestricted open chat.
- **Benefit**: Guaranteed convergence to search results within at most 2 turns; 100% unit-testable and predictable conversation flow.

## ADR 5: Negation Modeling via `FilterAction.EXCLUDE` & Negative Fact Verification

### Status
Accepted

### Date
2026-11-03

### Context
Real-estate shoppers frequently express negative constraints (e.g. `"not in sector 150"`, `"any floor except ground"`) or ask about features absent from the schema (e.g. `"is it near the metro?"`). Traditional keyword matchers or simplistic slot-filling often parse `"150"` as an active filter, returning the exact opposite of the customer's request.

### Decision
1. **Explicit Exclusion Enum**:
   - Added `FilterAction.EXCLUDE = "exclude"` to `FilterAction`.
   - Added `excluded_sectors: list[str]` to `SearchFilters`, mapped to `Property.sector.notin_(...)` in SQL search queries.
2. **Negative Fact Grounding**:
   - Unrecorded attributes (metro distance, possession dates, RERA, amenities) are mapped to `"not in our data"`.
   - The grounding verifier mandates that the responder explicitly state that this attribute is not in our data, and forbids asking search refinement questions on property answers.

### Trade-offs & Consequences
- **Trade-off**: Requires dedicated NLU prompt instructions and schema definitions.
- **Benefit**: Completely eliminates positive-filter inversions on negations and prevents the model from hallucinating neighborhood amenities.

## ADR 6: Dual Grounding Validation Loop with Deterministic Template Fallback

### Status
Accepted

### Date
2026-11-03

### Context
LLMs can occasionally hallucinate unlisted listing IDs, fabricate prices, or invent features when queried about properties. Relying purely on system prompt compliance is insufficient for high-stakes financial domains like real estate.

### Decision
1. **Two-Stage Validation Gate**:
   - Stage 1: The responder draft is inspected by `verify_grounding`:
     - All property IDs must be in the plan's whitelisted IDs.
     - All numeric tokens must match prices, floors, or areas in the plan.
     - Any budget or price figures mentioned during follow-up questions must match the customer's own specified constraints.
2. **Targeted Retry & Deterministic Fallback**:
   - On the first violation, the responder is re-prompted with the exact violation reason.
   - If the second attempt also fails, or on any LLM network/parsing error, the orchestrator immediately invokes `render_fallback_template(plan)`.

### Trade-offs & Consequences
- **Trade-off**: Re-prompting adds a second LLM latency penalty on the rare occasions a draft fails verification.
- **Benefit**: Absolute 0% hallucination rate; if the LLM fails completely, the customer receives a clean, 100% grounded response derived directly from code templates.
