import datetime
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.chat.apply import apply_nlu
from app.chat.grounding import render_fallback_template, verify_grounding
from app.chat.models import ConversationTurn, SearchLog, SessionModel
from app.chat.state import ChatState, LastSearchState, ShownItem
from app.config import get_settings
from app.domain.catalog import get_catalog
from app.domain.filters import SlotStatus
from app.domain.policy import (
    AskCity,
    AskFollowups,
    DialogueState,
    decide_next_action,
)
from app.domain.relax import explain_no_results
from app.domain.search import get_property, search_properties
from app.llm.client import LLMClient, session_limiter
from app.llm.nlu import run_nlu
from app.llm.schema import AskedAttribute, IntentType, NLUResult, ReferenceKind, SortType

logger = logging.getLogger(__name__)


def json_serializable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return str(obj)


RESPONDER_PROMPT = """You are PropertyAssistant, a warm, trustworthy, and expert real-estate
consultant. You phrase a response to the customer based STRICTLY on the ResponsePlan.

### Absolute Hard Constraints
1. Length: 1 or 2 warm, concise sentences only.
2. Language: Reply in the customer's language. If English, reply in natural English with NO
   Hindi or 'Namaste'. If Hinglish, reply in warm Hinglish.
3. No Greeting on Follow-ups: Greet ONLY on the first turn. NEVER use greetings ('Hello',
   'Hi', 'Namaste') on turn 2 onwards.
4. No Filler: Avoid filler phrases like 'exciting step', 'thrilled', or 'great choice'.
5. Do Not Repeat UI Elements: Listing cards and notes are rendered separately by UI. Do NOT
   invite customer to look at cards, do NOT re-list cards or specs, and do NOT repeat notes.
6. Price Formatting: Refer to prices exactly as formatted on cards (e.g. ₹95 L, ₹1.2 Cr).
7. Never Assume: Never assume, invent, or guess a budget, sector, or BHK when asking follow-up
   questions. Only ask what their preference is.
8. Single Refinement: After results are shown, suggest at most ONE next refinement (e.g.
   narrowing by sector, or viewing cheaper options).
9. Grounding:
   - Any property ID (PROP-###) mentioned MUST exist in the plan.
   - Any price, floor, area, or BHK mentioned MUST exist in the plan.
   - For missing values, say 'not listed'. For absent attributes, say 'not in our data'.
   - Direct lookup of sold or reserved: answer truthfully with their status ('sold'/'reserved').
     NEVER recommend them as available.
   - NEVER invent facts, amenities, investment advice, or unlisted properties.
10. Unsure Hint: Mention 'not sure' is fine ONLY when include_unsure_hint is true in plan.
"""


class PropertyCard(BaseModel):
    position: int
    property_id: str
    project: str
    city: str
    sector: str
    bhk: int
    price_lakh: float | None = None
    display_price: str
    floor: int | None = None
    area_sqft: int | None = None
    price_per_sqft: float | None = None
    status: str
    updated_at: str
    updated_days_ago: int
    tags: list[str] = Field(default_factory=list)


class FilterChip(BaseModel):
    slot: str
    label: str


class ChatResponsePayload(BaseModel):
    session_id: str
    reply: str
    cards: list[PropertyCard] = Field(default_factory=list)
    filter_chips: list[FilterChip] = Field(default_factory=list)
    quick_replies: list[str] = Field(default_factory=list)
    has_more: bool = False
    notes: list[str] = Field(default_factory=list)
    debug: dict[str, Any] | None = None


def format_display_price(price_lakh: float | None) -> str:
    if price_lakh is None:
        return "Price not listed"
    if price_lakh >= 100.0:
        cr = price_lakh / 100.0
        val = f"{cr:.2f}".rstrip("0").rstrip(".")
        return f"₹{val} Cr"
    val = f"{price_lakh:.2f}".rstrip("0").rstrip(".")
    return f"₹{val} L"


def calculate_days_ago(date_str: str | datetime.date) -> int:
    try:
        if isinstance(date_str, datetime.date):
            d = date_str
        else:
            d = datetime.date.fromisoformat(str(date_str))
        now = datetime.date.today()
        return max(0, (now - d).days)
    except Exception:
        return 0


def format_cards(rows: list[dict[str, Any]], start_pos: int = 1) -> list[PropertyCard]:
    if not rows:
        return []

    # Tag calculations
    valid_prices = [float(r["price_lakh"]) for r in rows if r.get("price_lakh") is not None]
    min_price = min(valid_prices) if valid_prices else None

    valid_areas = [int(r["area_sqft"]) for r in rows if r.get("area_sqft") is not None]
    max_area = max(valid_areas) if valid_areas else None

    valid_pps = [float(r["price_per_sqft"]) for r in rows if r.get("price_per_sqft") is not None]
    min_pps = min(valid_pps) if valid_pps else None

    cards: list[PropertyCard] = []
    for idx, r in enumerate(rows):
        pos = start_pos + idx
        p_lakh = float(r["price_lakh"]) if r.get("price_lakh") is not None else None
        p_area = int(r["area_sqft"]) if r.get("area_sqft") is not None else None
        p_pps = float(r["price_per_sqft"]) if r.get("price_per_sqft") is not None else None

        tags: list[str] = []
        if p_lakh is not None and min_price is not None and p_lakh == min_price:
            tags.append("cheapest")
        if p_area is not None and max_area is not None and p_area == max_area:
            tags.append("largest")
        if p_pps is not None and min_pps is not None and p_pps == min_pps:
            tags.append("best price/sqft")

        card = PropertyCard(
            position=pos,
            property_id=r["property_id"],
            project=r["project_name"],
            city=r["city"],
            sector=r["sector"],
            bhk=r["configuration_bhk"],
            price_lakh=p_lakh,
            display_price=format_display_price(p_lakh),
            floor=r.get("floor"),
            area_sqft=p_area,
            price_per_sqft=p_pps,
            status=r["availability"],
            updated_at=str(r["listing_updated_at"]),
            updated_days_ago=calculate_days_ago(r["listing_updated_at"]),
            tags=tags,
        )
        cards.append(card)

    return cards


def build_filter_chips(filters: Any) -> list[FilterChip]:
    chips: list[FilterChip] = []
    if filters.city:
        chips.append(FilterChip(slot="city", label=f"City: {filters.city}"))
    if filters.sector:
        chips.append(FilterChip(slot="sector", label=f"Sector: {filters.sector}"))
    if filters.bhk_in:
        bhk_str = "/".join(str(b) for b in filters.bhk_in)
        chips.append(FilterChip(slot="bhk", label=f"{bhk_str} BHK"))
    if filters.price_min_lakh or filters.price_max_lakh:
        if filters.price_min_lakh and filters.price_max_lakh:
            b_lbl = f"₹{filters.price_min_lakh}-{filters.price_max_lakh} L"
            chips.append(FilterChip(slot="budget", label=b_lbl))
        elif filters.price_max_lakh:
            chips.append(FilterChip(slot="budget", label=f"Under ₹{filters.price_max_lakh} L"))
        elif filters.price_min_lakh:
            chips.append(FilterChip(slot="budget", label=f"Above ₹{filters.price_min_lakh} L"))
    if filters.floor_min or filters.floor_max:
        chips.append(FilterChip(slot="floor", label="Floor filter"))
    if filters.area_min or filters.area_max:
        chips.append(FilterChip(slot="area", label="Area filter"))
    return chips


def handle_turn(
    session_id: str | None,
    message: str,
    db: Session,
    llm_client: LLMClient,
) -> ChatResponsePayload:
    """Orchestrate a single conversational turn."""
    start_time = time.perf_counter()

    # 1. Validation
    if not message or not message.strip():
        raise ValueError("Message cannot be empty")
    if len(message) > 1000:
        raise ValueError("Message exceeds maximum length of 1000 characters")

    # 2. Session load / create
    sess_uuid: uuid.UUID
    if session_id:
        try:
            sess_uuid = uuid.UUID(session_id)
        except ValueError:
            sess_uuid = uuid.uuid4()
    else:
        sess_uuid = uuid.uuid4()

    session_rec = db.query(SessionModel).filter(SessionModel.id == sess_uuid).first()
    if not session_rec:
        state = ChatState()
        session_rec = SessionModel(id=sess_uuid, state=state.model_dump(mode="json"))
        db.add(session_rec)
        db.commit()
    else:
        state = ChatState.model_validate(session_rec.state)

    # Rate Limiter check (per session, max 12 requests per minute)
    if session_id and not session_limiter.is_allowed(session_id):
        reply = "You are sending messages too quickly. Please wait a moment before trying again."
        chips = build_filter_chips(state.filters)
        return ChatResponsePayload(
            session_id=str(sess_uuid),
            reply=reply,
            cards=[],
            filter_chips=chips,
            quick_replies=["Wait a moment"],
            has_more=False,
            notes=[],
        )

    history_records = (
        db.query(ConversationTurn)
        .filter(ConversationTurn.session_id == sess_uuid)
        .order_by(ConversationTurn.id.asc())
        .all()
    )
    history_turns = [{"role": t.role, "content": t.content} for t in history_records]
    is_first_turn = len(history_turns) == 0

    catalog = get_catalog(db)
    shown_count = len(state.last_search.shown) if state.last_search else 0

    # 3. NLU call
    nlu_res: NLUResult
    try:
        nlu_res = run_nlu(
            client=llm_client,
            message=message,
            catalog=catalog,
            current_filters=state.filters,
            pending_slots=state.pending_slots,
            shown_count=shown_count,
            history_turns=history_turns,
        )
    except Exception as e:
        logger.warning("NLU execution error: %s", e)
        nlu_res = NLUResult(intent=IntentType.OUT_OF_SCOPE)
        plan_data = {
            "type": "nlu_failed",
            "message": (
                "I am sorry, I did not quite catch that. "
                "Could you please rephrase what you are looking for?"
            ),
        }
        reply = render_fallback_template(plan_data)
        return _persist_and_return(
            db=db,
            session_rec=session_rec,
            sess_uuid=sess_uuid,
            customer_message=message,
            reply=reply,
            state=state,
            debug={"intent": "nlu_failed", "error": str(e)},
        )

    # 4. Route by intent
    plan_data: dict[str, Any] = {}
    cards: list[PropertyCard] = []
    quick_replies: list[str] = []
    assumptions: list[str] = []
    search_log_entry: dict[str, Any] | None = None

    if nlu_res.intent == IntentType.RESET:
        state.reset_search_state()
        plan_data = {
            "type": "smalltalk",
            "message": (
                "Your search has been reset. What location, budget, or BHK are you looking for?"
            ),
        }
        quick_replies = ["3 BHK in Noida", "Gurugram under 1.5 Cr", "Sector 150"]

    elif nlu_res.intent == IntentType.SMALLTALK:
        plan_data = {
            "type": "smalltalk",
            "message": (
                "Hello! I am your real estate consultant. "
                "How can I help with your home search today?"
            ),
        }
        quick_replies = ["3 BHK in Noida", "Apartments in Gurugram"]

    elif nlu_res.intent == IntentType.OUT_OF_SCOPE:
        plan_data = {
            "type": "out_of_scope",
            "message": "I can only assist with verified residential listings in our catalog.",
        }

    elif nlu_res.intent == IntentType.SHOW_MORE:
        if not state.last_search or not state.search_done:
            plan_data = {
                "type": "clarify_reference",
                "message": (
                    "You have not performed a search yet. Which city or BHK are you interested in?"
                ),
            }
        else:
            page_size = 5
            new_offset = state.last_search.offset + page_size
            search_res = search_properties(
                session=db,
                filters=state.filters,
                sort=state.last_search.sort,
                limit=page_size,
                offset=new_offset,
            )
            start_pos = len(state.last_search.shown) + 1
            new_cards = format_cards(search_res.rows, start_pos=start_pos)
            cards = new_cards
            for c in new_cards:
                state.last_search.shown.append(
                    ShownItem(position=c.position, property_id=c.property_id)
                )
            state.last_search.offset = new_offset

            plan_data = {
                "type": "show_results",
                "rows": search_res.rows,
                "total": search_res.total,
                "applied_filters": state.filters.model_dump(mode="json", exclude_none=True),
                "assumptions": [],
                "unverifiable_ids": search_res.unverifiable_ids,
                "sort": state.last_search.sort,
                "has_more": (new_offset + len(search_res.rows) < search_res.total),
            }

    elif nlu_res.intent == IntentType.PROPERTY_QUESTION:
        target_pid = None
        ref = nlu_res.reference

        if ref.kind == ReferenceKind.PROPERTY_ID and ref.property_ids:
            target_pid = ref.property_ids[0]
        elif ref.kind == ReferenceKind.ORDINAL and ref.ordinal is not None:
            if not state.last_search or not state.last_search.shown:
                plan_data = {
                    "type": "clarify_reference",
                    "message": "No properties have been shown yet to reference by position.",
                }
            else:
                target_pid = state.last_search.get_by_position(ref.ordinal)
                if not target_pid:
                    plan_data = {
                        "type": "clarify_reference",
                        "message": (
                            f"Position {ref.ordinal} is not among the currently shown properties "
                            f"(we have shown {len(state.last_search.shown)} listings)."
                        ),
                    }
        elif ref.kind == ReferenceKind.LAST:
            if not state.last_search or not state.last_search.shown:
                plan_data = {
                    "type": "clarify_reference",
                    "message": "No properties have been shown yet.",
                }
            else:
                target_pid = state.last_search.get_last()

        if not plan_data and target_pid:
            p = get_property(db, target_pid)
            if not p:
                plan_data = {
                    "type": "clarify_reference",
                    "message": f"Property '{target_pid}' was not found in our records.",
                }
            else:
                attrs: dict[str, Any] = {}
                for attr in nlu_res.asked_attributes:
                    if attr == AskedAttribute.FLOOR:
                        attrs["floor"] = p["floor"]
                    elif attr == AskedAttribute.PRICE:
                        attrs["price_lakh"] = (
                            float(p["price_lakh"]) if p["price_lakh"] is not None else None
                        )
                    elif attr == AskedAttribute.AREA:
                        attrs["area_sqft"] = p["area_sqft"]
                    elif attr == AskedAttribute.STATUS:
                        attrs["availability"] = p["availability"]
                    elif attr == AskedAttribute.LOCATION:
                        attrs["location"] = p["location_raw"]
                    elif attr == AskedAttribute.BHK:
                        attrs["bhk"] = p["configuration_bhk"]
                    elif attr == AskedAttribute.PRICE_PER_SQFT:
                        attrs["price_per_sqft"] = (
                            float(p["price_per_sqft"]) if p["price_per_sqft"] is not None else None
                        )
                    elif attr == AskedAttribute.PROJECT:
                        attrs["project_name"] = p["project_name"]
                    elif attr == AskedAttribute.OTHER:
                        attrs["metro distance / amenities"] = "not in our data"

                plan_data = {
                    "type": "property_answer",
                    "property_id": target_pid,
                    "project_name": p["project_name"],
                    "status": p["availability"],
                    "attributes": attrs,
                }
        elif not plan_data:
            plan_data = {
                "type": "clarify_reference",
                "message": "Could you please specify which property you are asking about?",
            }

    elif nlu_res.intent == IntentType.COMPARE:
        plan_data = {
            "type": "compare",
            "properties": [
                get_property(db, item.property_id)
                for item in (state.last_search.shown[:2] if state.last_search else [])
            ],
        }

    else:  # SEARCH
        state, assumptions, issues = apply_nlu(state, nlu_res, catalog)
        if issues:
            plan_data = {"type": "issue", "issues": [i.model_dump() for i in issues]}
        else:
            page_size = 5
            # If search was already done, refinements execute immediately
            if state.search_done:
                sort_override = (
                    nlu_res.sort.value
                    if nlu_res.sort != SortType.NONE
                    else (state.last_search.sort if state.last_search else None)
                )
                search_res = search_properties(
                    db, state.filters, sort=sort_override, limit=page_size, offset=0
                )
                if search_res.total == 0:
                    expl = explain_no_results(db, state.filters)
                    plan_data = {
                        "type": "no_results",
                        "explanation": expl.model_dump(mode="json"),
                        "applied_filters": state.filters.model_dump(mode="json", exclude_none=True),
                    }
                else:
                    cards = format_cards(search_res.rows, start_pos=1)
                    state.last_search = LastSearchState(
                        shown=[
                            ShownItem(position=c.position, property_id=c.property_id) for c in cards
                        ],
                        total=search_res.total,
                        offset=0,
                        sort=search_res.applied_sort,
                    )
                    plan_data = {
                        "type": "show_results",
                        "rows": search_res.rows,
                        "total": search_res.total,
                        "applied_filters": state.filters.model_dump(mode="json", exclude_none=True),
                        "assumptions": assumptions,
                        "unverifiable_ids": search_res.unverifiable_ids,
                        "sort": search_res.applied_sort,
                        "has_more": (len(search_res.rows) < search_res.total),
                    }
                    search_log_entry = {
                        "filters": state.filters.model_dump(mode="json"),
                        "sort": search_res.applied_sort,
                        "result_ids": [r["property_id"] for r in search_res.rows],
                        "total": search_res.total,
                    }
            else:
                # Pre-result policy check
                policy_state = DialogueState(
                    filters=state.filters,
                    followups_asked=state.followups_asked,
                    wants_results_now=nlu_res.wants_results_now,
                    unsure_hint_given=state.include_unsure_hint_used,
                )
                action = decide_next_action(policy_state, db)
                if isinstance(action, AskCity):
                    state.pending_slots = ["city"]
                    plan_data = {
                        "type": "ask_city",
                        "applied_filters": state.filters.model_dump(mode="json", exclude_none=True),
                        "is_first_turn": is_first_turn,
                    }
                    quick_replies = list(action.options)
                elif isinstance(action, AskFollowups):
                    state.pending_slots = action.slots
                    state.followups_asked += len(action.slots)
                    state.include_unsure_hint_used = True
                    plan_data = {
                        "type": "ask_followups",
                        "slots": action.slots,
                        "include_unsure_hint": action.include_unsure_hint,
                        "applied_filters": state.filters.model_dump(mode="json", exclude_none=True),
                        "is_first_turn": is_first_turn,
                    }
                    # Generate quick replies: option bands ONLY go into quick_replies!
                    for opt_list in action.options.values():
                        if isinstance(opt_list, list):
                            for item in opt_list[:4]:
                                if isinstance(item, dict) and "label" in item:
                                    quick_replies.append(item["label"])
                                elif isinstance(item, dict) and "sector" in item:
                                    quick_replies.append(f"Sector {item['sector']}")
                                elif isinstance(item, (str, int)):
                                    quick_replies.append(str(item))
                    if action.include_unsure_hint:
                        quick_replies.append("Not sure")
                    quick_replies.append("Just show me what you have")
                else:  # RunSearch
                    state.search_done = True
                    state.pending_slots = []
                    sort_override = nlu_res.sort.value if nlu_res.sort != SortType.NONE else None
                    search_res = search_properties(
                        db, state.filters, sort=sort_override, limit=page_size, offset=0
                    )
                    if search_res.total == 0:
                        expl = explain_no_results(db, state.filters)
                        plan_data = {
                            "type": "no_results",
                            "explanation": expl.model_dump(mode="json"),
                            "applied_filters": state.filters.model_dump(
                                mode="json", exclude_none=True
                            ),
                        }
                    else:
                        cards = format_cards(search_res.rows, start_pos=1)
                        state.last_search = LastSearchState(
                            shown=[
                                ShownItem(position=c.position, property_id=c.property_id)
                                for c in cards
                            ],
                            total=search_res.total,
                            offset=0,
                            sort=search_res.applied_sort,
                        )
                        plan_data = {
                            "type": "show_results",
                            "rows": search_res.rows,
                            "total": search_res.total,
                            "applied_filters": state.filters.model_dump(
                                mode="json", exclude_none=True
                            ),
                            "assumptions": assumptions,
                            "unverifiable_ids": search_res.unverifiable_ids,
                            "sort": search_res.applied_sort,
                            "has_more": (len(search_res.rows) < search_res.total),
                        }
                        search_log_entry = {
                            "filters": state.filters.model_dump(mode="json"),
                            "sort": search_res.applied_sort,
                            "result_ids": [r["property_id"] for r in search_res.rows],
                            "total": search_res.total,
                        }

    # 5. Phrasing with Responder & Grounding
    responder_payload = json.dumps(plan_data, default=json_serializable, indent=2)
    final_reply: str = ""
    grounding_path = "llm"

    try:
        candidate_reply = llm_client.generate_text(
            system=RESPONDER_PROMPT,
            payload=responder_payload,
        )
        is_grounded, violation = verify_grounding(candidate_reply, plan_data)
        if is_grounded:
            final_reply = candidate_reply
        else:
            logger.warning(
                "Grounding violation on attempt 1 (%s), retrying responder...", violation
            )
            retry_payload = json.dumps(
                {
                    "plan": plan_data,
                    "violation": violation,
                    "instruction": (
                        "Fix the violation. Only mention property IDs and numbers directly "
                        "present in the plan."
                    ),
                },
                default=json_serializable,
            )
            retry_reply = llm_client.generate_text(system=RESPONDER_PROMPT, payload=retry_payload)
            is_grounded_2, violation_2 = verify_grounding(retry_reply, plan_data)
            if is_grounded_2:
                final_reply = retry_reply
                grounding_path = "llm_retry"
            else:
                logger.warning(
                    "Grounding failed on retry (%s), falling back to template", violation_2
                )
                final_reply = render_fallback_template(plan_data)
                grounding_path = "fallback_template"
    except Exception as e:
        logger.warning("Responder generation error (%s), using fallback template", e)
        final_reply = render_fallback_template(plan_data)
        grounding_path = "fallback_template_error"

    elapsed = time.perf_counter() - start_time

    # 6. Build quick replies, notes, debug payload and persist
    if plan_data.get("type") == "show_results":
        quick_replies = ["Show cheaper", "Change budget", "New search"]
        if plan_data.get("has_more"):
            quick_replies.insert(0, "Show more")
    elif plan_data.get("type") == "no_results":
        quick_replies = ["Change budget", "Change location", "New search"]

    notes: list[str] = []
    if cards:
        notes.append("Showing available listings only")
    for a in assumptions:
        clean = a.strip()
        if clean:
            cap = clean[0].upper() + clean[1:]
            if cap not in notes:
                notes.append(cap)
    for uid in plan_data.get("unverifiable_ids", []):
        unv_note = f"{uid} also fits but its price is not listed"
        if unv_note not in notes:
            notes.append(unv_note)

    client_debug = {
        "intent": nlu_res.intent.value if hasattr(nlu_res, "intent") else None,
        "normalised_filters": state.filters.model_dump(mode="json", exclude_none=True),
        "assumptions": assumptions,
        "plan_type": plan_data.get("type"),
        "grounding_path": grounding_path,
        "latency_ms": round(elapsed * 1000),
        "result_ids": [c.property_id for c in cards],
    }

    debug_info = {
        "nlu_result": nlu_res.model_dump(mode="json"),
        "plan_type": plan_data.get("type"),
        "grounding_path": grounding_path,
        "latency_sec": round(elapsed, 3),
    }

    if search_log_entry:
        slog = SearchLog(
            session_id=sess_uuid,
            filters=search_log_entry["filters"],
            sort=search_log_entry["sort"],
            result_ids=search_log_entry["result_ids"],
            total=search_log_entry["total"],
        )
        db.add(slog)

    chips = build_filter_chips(state.filters)
    has_more = plan_data.get("has_more", False)

    return _persist_and_return(
        db=db,
        session_rec=session_rec,
        sess_uuid=sess_uuid,
        customer_message=message,
        reply=final_reply,
        state=state,
        debug=debug_info,
        cards=cards,
        filter_chips=chips,
        quick_replies=quick_replies,
        has_more=has_more,
        notes=notes,
        client_debug=client_debug,
    )


def _persist_and_return(
    db: Session,
    session_rec: SessionModel,
    sess_uuid: uuid.UUID,
    customer_message: str,
    reply: str,
    state: ChatState,
    debug: dict[str, Any] | None = None,
    cards: list[PropertyCard] | None = None,
    filter_chips: list[FilterChip] | None = None,
    quick_replies: list[str] | None = None,
    has_more: bool = False,
    notes: list[str] | None = None,
    client_debug: dict[str, Any] | None = None,
) -> ChatResponsePayload:
    # Save turns
    turn_user = ConversationTurn(
        session_id=sess_uuid,
        role="customer",
        content=customer_message,
    )
    turn_assistant = ConversationTurn(
        session_id=sess_uuid,
        role="assistant",
        content=reply,
        debug=debug,
    )
    db.add(turn_user)
    db.add(turn_assistant)

    session_rec.state = state.model_dump(mode="json")
    db.commit()

    settings = get_settings()
    debug_payload = client_debug if settings.DEBUG else None

    return ChatResponsePayload(
        session_id=str(sess_uuid),
        reply=reply,
        cards=cards or [],
        filter_chips=filter_chips or [],
        quick_replies=quick_replies or [],
        has_more=has_more,
        notes=notes or [],
        debug=debug_payload,
    )


def remove_filter(
    session_id: str,
    slot: str,
    db: Session,
    llm_client: LLMClient,
) -> ChatResponsePayload:
    """Remove a filter slot and re-execute search without an NLU LLM call."""
    try:
        sess_uuid = uuid.UUID(session_id)
    except ValueError:
        raise ValueError("Invalid session_id format") from None

    session_rec = db.query(SessionModel).filter(SessionModel.id == sess_uuid).first()
    if not session_rec:
        raise ValueError(f"Session '{session_id}' not found")

    state = ChatState.model_validate(session_rec.state)

    # Clear specified slot
    if slot == "city":
        state.filters.city = None
        state.filters.sector = None
        state.filters.set_slot_status("city", SlotStatus.UNSPECIFIED)
    elif slot == "sector":
        state.filters.sector = None
        state.filters.set_slot_status("sector", SlotStatus.UNSPECIFIED)
    elif slot == "bhk":
        state.filters.bhk_in = None
        state.filters.bhk_min = None
        state.filters.set_slot_status("bhk", SlotStatus.UNSPECIFIED)
    elif slot == "budget":
        state.filters.price_min_lakh = None
        state.filters.price_max_lakh = None
        state.filters.set_slot_status("budget", SlotStatus.UNSPECIFIED)
    elif slot == "floor":
        state.filters.floor_min = None
        state.filters.floor_max = None
        state.filters.set_slot_status("floor", SlotStatus.UNSPECIFIED)
    elif slot == "area":
        state.filters.area_min = None
        state.filters.area_max = None
        state.filters.set_slot_status("area", SlotStatus.UNSPECIFIED)

    page_size = 5
    sort_to_use = state.last_search.sort if state.last_search else None
    search_res = search_properties(db, state.filters, sort=sort_to_use, limit=page_size, offset=0)

    if search_res.total == 0:
        expl = explain_no_results(db, state.filters)
        plan_data = {
            "type": "no_results",
            "explanation": expl.model_dump(mode="json"),
            "applied_filters": state.filters.model_dump(mode="json", exclude_none=True),
        }
        cards = []
    else:
        cards = format_cards(search_res.rows, start_pos=1)
        state.last_search = LastSearchState(
            shown=[ShownItem(position=c.position, property_id=c.property_id) for c in cards],
            total=search_res.total,
            offset=0,
            sort=search_res.applied_sort,
        )
        plan_data = {
            "type": "show_results",
            "rows": search_res.rows,
            "total": search_res.total,
            "applied_filters": state.filters.model_dump(mode="json", exclude_none=True),
            "assumptions": [f"removed {slot} filter"],
            "unverifiable_ids": search_res.unverifiable_ids,
            "sort": search_res.applied_sort,
            "has_more": (len(search_res.rows) < search_res.total),
        }

    try:
        reply = llm_client.generate_text(
            system=RESPONDER_PROMPT,
            payload=json.dumps(plan_data, default=json_serializable, indent=2),
        )
        is_ok, _ = verify_grounding(reply, plan_data)
        if not is_ok:
            reply = render_fallback_template(plan_data)
    except Exception:
        reply = render_fallback_template(plan_data)

    chips = build_filter_chips(state.filters)
    has_more = plan_data.get("has_more", False)

    quick_replies = ["Show cheaper", "Change budget", "New search"]
    if has_more:
        quick_replies.insert(0, "Show more")

    notes = []
    if cards:
        notes.append("Showing available listings only")
    notes.append(f"Removed {slot} filter")
    for uid in search_res.unverifiable_ids:
        notes.append(f"{uid} also fits but its price is not listed")

    client_debug = {
        "action": "remove_filter",
        "slot": slot,
        "normalised_filters": state.filters.model_dump(mode="json", exclude_none=True),
        "result_ids": [c.property_id for c in cards],
    }

    return _persist_and_return(
        db=db,
        session_rec=session_rec,
        sess_uuid=sess_uuid,
        customer_message=f"Remove filter: {slot}",
        reply=reply,
        state=state,
        debug={"action": "remove_filter", "slot": slot},
        cards=cards,
        filter_chips=chips,
        quick_replies=quick_replies,
        has_more=has_more,
        notes=notes,
        client_debug=client_debug,
    )
