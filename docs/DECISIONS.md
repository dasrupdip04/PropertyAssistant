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
