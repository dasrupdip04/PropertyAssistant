import re
from typing import Any


def extract_allowed_property_ids(plan_data: dict[str, Any]) -> set[str]:
    """Collect all valid property IDs present anywhere in the response plan."""
    allowed: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, str):
            for pid in re.findall(r"PROP-\d+", obj):
                allowed.add(pid)

    walk(plan_data)
    return allowed


def extract_allowed_numbers(plan_data: dict[str, Any]) -> set[float]:
    """Collect all numeric figures (BHK, price, floor, area, sectors) anywhere in the plan."""
    allowed: set[float] = set()

    def add_val(v: Any) -> None:
        if v is None:
            return
        try:
            val = float(v)
            allowed.add(val)
            if val >= 50.0:
                allowed.add(round(val / 100.0, 2))
                allowed.add(round(val / 100.0, 1))
        except (ValueError, TypeError):
            pass

    plan_type = plan_data.get("type", "")
    if plan_type in ("ask_followups", "ask_city"):
        applied = plan_data.get("applied_filters", {})
        for v in applied.values():
            if isinstance(v, list):
                for item in v:
                    add_val(item)
            else:
                add_val(v)
        return allowed

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                for num in re.findall(r"\b\d+(?:\.\d+)?\b", str(k)):
                    add_val(num)
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, str):
            for num in re.findall(r"\b\d+(?:\.\d+)?\b", obj):
                add_val(num)
        elif isinstance(obj, (int, float)):
            add_val(obj)

    walk(plan_data)
    return allowed


def verify_grounding(reply: str, plan_data: dict[str, Any]) -> tuple[bool, str | None]:
    """Check that every property ID and key figure in reply exists in the plan."""
    plan_type = plan_data.get("type", "")

    # Reject unrequested budget mentions in ask plans
    if plan_type in ("ask_followups", "ask_city"):
        applied = plan_data.get("applied_filters", {})
        has_budget = bool(applied.get("price_min_lakh") or applied.get("price_max_lakh"))
        if not has_budget:
            reply_lower = reply.lower()
            if "assuming" in reply_lower and any(
                w in reply_lower for w in ("budget", "lakh", "cr", "crore")
            ):
                return (
                    False,
                    "Grounding violation: responder assumed budget without customer input.",
                )
            price_terms = re.findall(
                r"\b(\d+(?:\.\d+)?)\s*(?:lakh|lac|crore|cr|l\b)", reply, re.IGNORECASE
            )
            if price_terms:
                return (
                    False,
                    f"Grounding violation: unrequested price '{price_terms[0]}'.",
                )

    # 1. Check property IDs
    mentioned_ids = re.findall(r"PROP-\d+", reply)
    allowed_ids = extract_allowed_property_ids(plan_data)

    for pid in mentioned_ids:
        if pid not in allowed_ids:
            return False, f"Mentioned property ID '{pid}' is not in the plan."

    # 2. Check for hallucinatory price/floor/area numbers if property IDs are mentioned
    allowed_nums = extract_allowed_numbers(plan_data)
    benign_nums = {1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0}
    allowed_nums.update(benign_nums)

    # Check standalone numbers > 10 in reply
    tokens = re.findall(r"\b(\d+(?:\.\d+)?)\b", reply)
    for tok in tokens:
        try:
            val = float(tok)
            if val > 10.0 and val not in allowed_nums:
                close = any(abs(val - a) < 0.05 for a in allowed_nums)
                if not close:
                    if f"PROP-{int(val):03d}" in allowed_ids or f"PROP-{tok}" in allowed_ids:
                        continue
                    return False, f"Figure '{tok}' in reply does not match any value in the plan."
        except ValueError:
            pass

    return True, None


def render_fallback_template(plan_data: dict[str, Any]) -> str:
    """Generate a deterministic, 100% grounded fallback response from plan data."""
    plan_type = plan_data.get("type", "")

    if plan_type == "ask_followups":
        slots = plan_data.get("slots", [])
        slots_str = " and ".join(slots)
        include_hint = plan_data.get("include_unsure_hint")
        hint = (
            " (Feel free to say 'not sure' if you don't have a preference.)" if include_hint else ""
        )
        return f"To help find the right match, could you tell me your preferred {slots_str}?{hint}"

    elif plan_type == "ask_city":
        options = plan_data.get("options", [])
        options_str = " or ".join(options)
        return f"That sector exists in multiple cities. Are you looking in {options_str}?"

    elif plan_type == "show_results":
        total = plan_data.get("total", 0)
        rows = plan_data.get("rows", [])
        assumptions = plan_data.get("assumptions", [])
        unverifiables = plan_data.get("unverifiable_ids", [])

        parts = []
        if assumptions:
            parts.append(f"Based on your criteria ({', '.join(assumptions)}), ")
        else:
            parts.append("Here are the properties matching your criteria: ")

        id_list = ", ".join(r["property_id"] for r in rows[:3])
        parts.append(f"I found {total} matching listings including {id_list}.")

        if unverifiables:
            unv_str = ", ".join(unverifiables)
            parts.append(f" Note that {unv_str} might also match, but has missing listed details.")

        return "".join(parts)

    elif plan_type == "no_results":
        expl = plan_data.get("explanation", {})
        other_cities = expl.get("sector_in_other_cities", {})
        unavail = expl.get("unavailable_units", {})

        if other_cities:
            for sec, cities in other_cities.items():
                c_str = ", ".join(cities)
                return f"No matching properties found. Sector {sec} is located in {c_str}."

        if unavail:
            res_ids = unavail.get("reserved", [])
            sold_ids = unavail.get("sold", [])
            parts = ["No available units match right now."]
            if res_ids:
                parts.append(f"Units currently reserved: {', '.join(res_ids)}.")
            if sold_ids:
                parts.append(f"Units already sold: {', '.join(sold_ids)}.")
            return " ".join(parts)

        return (
            "I could not find any properties matching those exact filters. "
            "Would you like to adjust your budget or location?"
        )

    elif plan_type == "property_answer":
        pid = plan_data.get("property_id")
        proj = plan_data.get("project_name", "")
        status = plan_data.get("status", "")
        attrs = plan_data.get("attributes", {})

        details = []
        for k, v in attrs.items():
            val_str = "not listed" if v is None else str(v)
            details.append(f"{k}: {val_str}")
        details_str = ", ".join(details) if details else f"status is {status}"

        warning = ""
        if status in ("sold", "reserved"):
            warning = f" Please note that {pid} is currently {status}."

        return f"For {pid} ({proj}), {details_str}.{warning}"

    elif plan_type == "compare":
        props = plan_data.get("properties", [])
        p_summaries = [
            f"{p.get('property_id')} ({p.get('configuration_bhk')} BHK, {p.get('price_lakh')} Lakh)"
            for p in props
        ]
        return f"Comparing listings: {'; '.join(p_summaries)}."

    elif plan_type == "clarify_reference":
        return plan_data.get(
            "message", "Could you please clarify which property you are referring to?"
        )

    elif plan_type == "issue":
        issues = plan_data.get("issues", [])
        if issues:
            return issues[0].get("message", "There was an issue understanding that preference.")
        return "I encountered an issue with that selection. Could you please specify again?"

    elif plan_type == "smalltalk":
        return "Hello! I am your PropertyAssistant. How can I assist with your home search today?"

    elif plan_type == "out_of_scope":
        return "I can only assist with verified property listings in our inventory."

    elif plan_type == "nlu_failed":
        return (
            "I am sorry, I did not quite catch that. "
            "Could you please rephrase what you are looking for?"
        )

    return "Here are the details from our database."
