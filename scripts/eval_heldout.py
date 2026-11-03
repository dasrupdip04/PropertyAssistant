import asyncio
import json
import time
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal
from app.domain.catalog import get_catalog
from app.llm.client import GeminiClient
from app.llm.nlu import run_nlu


def main():
    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        print("GEMINI_API_KEY not set!")
        return

    client = GeminiClient()
    with SessionLocal() as db:
        catalog = get_catalog(db)

    heldout_path = Path("tests/golden_nlu_heldout.json")
    with open(heldout_path) as f:
        cases = json.load(f)

    results = []
    print(f"Running evaluation on {len(cases)} held-out cases...")

    correct_counts = {
        "intent": 0,
        "city": 0,
        "bhk": 0,
        "budget": 0,
        "sector": 0,
        "reference": 0,
    }

    for idx, case in enumerate(cases):
        msg = case["message"]
        expected = case["expected"]
        print(f"[{idx+1}/{len(cases)}] Evaluating: '{msg}'...")
        try:
            nlu = run_nlu(client, msg, catalog)
        except Exception as e:
            print(f"Error on case {idx+1}: {e}")
            time.sleep(4)
            continue

        # Intent
        intent_match = nlu.intent.value == expected["intent"]
        if intent_match:
            correct_counts["intent"] += 1

        # City
        got_city = nlu.filters.city.raw_text if nlu.filters.city else None
        if expected["city"] is None:
            city_match = got_city is None or nlu.filters.city.action.value == "keep"
        else:
            city_match = got_city is not None and expected["city"].lower() in got_city.lower()
        if city_match:
            correct_counts["city"] += 1

        # BHK
        got_bhk = nlu.filters.bhk.values if nlu.filters.bhk else []
        if expected["bhk"] is None:
            bhk_match = len(got_bhk) == 0
        else:
            bhk_match = set(got_bhk) == set(expected["bhk"])
        if bhk_match:
            correct_counts["bhk"] += 1

        # Budget
        got_budget_high = (
            nlu.filters.budget.high.amount
            if (nlu.filters.budget and nlu.filters.budget.high)
            else None
        )
        got_budget_low = (
            nlu.filters.budget.low.amount
            if (nlu.filters.budget and nlu.filters.budget.low)
            else None
        )
        if expected["budget"] is None:
            budget_match = got_budget_high is None and got_budget_low is None
        else:
            budget_match = (
                got_budget_high == expected["budget"]
                or got_budget_low == expected["budget"]
                or (nlu.filters.budget and nlu.filters.budget.raw_text is not None)
            )
        if budget_match:
            correct_counts["budget"] += 1

        # Sector
        got_sector = nlu.filters.sector.raw_text if nlu.filters.sector else None
        if expected["sector"] is None:
            sector_match = got_sector is None or nlu.filters.sector.action.value == "keep"
        else:
            sector_match = (
                got_sector is not None and expected["sector"].lower() in got_sector.lower()
            )
        if sector_match:
            correct_counts["sector"] += 1

        # Reference
        got_ref = None
        if nlu.reference:
            if nlu.reference.ordinal:
                got_ref = str(nlu.reference.ordinal)
            elif nlu.reference.property_ids:
                got_ref = nlu.reference.property_ids[0]
            elif nlu.intent.value == "compare":
                got_ref = "compare"
        if expected["reference"] is None:
            ref_match = got_ref is None
        else:
            ref_match = got_ref == expected["reference"] or (
                got_ref is not None and expected["reference"] in got_ref
            )
        if ref_match:
            correct_counts["reference"] += 1

        results.append(
            {
                "id": case["id"],
                "message": msg,
                "category": case["category"],
                "intent_match": intent_match,
                "city_match": city_match,
                "bhk_match": bhk_match,
                "budget_match": budget_match,
                "sector_match": sector_match,
                "reference_match": ref_match,
                "got_intent": nlu.intent.value,
                "expected_intent": expected["intent"],
            }
        )

        time.sleep(3.5)  # Pace under 15 RPM

    total = len(cases)
    print("\n--- EVALUATION SUMMARY ---")
    for k, v in correct_counts.items():
        pct = (v / total) * 100
        print(f"{k}: {v}/{total} ({pct:.1f}%)")

    # Generate markdown table
    md_content = "# Step 4 NLU Held-out Evaluation Report\n\n"
    md_content += f"Evaluated on {total} held-out test cases not present in few-shots.\n\n"
    md_content += "## Summary Accuracy\n\n"
    md_content += "| Field | Accuracy | Correct / Total |\n"
    md_content += "|---|---|---|\n"
    for k, v in correct_counts.items():
        pct = (v / total) * 100
        md_content += f"| **{k.capitalize()}** | {pct:.1f}% | {v}/{total} |\n"

    md_content += "\n## Detailed Results\n\n"
    md_content += "| ID | Category | Message | Intent Match | City | BHK | Budget | Sector | Reference |\n"
    md_content += "|---|---|---|---|---|---|---|---|---|\n"
    for r in results:
        md_content += (
            f"| {r['id']} | {r['category']} | `{r['message']}` | "
            f"{'✅' if r['intent_match'] else '❌ (' + r['got_intent'] + ')'} | "
            f"{'✅' if r['city_match'] else '❌'} | "
            f"{'✅' if r['bhk_match'] else '❌'} | "
            f"{'✅' if r['budget_match'] else '❌'} | "
            f"{'✅' if r['sector_match'] else '❌'} | "
            f"{'✅' if r['reference_match'] else '❌'} |\n"
        )

    out_file = Path("docs/EVALUATION_STEP4.md")
    out_file.write_text(md_content)
    print(f"\nSaved report to {out_file}")


if __name__ == "__main__":
    main()
