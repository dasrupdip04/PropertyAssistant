from collections import Counter
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.catalog import get_catalog
from app.domain.filters import SearchFilters, SlotStatus
from app.domain.normalizers import (
    compute_area_bands,
    format_band_label,
    get_floor_band,
)
from app.domain.search import build_base_conditions, build_range_conditions
from app.models import Property


class ActionType(StrEnum):
    RUN_SEARCH = "run_search"
    ASK_FOLLOWUPS = "ask_followups"
    ASK_CITY = "ask_city"


class RunSearch(BaseModel):
    action: ActionType = ActionType.RUN_SEARCH


class AskCity(BaseModel):
    action: ActionType = ActionType.ASK_CITY
    options: list[str]


class AskFollowups(BaseModel):
    action: ActionType = ActionType.ASK_FOLLOWUPS
    slots: list[str]
    options: dict[str, Any]
    include_unsure_hint: bool = False


Action = RunSearch | AskCity | AskFollowups


class DialogueState(BaseModel):
    filters: SearchFilters = Field(default_factory=SearchFilters)
    followups_asked: int = 0
    wants_results_now: bool = False
    unsure_hint_given: bool = False


def compute_friendly_price_bands(prices: list[Any]) -> list[dict[str, Any]]:
    if not prices:
        return []
    p_floats = sorted(float(p) for p in prices)
    min_p, max_p = p_floats[0], p_floats[-1]

    if len(set(p_floats)) <= 2 or (max_p - min_p) < 15:
        mid = round((min_p + max_p) / 10.0) * 5.0
        return [
            {"label": format_band_label(None, mid), "max_lakh": mid},
            {"label": format_band_label(mid, None), "min_lakh": mid},
        ]

    q1_raw = p_floats[len(p_floats) // 4]
    q2_raw = p_floats[len(p_floats) // 2]
    q3_raw = p_floats[(3 * len(p_floats)) // 4]

    def round_friendly(val: float) -> float:
        if val >= 100.0:
            return round(val / 10.0) * 10.0
        return round(val / 5.0) * 5.0

    c1 = round_friendly(q1_raw)
    c2 = round_friendly(q2_raw)
    c3 = round_friendly(q3_raw)

    if c1 < min_p:
        c1 = min_p
    if c2 <= c1:
        c2 = c1 + (10.0 if c1 >= 100.0 else 5.0)
    if c3 <= c2:
        c3 = c2 + (10.0 if c2 >= 100.0 else 5.0)

    return [
        {"label": format_band_label(None, c1), "max_lakh": c1},
        {"label": format_band_label(c1, c2), "min_lakh": c1, "max_lakh": c2},
        {"label": format_band_label(c2, c3), "min_lakh": c2, "max_lakh": c3},
        {"label": format_band_label(c3, None), "min_lakh": c3},
    ]


def decide_next_action(state: DialogueState, session: Session) -> Action:
    """Decide dialogue policy next action based on filters, candidate properties, and state."""
    settings = get_settings()
    catalog = get_catalog(session)

    # 1. Sector in multiple cities without a city given
    if state.filters.sector and not state.filters.city:
        cities = catalog.cities_for_sector(state.filters.sector)
        if len(cities) > 1:
            return AskCity(options=cities)

    # 2. Fast search triggers
    if state.wants_results_now:
        return RunSearch()

    if state.followups_asked >= 2:
        return RunSearch()

    # Query current candidate set
    conds = build_base_conditions(state.filters) + build_range_conditions(state.filters)
    stmt = select(Property).where(*conds)
    candidates = session.execute(stmt).scalars().all()
    candidate_count = len(candidates)

    if candidate_count <= settings.FOLLOWUP_SKIP_THRESHOLD:
        return RunSearch()

    # 3. Analyze candidate attributes to evaluate distinct values
    distinct_cities = sorted({c.city for c in candidates if c.city})
    prices = sorted([c.price_lakh for c in candidates if c.price_lakh is not None])
    distinct_bhks = sorted(
        {c.configuration_bhk for c in candidates if c.configuration_bhk is not None}
    )
    distinct_sectors = sorted({c.sector for c in candidates if c.sector})
    floors = [c.floor for c in candidates if c.floor is not None]
    distinct_floor_bands = sorted({get_floor_band(f) for f in floors})
    areas = [c.area_sqft for c in candidates if c.area_sqft is not None]

    def is_slot_askable(slot: str) -> bool:
        status = state.filters.get_slot_status(slot)
        if status in (SlotStatus.SPECIFIED, SlotStatus.UNSURE):
            return False

        if slot == "city":
            return len(distinct_cities) > 1
        elif slot == "budget":
            return len(set(prices)) > 1
        elif slot == "bhk":
            return len(distinct_bhks) > 1
        elif slot == "sector":
            return len(distinct_sectors) > 1
        elif slot == "floor":
            return len(distinct_floor_bands) > 1
        elif slot == "area":
            return len(set(areas)) > 1
        return False

    # Priority order: city, budget, bhk, sector, floor, area
    higher_priority = ["city", "budget", "bhk", "sector"]
    higher_askable = [s for s in higher_priority if is_slot_askable(s)]

    slots_to_ask: list[str] = []
    if len(higher_askable) >= 2:
        slots_to_ask = higher_askable[:2]
    else:
        slots_to_ask = list(higher_askable)
        lower_priority = ["floor", "area"]
        lower_askable = [s for s in lower_priority if is_slot_askable(s)]
        for s in lower_askable:
            if len(slots_to_ask) < 2:
                slots_to_ask.append(s)

    if not slots_to_ask:
        return RunSearch()

    # 4. Generate options for the selected slots
    options: dict[str, Any] = {}
    for slot in slots_to_ask:
        if slot == "city":
            options["city"] = distinct_cities
        elif slot == "budget":
            options["budget"] = compute_friendly_price_bands(prices)
        elif slot == "bhk":
            options["bhk"] = distinct_bhks
        elif slot == "sector":
            sector_counts = Counter(c.sector for c in candidates if c.sector)
            options["sector"] = [
                {"sector": s, "count": sector_counts[s]} for s in sorted(sector_counts.keys())
            ]
        elif slot == "floor":
            options["floor"] = [b for b in ["low", "mid", "high"] if b in distinct_floor_bands]
        elif slot == "area":
            options["area"] = compute_area_bands(areas)

    include_unsure_hint = not state.unsure_hint_given
    state.unsure_hint_given = True
    state.followups_asked += len(slots_to_ask)

    return AskFollowups(
        slots=slots_to_ask,
        options=options,
        include_unsure_hint=include_unsure_hint,
    )
