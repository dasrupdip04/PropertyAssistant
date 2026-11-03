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
