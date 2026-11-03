from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.chat.state import ChatState
from app.domain.catalog import Catalog
from app.domain.filters import SlotStatus
from app.domain.normalizers import (
    get_floor_band_range,
    normalize_sector,
    parse_bhk,
    parse_budget,
    parse_budget_string,
    resolve_city,
    resolve_project,
)
from app.llm.schema import FilterAction, IntentType, NLUResult


class NLUIssueType(StrEnum):
    UNKNOWN_CITY = "unknown_city"
    UNAVAILABLE_BHK = "unavailable_bhk"
    INVALID_BHK = "invalid_bhk"
    UNPARSEABLE_BUDGET = "unparseable_budget"
    INVALID_SECTOR = "invalid_sector"


class NLUIssue(BaseModel):
    issue_type: NLUIssueType
    slot: str
    raw_value: Any
    message: str
    available_options: list[Any] = Field(default_factory=list)


def apply_nlu(
    state: ChatState,
    nlu: NLUResult,
    catalog: Catalog,
) -> tuple[ChatState, list[str], list[NLUIssue]]:
    """Apply parsed NLUResult to ChatState using Step 2 normalizers.

    Returns (updated_state, assumptions, issues).
    """
    assumptions: list[str] = []
    issues: list[NLUIssue] = []

    # 1. Reset / starts_new_search
    if nlu.intent == IntentType.RESET or nlu.starts_new_search:
        state.reset_search_state()
        if nlu.intent == IntentType.RESET:
            assumptions.append("reset all search filters and conversation state")
            return state, assumptions, issues

    # 2. City
    city_action = nlu.filters.city.action
    if city_action == FilterAction.CLEAR:
        state.filters.city = None
        state.filters.sector = None
        state.filters.set_slot_status("city", SlotStatus.UNSPECIFIED)
        assumptions.append("cleared city filter")
    elif city_action == FilterAction.UNSURE:
        state.filters.mark_unsure("city")
    elif city_action == FilterAction.SET and nlu.filters.city.raw_text:
        raw_city = nlu.filters.city.raw_text
        res_city = resolve_city(raw_city, catalog)
        if res_city.value:
            city_changed = (
                state.filters.city is not None
                and state.filters.city.lower() != res_city.value.lower()
            )
            state.filters.city = res_city.value
            state.filters.set_slot_status("city", SlotStatus.SPECIFIED)
            status_val = res_city.status.value
            assumptions.append(f"resolved city '{raw_city}' -> '{res_city.value}' ({status_val})")

            # Changing city clears sector unless sector is specified in the same message
            if city_changed and nlu.filters.sector.action != FilterAction.SET:
                state.filters.sector = None
                state.filters.set_slot_status("sector", SlotStatus.UNSPECIFIED)
                assumptions.append("city changed; cleared sector filter")
        else:
            avail_cities = sorted(catalog.cities)
            issues.append(
                NLUIssue(
                    issue_type=NLUIssueType.UNKNOWN_CITY,
                    slot="city",
                    raw_value=raw_city,
                    message=(
                        f"I don't have properties listed in '{raw_city}'. "
                        f"Currently, we cover: {', '.join(avail_cities)}."
                    ),
                    available_options=avail_cities,
                )
            )

    # 3. Sector
    sector_action = nlu.filters.sector.action
    if sector_action == FilterAction.CLEAR:
        state.filters.sector = None
        state.filters.set_slot_status("sector", SlotStatus.UNSPECIFIED)
        assumptions.append("cleared sector filter")
    elif sector_action == FilterAction.UNSURE:
        state.filters.mark_unsure("sector")
    elif sector_action == FilterAction.SET and nlu.filters.sector.raw_text:
        raw_sec = nlu.filters.sector.raw_text
        norm_sec = normalize_sector(raw_sec)
        if norm_sec:
            state.filters.sector = norm_sec
            state.filters.set_slot_status("sector", SlotStatus.SPECIFIED)
            assumptions.append(f"normalized sector '{raw_sec}' -> '{norm_sec}'")
        else:
            issues.append(
                NLUIssue(
                    issue_type=NLUIssueType.INVALID_SECTOR,
                    slot="sector",
                    raw_value=raw_sec,
                    message=f"Could not recognize sector format '{raw_sec}'.",
                )
            )

    # 4. Project
    proj_action = nlu.filters.project.action
    if proj_action == FilterAction.CLEAR:
        state.filters.project = None
        state.filters.set_slot_status("project", SlotStatus.UNSPECIFIED)
    elif proj_action == FilterAction.UNSURE:
        state.filters.mark_unsure("project")
    elif proj_action == FilterAction.SET and nlu.filters.project.raw_text:
        raw_proj = nlu.filters.project.raw_text
        res_proj = resolve_project(raw_proj, catalog)
        if res_proj.value:
            state.filters.project = res_proj.value
            state.filters.set_slot_status("project", SlotStatus.SPECIFIED)
            assumptions.append(f"resolved project '{raw_proj}' -> '{res_proj.value}'")

    # 5. BHK
    bhk_action = nlu.filters.bhk.action
    if bhk_action == FilterAction.CLEAR:
        state.filters.bhk_in = None
        state.filters.bhk_min = None
        state.filters.set_slot_status("bhk", SlotStatus.UNSPECIFIED)
    elif bhk_action == FilterAction.UNSURE:
        state.filters.mark_unsure("bhk")
    elif bhk_action == FilterAction.SET:
        raw_bhk = nlu.filters.bhk.values or nlu.filters.bhk.raw_text or nlu.filters.bhk.min_value
        bhk_res = parse_bhk(raw_bhk, catalog)
        if bhk_res.missing_from_catalog:
            missing_str = ", ".join(f"{b} BHK" for b in bhk_res.missing_from_catalog)
            avail_str = ", ".join(f"{b} BHK" for b in bhk_res.available_catalog_bhks)
            issues.append(
                NLUIssue(
                    issue_type=NLUIssueType.UNAVAILABLE_BHK,
                    slot="bhk",
                    raw_value=raw_bhk,
                    message=(
                        f"We do not have {missing_str} listed in our inventory. "
                        f"Available options are {avail_str}."
                    ),
                    available_options=bhk_res.available_catalog_bhks,
                )
            )
        elif bhk_res.invalid_values and not bhk_res.bhk_values:
            issues.append(
                NLUIssue(
                    issue_type=NLUIssueType.INVALID_BHK,
                    slot="bhk",
                    raw_value=raw_bhk,
                    message="BHK configuration must be an integer between 1 and 10.",
                )
            )
        else:
            state.filters.bhk_in = bhk_res.bhk_values
            state.filters.set_slot_status("bhk", SlotStatus.SPECIFIED)
            assumptions.extend(bhk_res.assumptions)

    # 6. Budget
    budget_action = nlu.filters.budget.action
    if budget_action == FilterAction.CLEAR:
        state.filters.price_min_lakh = None
        state.filters.price_max_lakh = None
        state.filters.set_slot_status("budget", SlotStatus.UNSPECIFIED)
    elif budget_action == FilterAction.UNSURE:
        state.filters.mark_unsure("budget")
    elif budget_action == FilterAction.SET:
        b_filt = nlu.filters.budget
        if b_filt.high and b_filt.high.amount is not None:
            qual = b_filt.qualifier.value if b_filt.qualifier else None
            low_amt = b_filt.low.amount if b_filt.low else None
            pr = parse_budget(
                amount=b_filt.high.amount,
                unit=b_filt.high.unit,
                qualifier=qual,
                amount2=low_amt,
            )
            state.filters.price_min_lakh = pr.low_lakh
            state.filters.price_max_lakh = pr.high_lakh
            state.filters.set_slot_status("budget", SlotStatus.SPECIFIED)
            assumptions.extend(pr.assumptions)
        elif b_filt.raw_text:
            qual = b_filt.qualifier.value if b_filt.qualifier else None
            pr = parse_budget_string(b_filt.raw_text, qualifier=qual)
            if pr.low_lakh is not None or pr.high_lakh is not None:
                state.filters.price_min_lakh = pr.low_lakh
                state.filters.price_max_lakh = pr.high_lakh
                state.filters.set_slot_status("budget", SlotStatus.SPECIFIED)
                assumptions.extend(pr.assumptions)
            else:
                issues.append(
                    NLUIssue(
                        issue_type=NLUIssueType.UNPARSEABLE_BUDGET,
                        slot="budget",
                        raw_value=b_filt.raw_text,
                        message=f"Could not parse budget '{b_filt.raw_text}'.",
                    )
                )

    # 7. Floor
    floor_action = nlu.filters.floor.action
    if floor_action == FilterAction.CLEAR:
        state.filters.floor_min = None
        state.filters.floor_max = None
        state.filters.set_slot_status("floor", SlotStatus.UNSPECIFIED)
    elif floor_action == FilterAction.UNSURE:
        state.filters.mark_unsure("floor")
    elif floor_action == FilterAction.SET:
        if nlu.filters.floor.band:
            low, high = get_floor_band_range(nlu.filters.floor.band)
            state.filters.floor_min = low
            state.filters.floor_max = high
            state.filters.set_slot_status("floor", SlotStatus.SPECIFIED)
            band_str = f"{low} to {high or '+'}"
            assumptions.append(f"selected {nlu.filters.floor.band} floor band ({band_str})")
        elif nlu.filters.floor.min is not None or nlu.filters.floor.max is not None:
            state.filters.floor_min = nlu.filters.floor.min
            state.filters.floor_max = nlu.filters.floor.max
            state.filters.set_slot_status("floor", SlotStatus.SPECIFIED)

    # 8. Area
    area_action = nlu.filters.area.action
    if area_action == FilterAction.CLEAR:
        state.filters.area_min = None
        state.filters.area_max = None
        state.filters.set_slot_status("area", SlotStatus.UNSPECIFIED)
    elif area_action == FilterAction.UNSURE:
        state.filters.mark_unsure("area")
    elif area_action == FilterAction.SET:
        if nlu.filters.area.min is not None or nlu.filters.area.max is not None:
            state.filters.area_min = nlu.filters.area.min
            state.filters.area_max = nlu.filters.area.max
            state.filters.set_slot_status("area", SlotStatus.SPECIFIED)

    return state, assumptions, issues
