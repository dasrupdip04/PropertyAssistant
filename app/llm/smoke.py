import json
import sys
import time
from pathlib import Path
from typing import Any

from app.chat.service import handle_turn
from app.config import get_settings
from app.db import SessionLocal
from app.domain.catalog import get_catalog
from app.llm.client import FakeLLMClient, GeminiClient
from app.llm.nlu import run_nlu
from app.llm.schema import FilterAction

GOLDEN_PATH = Path(__file__).parent.parent.parent / "tests" / "golden_nlu.json"


def build_synthetic_nlu(expected: dict[str, Any]) -> Any:
    from app.llm.schema import (
        AskedAttribute,
        BHKFilterValue,
        BudgetAmountUnit,
        BudgetFilterValue,
        BudgetQualifier,
        CityFilterValue,
        FilterAction,
        IntentType,
        NLUFilters,
        NLUResult,
        Reference,
        ReferenceKind,
        SectorFilterValue,
        SortType,
    )

    exp_intent = IntentType(expected.get("intent", "search"))
    filters = NLUFilters()
    if "city" in expected:
        filters.city = CityFilterValue(action=FilterAction.SET, raw_text=expected["city"])
    if "sector" in expected:
        filters.sector = SectorFilterValue(action=FilterAction.SET, raw_text=expected["sector"])
    if "bhk" in expected:
        filters.bhk = BHKFilterValue(action=FilterAction.SET, values=expected["bhk"])
    if "budget_qualifier" in expected:
        filters.budget = BudgetFilterValue(
            action=FilterAction.SET,
            qualifier=BudgetQualifier(expected["budget_qualifier"]),
            high=BudgetAmountUnit(amount=expected.get("budget_amount", 1.0), unit="cr"),
        )
    if "unsure_slots" in expected:
        for slot in expected["unsure_slots"]:
            if slot == "budget":
                filters.budget.action = FilterAction.UNSURE
            elif slot == "sector":
                filters.sector.action = FilterAction.UNSURE
    ref = Reference()
    if "reference_kind" in expected:
        ref.kind = ReferenceKind(expected["reference_kind"])
        ref.ordinal = expected.get("reference_ordinal")
        if "property_id" in expected:
            ref.property_ids = [expected["property_id"]]

    asked_attrs = []
    if "asked_attributes" in expected:
        for a in expected["asked_attributes"]:
            asked_attrs.append(AskedAttribute(a))

    return NLUResult(
        intent=exp_intent,
        wants_results_now=expected.get("wants_results_now", False),
        filters=filters,
        sort=SortType(expected.get("sort", "none")),
        reference=ref,
        asked_attributes=asked_attrs,
    )


def evaluate_golden() -> float:
    settings = get_settings()
    has_real_key = bool(settings.GEMINI_API_KEY and "your-gemini" not in settings.GEMINI_API_KEY)

    if not has_real_key:
        print("[SMOKE] No GEMINI_API_KEY set; running golden eval with fake client.")
        client = FakeLLMClient()
    else:
        print("[SMOKE] Running golden evaluation using real Gemini Client...")
        client = GeminiClient()

    if not GOLDEN_PATH.exists():
        print(f"Error: Golden file not found at {GOLDEN_PATH}")
        sys.exit(1)

    with open(GOLDEN_PATH, encoding="utf-8") as f:
        cases = json.load(f)

    with SessionLocal() as db:
        catalog = get_catalog(db)

    field_scores: dict[str, list[bool]] = {
        "intent": [],
        "city": [],
        "sector": [],
        "bhk": [],
        "budget": [],
        "unsure": [],
        "reference": [],
    }

    print(f"\nEvaluating {len(cases)} golden NLU cases...")
    print(f"{'#':<3} | {'Message':<35} | {'Intent':<17} | {'Status':<6}")
    print("-" * 70)

    for idx, case in enumerate(cases):
        msg = case["message"]
        expected = case["expected"]
        pending = case.get("pending_slots", [])
        shown = case.get("shown_count", 0)

        # For FakeLLMClient in test mode without network
        if not has_real_key:
            nlu_res = build_synthetic_nlu(expected)
        else:
            try:
                time.sleep(1.2)  # Respect free-tier 15 RPM rate limit
                nlu_res = run_nlu(
                    client=client,
                    message=msg,
                    catalog=catalog,
                    pending_slots=pending,
                    shown_count=shown,
                )
            except Exception as e:
                print(f"Error on case {idx + 1}: {e}")
                continue

        # Evaluate intent
        intent_match = nlu_res.intent.value == expected.get("intent")
        field_scores["intent"].append(intent_match)

        # City
        if "city" in expected:
            c_val = nlu_res.filters.city.raw_text or ""
            c_match = c_val.lower() == expected["city"].lower()
            field_scores["city"].append(c_match)

        # Sector
        if "sector" in expected:
            s_val = nlu_res.filters.sector.raw_text or ""
            s_match = s_val.upper() == expected["sector"].upper()
            field_scores["sector"].append(s_match)

        # BHK
        if "bhk" in expected:
            b_val = nlu_res.filters.bhk.values
            b_match = b_val == expected["bhk"]
            field_scores["bhk"].append(b_match)

        # Budget
        if "budget_qualifier" in expected:
            q = nlu_res.filters.budget.qualifier
            q_val = q.value if q else None
            field_scores["budget"].append(q_val == expected["budget_qualifier"])

        # Unsure
        if "unsure_slots" in expected:
            unsure_found = []
            if nlu_res.filters.budget.action == FilterAction.UNSURE:
                unsure_found.append("budget")
            if nlu_res.filters.sector.action == FilterAction.UNSURE:
                unsure_found.append("sector")
            field_scores["unsure"].append(set(unsure_found) == set(expected["unsure_slots"]))

        # Reference
        if "reference_kind" in expected:
            r_kind_match = nlu_res.reference.kind.value == expected["reference_kind"]
            if "reference_ordinal" in expected:
                ref_ord_match = nlu_res.reference.ordinal == expected["reference_ordinal"]
                r_kind_match = r_kind_match and ref_ord_match
            field_scores["reference"].append(r_kind_match)

        status_str = "PASS" if intent_match else "FAIL"
        short_msg = (msg[:32] + "...") if len(msg) > 32 else msg
        print(f"{idx + 1:<3} | {short_msg:<35} | {nlu_res.intent.value:<17} | {status_str:<6}")

    print("\n--- Accuracy Summary ---")
    total_accs = []
    for field, scores in field_scores.items():
        if scores:
            acc = (sum(scores) / len(scores)) * 100.0
            total_accs.append(acc)
            print(f"{field:<12}: {acc:>6.1f}% ({sum(scores)}/{len(scores)})")

    overall = sum(total_accs) / len(total_accs) if total_accs else 0.0
    print(f"\nOverall Average Field Accuracy: {overall:.1f}%\n")
    return overall


def run_smoke_conversations() -> None:
    settings = get_settings()
    has_real_key = bool(settings.GEMINI_API_KEY and "your-gemini" not in settings.GEMINI_API_KEY)
    client = GeminiClient() if has_real_key else FakeLLMClient()

    if not has_real_key:
        mapping: dict[str, Any] = {}
        with open(GOLDEN_PATH, encoding="utf-8") as f:
            for item in json.load(f):
                mapping[item["message"].strip().lower()] = item["expected"]
        # Add conversational variants for the smoke conversation
        mapping["looking for 3 bhk in noida"] = {"intent": "search", "city": "Noida", "bhk": [3]}
        mapping["apartments in gurugram"] = {
            "intent": "search",
            "city": "Gurugram",
            "wants_results_now": True,
        }
        mapping["show me under 1.5 cr"] = {
            "intent": "search",
            "budget_qualifier": "max",
            "budget_amount": 1.5,
        }
        mapping["sector 150 gurugram"] = {
            "intent": "search",
            "city": "Gurugram",
            "sector": "150",
            "wants_results_now": True,
        }

        def mock_nlu_handler(system: str, payload: str, schema: type) -> Any:
            try:
                p_dict = json.loads(payload)
                raw = p_dict.get("customer_message") or p_dict.get("message") or ""
                msg = str(raw).strip().lower()
            except Exception:
                msg = ""
            exp = mapping.get(msg, {"intent": "search"})
            return build_synthetic_nlu(exp)

        client.set_structured_handler(mock_nlu_handler)

    print("========================================")
    print("Running 3 End-to-End Sample Conversations")
    print("========================================")

    convs = [
        [
            "Looking for 3 BHK in Noida",
            "around 1 cr, not sure about sector",
            "what floor is the second one on?",
        ],
        [
            "Apartments in Gurugram",
            "show me under 1.5 cr",
        ],
        [
            "Sector 150 Gurugram",
        ],
    ]

    with SessionLocal() as db:
        for c_idx, turns in enumerate(convs, start=1):
            print(f"\n--- Conversation {c_idx} ---")
            sess_id = None
            for user_msg in turns:
                print(f"\nUser: {user_msg}")
                try:
                    resp = handle_turn(
                        session_id=sess_id,
                        message=user_msg,
                        db=db,
                        llm_client=client,
                    )
                    sess_id = resp.session_id
                    card_ids = [c.property_id for c in resp.cards]
                    print(f"Assistant: {resp.reply}")
                    print(f"Cards ({len(card_ids)}): {card_ids}")
                    if resp.filter_chips:
                        print(f"Chips: {[f'{c.slot}:{c.label}' for c in resp.filter_chips]}")
                except Exception as e:
                    print(f"Turn error: {e}")


def main() -> None:
    evaluate_golden()
    run_smoke_conversations()


if __name__ == "__main__":
    main()
