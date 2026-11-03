import argparse
import json
from typing import Any

from app.db import SessionLocal
from app.domain.catalog import get_catalog
from app.domain.filters import SearchFilters
from app.domain.normalizers import (
    normalize_sector,
    parse_bhk,
    parse_budget_string,
    resolve_city,
    resolve_project,
)
from app.domain.policy import DialogueState, decide_next_action
from app.domain.relax import explain_no_results
from app.domain.search import search_properties


def main() -> None:
    parser = argparse.ArgumentParser(description="PropertyAssistant Search Engine CLI Demo")
    parser.add_argument(
        "--city", type=str, default=None, help="City name or alias (e.g. ggn, noida)"
    )
    parser.add_argument(
        "--sector", type=str, default=None, help="Sector identifier (e.g. 'Sec 150', '16B')"
    )
    parser.add_argument(
        "--project", type=str, default=None, help="Project name (e.g. 'Maple Heights')"
    )
    parser.add_argument("--bhk", type=str, default=None, help="BHK value (e.g. 3, '2,3')")
    parser.add_argument(
        "--budget", type=str, default=None, help="Budget string (e.g. '1 cr', '90 lakh')"
    )
    parser.add_argument(
        "--qualifier", type=str, default=None, help="Budget qualifier ('around', 'under', 'min')"
    )
    parser.add_argument(
        "--sort", type=str, default=None, help="Sort override ('price_asc', 'newest', 'area_desc')"
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of results to display"
    )
    parser.add_argument(
        "--show-relax", action="store_true", help="Show no-match relaxation if 0 results"
    )
    parser.add_argument(
        "--show-policy", action="store_true", help="Show dialogue policy next action"
    )

    args = parser.parse_args()

    assumptions: list[str] = []
    filters = SearchFilters()

    with SessionLocal() as session:
        catalog = get_catalog(session)

        # 1. Resolve city
        if args.city:
            res_city = resolve_city(args.city, catalog)
            if res_city.value:
                filters.city = res_city.value
                assumptions.append(
                    f"resolved city '{args.city}' -> '{res_city.value}' ({res_city.status.value})"
                )
            else:
                assumptions.append(f"unknown city '{args.city}'")

        # 2. Normalize sector
        if args.sector:
            norm_sec = normalize_sector(args.sector)
            if norm_sec:
                filters.sector = norm_sec
                assumptions.append(f"normalized sector '{args.sector}' -> '{norm_sec}'")
            else:
                assumptions.append(f"could not parse sector '{args.sector}'")

        # 3. Resolve project
        if args.project:
            res_proj = resolve_project(args.project, catalog)
            if res_proj.value:
                filters.project = res_proj.value
                assumptions.append(
                    f"resolved project '{args.project}' -> '{res_proj.value}' "
                    f"({res_proj.status.value})"
                )
            else:
                assumptions.append(f"unknown project '{args.project}'")

        # 4. Parse BHK
        if args.bhk:
            res_bhk = parse_bhk(args.bhk, catalog)
            if res_bhk.bhk_values:
                filters.bhk_in = res_bhk.bhk_values
            assumptions.extend(res_bhk.assumptions)

        # 5. Parse budget
        if args.budget:
            res_budget = parse_budget_string(args.budget, qualifier=args.qualifier)
            filters.price_min_lakh = res_budget.low_lakh
            filters.price_max_lakh = res_budget.high_lakh
            assumptions.extend(res_budget.assumptions)

        # Execute search
        search_res = search_properties(
            session=session,
            filters=filters,
            sort=args.sort,
            limit=args.limit,
        )

        output: dict[str, Any] = {
            "filters": filters.model_dump(mode="json", exclude_none=True),
            "assumptions": assumptions,
            "total_matches": search_res.total,
            "applied_sort": search_res.applied_sort,
            "results": [
                {
                    "rank": idx + 1,
                    "property_id": r["property_id"],
                    "project_name": r["project_name"],
                    "city": r["city"],
                    "sector": r["sector"],
                    "bhk": r["configuration_bhk"],
                    "price_lakh": float(r["price_lakh"]) if r["price_lakh"] is not None else None,
                    "floor": r["floor"],
                    "area_sqft": r["area_sqft"],
                    "updated_at": str(r["listing_updated_at"]),
                }
                for idx, r in enumerate(search_res.rows)
            ],
            "unverifiable_ids": search_res.unverifiable_ids,
            "unverifiable_count": len(search_res.unverifiable_ids),
        }

        if search_res.total == 0 and args.show_relax:
            output["explanation"] = explain_no_results(session, filters).model_dump(mode="json")

        if args.show_policy:
            policy_state = DialogueState(filters=filters)
            output["policy_action"] = decide_next_action(policy_state, session).model_dump(
                mode="json"
            )

        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
