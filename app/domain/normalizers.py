import re
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from app.config import get_settings
from app.domain.aliases import CITY_ALIASES
from app.domain.catalog import Catalog


class ResolutionStatus(StrEnum):
    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ResolutionResult(BaseModel):
    status: ResolutionStatus
    value: str | None = None
    candidates: list[str] = Field(default_factory=list)


class PriceRange(BaseModel):
    low_lakh: Decimal | None = None
    high_lakh: Decimal | None = None
    assumptions: list[str] = Field(default_factory=list)


class BHKResult(BaseModel):
    bhk_values: list[int] = Field(default_factory=list)
    bhk_min: int | None = None
    invalid_values: list[Any] = Field(default_factory=list)
    missing_from_catalog: list[int] = Field(default_factory=list)
    available_catalog_bhks: list[int] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def clean_text(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(cleaned.split())


def resolve_city(text: str, catalog: Catalog) -> ResolutionResult:
    """Resolve city string against catalog and aliases."""
    if not text or not text.strip():
        return ResolutionResult(status=ResolutionStatus.UNKNOWN, value=None)

    cleaned = clean_text(text)
    if not cleaned:
        return ResolutionResult(status=ResolutionStatus.UNKNOWN, value=None)

    # 1. Exact match on catalog cities (case-insensitive)
    for c in catalog.cities:
        if cleaned == c.lower():
            return ResolutionResult(
                status=ResolutionStatus.EXACT,
                value=c,
                candidates=[c],
            )

    # 2. Match against alias table
    # "An alias only counts if its canonical value exists in the catalog"
    if cleaned in CITY_ALIASES:
        canonical = CITY_ALIASES[cleaned]
        if canonical in catalog.cities:
            # Special check: never let 'noida' match 'Greater Noida West'
            if cleaned == "noida" and canonical != "Noida":
                pass
            else:
                return ResolutionResult(
                    status=ResolutionStatus.ALIAS,
                    value=canonical,
                    candidates=[canonical],
                )

    # 3. Rapidfuzz fuzzy matching (score >= 88, only if one clear best)
    # Check both canonical cities and aliases, but map to canonical
    best_candidate: str | None = None
    best_score = 0.0
    second_best_score = 0.0

    # Build targets: catalog cities and active aliases
    targets: dict[str, str] = {c.lower(): c for c in catalog.cities}
    for alias, canonical in CITY_ALIASES.items():
        if canonical in catalog.cities:
            targets[alias] = canonical

    for target_key, canonical_city in targets.items():
        # Prevent 'noida' from fuzzy matching 'greater noida west'
        if cleaned == "noida" and canonical_city == "Greater Noida West":
            continue
        if cleaned == "greater noida west" and canonical_city == "Noida":
            continue

        ratio_score = fuzz.ratio(cleaned, target_key)
        jw_score = JaroWinkler.similarity(cleaned, target_key) * 100
        score = max(ratio_score, jw_score)

        if score > best_score:
            second_best_score = best_score
            best_score = score
            best_candidate = canonical_city
        elif score == best_score:
            if best_candidate != canonical_city:
                second_best_score = score
        elif score > second_best_score:
            second_best_score = score

    if best_score >= 88.0 and best_candidate is not None:
        if best_score - second_best_score >= 2.0 or best_score >= 95.0:
            return ResolutionResult(
                status=ResolutionStatus.FUZZY,
                value=best_candidate,
                candidates=[best_candidate],
            )
        return ResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            value=None,
            candidates=list({best_candidate}),
        )

    return ResolutionResult(status=ResolutionStatus.UNKNOWN, value=None)


SECTOR_REGEX = re.compile(
    r"^(?:sec(?:tor)?|sctr)?[\s\-_]*([0-9]+[a-zA-Z]?)$",
    re.IGNORECASE,
)


def normalize_sector(text: str) -> str | None:
    """Normalize sector string to exact uppercase representation e.g. '150', '16B'."""
    if not text or not text.strip():
        return None
    match = SECTOR_REGEX.match(text.strip())
    if not match:
        return None
    return match.group(1).upper()


def resolve_project(text: str, catalog: Catalog) -> ResolutionResult:
    """Fuzzy match project name against catalog (score >= 85)."""
    if not text or not text.strip() or not catalog.project_names:
        return ResolutionResult(status=ResolutionStatus.UNKNOWN, value=None)

    cleaned = text.strip()
    # Check exact match first (case-insensitive)
    for name in catalog.project_names:
        if cleaned.lower() == name.lower():
            return ResolutionResult(
                status=ResolutionStatus.EXACT,
                value=name,
                candidates=[name],
            )

    best_name: str | None = None
    best_score = 0.0
    for name in catalog.project_names:
        score = max(
            fuzz.ratio(cleaned.lower(), name.lower()),
            fuzz.WRatio(cleaned, name),
        )
        if score > best_score:
            best_score = score
            best_name = name

    if best_score >= 85.0 and best_name is not None:
        return ResolutionResult(
            status=ResolutionStatus.FUZZY,
            value=best_name,
            candidates=[best_name],
        )

    return ResolutionResult(status=ResolutionStatus.UNKNOWN, value=None)



def parse_budget(
    amount: float | int | Decimal | str,
    unit: str | None = None,
    qualifier: str | None = None,
    amount2: float | int | Decimal | str | None = None,
) -> PriceRange:
    """Parse budget amount, unit, and qualifier into PriceRange with plain-English assumptions."""
    settings = get_settings()
    assumptions: list[str] = []

    def to_decimal(v: float | int | Decimal | str) -> Decimal:
        return Decimal(str(v).strip())

    val1 = to_decimal(amount)
    unit_str = (unit or "").lower().strip()

    # Determine unit and convert val1 to lakhs
    if not unit_str:
        if val1 >= 20:
            lakhs1 = val1
            assumptions.append(f"treated bare {val1} as {val1} lakh")
        else:
            lakhs1 = val1 * Decimal(100)
            assumptions.append(f"treated bare {val1} as {val1} crore ({lakhs1} lakh)")
    elif unit_str in ("cr", "crore", "crores"):
        lakhs1 = val1 * Decimal(100)
        assumptions.append(f"converted {val1} {unit_str} to {lakhs1} lakh")
    elif unit_str in ("lakh", "lac", "l", "lakhs", "lacs"):
        lakhs1 = val1
    else:
        lakhs1 = val1
        assumptions.append(f"unrecognized unit '{unit_str}', treated as {val1} lakh")

    q = (qualifier or "").lower().strip()

    # Qualifiers handling
    if q in ("under", "upto", "up to", "within", "budget", "max", "less than"):
        return PriceRange(
            low_lakh=None,
            high_lakh=lakhs1,
            assumptions=assumptions,
        )
    elif q in ("above", "at least", "min", "more than"):
        return PriceRange(
            low_lakh=lakhs1,
            high_lakh=None,
            assumptions=assumptions,
        )
    elif q == "around":
        p = Decimal(str(settings.PRICE_AROUND_PCT))
        low = round(lakhs1 * (Decimal(1) - p), 2)
        high = round(lakhs1 * (Decimal(1) + p), 2)
        unit_display = f"{amount} {unit}" if unit else f"{amount}"
        assumptions.append(f"treated around {unit_display} as {low} to {high} lakh")
        return PriceRange(
            low_lakh=low,
            high_lakh=high,
            assumptions=assumptions,
        )
    elif q in ("between", "range") and amount2 is not None:
        val2 = to_decimal(amount2)
        if not unit_str:
            lakhs2 = val2 if val2 >= 20 else val2 * Decimal(100)
        elif unit_str in ("cr", "crore", "crores"):
            lakhs2 = val2 * Decimal(100)
        else:
            lakhs2 = val2
        low = min(lakhs1, lakhs2)
        high = max(lakhs1, lakhs2)
        assumptions.append(f"treated between {val1} and {val2} as {low} to {high} lakh")
        return PriceRange(
            low_lakh=low,
            high_lakh=high,
            assumptions=assumptions,
        )
    elif q in ("exact", "exactly"):
        assumptions.append(f"treated exact {val1} as exactly {lakhs1} lakh")
        return PriceRange(
            low_lakh=lakhs1,
            high_lakh=lakhs1,
            assumptions=assumptions,
        )
    else:
        # Default with no qualifier: upper bound
        return PriceRange(
            low_lakh=None,
            high_lakh=lakhs1,
            assumptions=assumptions,
        )


def parse_budget_string(
    text: str,
    qualifier: str | None = None,
) -> PriceRange:
    """Parse budget from free-form string like 'under 1 cr', 'between 90 and 110', 'around 1 cr'."""
    s = text.strip().lower()

    # Detect qualifier prefix if not explicitly passed
    detected_qualifier = qualifier
    if not detected_qualifier:
        if s.startswith("under "):
            detected_qualifier = "under"
            s = s[6:].strip()
        elif s.startswith("upto "):
            detected_qualifier = "upto"
            s = s[5:].strip()
        elif s.startswith("up to "):
            detected_qualifier = "upto"
            s = s[6:].strip()
        elif s.startswith("within "):
            detected_qualifier = "within"
            s = s[7:].strip()
        elif s.startswith("around "):
            detected_qualifier = "around"
            s = s[7:].strip()
        elif s.startswith("above "):
            detected_qualifier = "above"
            s = s[6:].strip()
        elif s.startswith("at least "):
            detected_qualifier = "at least"
            s = s[9:].strip()
        elif s.startswith("between "):
            detected_qualifier = "between"
            s = s[8:].strip()

    # Between pattern
    between_match = re.match(
        r"^([0-9]+(?:\.[0-9]+)?)\s*(?:and|to|-)\s*([0-9]+(?:\.[0-9]+)?)\s*(cr|crore|lakh|lac|l)?$",
        s,
        re.IGNORECASE,
    )
    if between_match:
        val1 = between_match.group(1)
        val2 = between_match.group(2)
        unit = between_match.group(3)
        return parse_budget(
            amount=val1,
            unit=unit,
            qualifier="between",
            amount2=val2,
        )

    # Single amount with optional unit
    single_match = re.match(
        r"^([0-9]+(?:\.[0-9]+)?)\s*(cr|crore|crores|lakh|lakhs|lac|lacs|l)?$",
        s,
        re.IGNORECASE,
    )
    if single_match:
        val = single_match.group(1)
        unit = single_match.group(2)
        return parse_budget(
            amount=val,
            unit=unit,
            qualifier=detected_qualifier,
        )

    # Fallback to bare number extraction
    nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", s)
    if nums:
        return parse_budget(
            amount=nums[0],
            unit=None,
            qualifier=detected_qualifier,
        )

    return PriceRange(assumptions=["could not parse budget string"])


def parse_bhk(
    value: int | str | list[int] | list[str],
    catalog: Catalog | None = None,
) -> BHKResult:
    """Validate and extract BHK integers (1-10) and report catalog presence."""
    items: list[Any] = value if isinstance(value, list) else [value]
    bhk_values: list[int] = []
    invalid_values: list[Any] = []
    assumptions: list[str] = []

    for item in items:
        if isinstance(item, int):
            int_val = item
        else:
            match = re.search(r"([0-9]+)", str(item))
            if match:
                int_val = int(match.group(1))
            else:
                invalid_values.append(item)
                continue

        if 1 <= int_val <= 10:
            if int_val not in bhk_values:
                bhk_values.append(int_val)
        else:
            invalid_values.append(item)

    missing_from_catalog: list[int] = []
    available_catalog_bhks: list[int] = []

    if catalog:
        available_catalog_bhks = sorted(catalog.bhk_values)
        for b in bhk_values:
            if b not in catalog.bhk_values:
                missing_from_catalog.append(b)

        if missing_from_catalog:
            missing_str = ", ".join(f"{b} BHK" for b in missing_from_catalog)
            avail_str = ", ".join(f"{b} BHK" for b in available_catalog_bhks)
            assumptions.append(
                f"Catalog has no listings for {missing_str}; available BHKs are {avail_str}"
            )

    return BHKResult(
        bhk_values=bhk_values,
        invalid_values=invalid_values,
        missing_from_catalog=missing_from_catalog,
        available_catalog_bhks=available_catalog_bhks,
        assumptions=assumptions,
    )


def get_floor_band(floor: int) -> str:
    s = get_settings()
    if floor <= s.FLOOR_BAND_LOW_MAX:
        return "low"
    elif floor <= s.FLOOR_BAND_MID_MAX:
        return "mid"
    return "high"


def get_floor_band_range(band: str) -> tuple[int | None, int | None]:
    s = get_settings()
    b = band.lower().strip()
    if b == "low":
        return 1, s.FLOOR_BAND_LOW_MAX
    elif b == "mid":
        return s.FLOOR_BAND_MID_MIN, s.FLOOR_BAND_MID_MAX
    elif b == "high":
        return s.FLOOR_BAND_HIGH_MIN, None
    return None, None


def compute_area_bands(areas: list[int]) -> list[dict[str, Any]]:
    """Compute area quartiles from candidate areas."""
    sorted_areas = sorted([a for a in areas if a is not None])
    if not sorted_areas:
        return []
    n = len(sorted_areas)
    if n < 4:
        return [
            {
                "min": sorted_areas[0],
                "max": sorted_areas[-1],
                "label": f"{sorted_areas[0]}-{sorted_areas[-1]} sqft",
            }
        ]

    q1 = sorted_areas[n // 4]
    q2 = sorted_areas[n // 2]
    q3 = sorted_areas[(3 * n) // 4]
    min_a = sorted_areas[0]
    max_a = sorted_areas[-1]

    return [
        {"min": min_a, "max": q1, "label": f"{min_a}-{q1} sqft"},
        {"min": q1 + 1, "max": q2, "label": f"{q1 + 1}-{q2} sqft"},
        {"min": q2 + 1, "max": q3, "label": f"{q2 + 1}-{q3} sqft"},
        {"min": q3 + 1, "max": max_a, "label": f"{q3 + 1}-{max_a} sqft"},
    ]
