import json
from pathlib import Path
from typing import Any

from app.domain.catalog import Catalog
from app.domain.filters import SearchFilters
from app.llm.client import LLMClient
from app.llm.schema import NLUResult

PROMPTS_DIR = Path(__file__).parent / "prompts"
NLU_SYSTEM_PROMPT = (PROMPTS_DIR / "nlu.md").read_text(encoding="utf-8")


def build_nlu_payload(
    message: str,
    catalog: Catalog,
    current_filters: SearchFilters | None = None,
    pending_slots: list[str] | None = None,
    shown_count: int = 0,
    history_turns: list[dict[str, str]] | None = None,
) -> str:
    """Build the dynamic contextual payload for the NLU LLM call."""
    catalog_info = {
        "cities": catalog.cities,
        "sectors_by_city": catalog.sectors_by_city,
        "bhk_values": catalog.bhk_values,
        "price_min_lakh": float(catalog.price_min) if catalog.price_min is not None else None,
        "price_max_lakh": float(catalog.price_max) if catalog.price_max is not None else None,
    }

    # Format recent history (capped at last 4 turns)
    recent_history = []
    if history_turns:
        for turn in history_turns[-4:]:
            content = turn.get("content", "")
            if len(content) > 300:
                content = content[:300] + "..."
            recent_history.append({"role": turn.get("role", "customer"), "content": content})

    filters_dict = (
        current_filters.model_dump(mode="json", exclude_none=True) if current_filters else {}
    )

    payload_obj: dict[str, Any] = {
        "customer_message": message,
        "catalog": catalog_info,
        "current_filters": filters_dict,
        "pending_slots": pending_slots or [],
        "properties_shown": shown_count,
        "conversation_history": recent_history,
    }
    return json.dumps(payload_obj, indent=2)


def run_nlu(
    client: LLMClient,
    message: str,
    catalog: Catalog,
    current_filters: SearchFilters | None = None,
    pending_slots: list[str] | None = None,
    shown_count: int = 0,
    history_turns: list[dict[str, str]] | None = None,
) -> NLUResult:
    """Execute NLU extraction via LLMClient returning structured NLUResult."""
    payload = build_nlu_payload(
        message=message,
        catalog=catalog,
        current_filters=current_filters,
        pending_slots=pending_slots,
        shown_count=shown_count,
        history_turns=history_turns,
    )
    return client.generate_structured(
        system=NLU_SYSTEM_PROMPT,
        payload=payload,
        schema=NLUResult,
    )
