from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.domain.filters import SearchFilters
from app.models import Property


class SearchResult(BaseModel):
    rows: list[dict[str, Any]]
    total: int
    applied_sort: str
    unverifiable_ids: list[str] = Field(default_factory=list)


def build_base_conditions(filters: SearchFilters) -> list[Any]:
    """Build conditions that do not include nullable range checks (price, floor, area)."""
    conds = [Property.is_active.is_(True)]

    # Sold is NEVER returned from search
    conds.append(Property.availability != "sold")

    # Availability filter (default: {'available'})
    requested_avail = {a.lower() for a in filters.availability}
    # Exclude 'sold' even if requested
    allowed_avail = requested_avail - {"sold"}
    if not allowed_avail:
        allowed_avail = {"available"}

    if "reserved" not in allowed_avail:
        conds.append(Property.availability == "available")
    else:
        conds.append(Property.availability.in_(list(allowed_avail)))

    # Equality for city and sector
    if filters.city:
        conds.append(Property.city == filters.city)
    if filters.sector:
        conds.append(Property.sector == filters.sector.upper())
    if filters.excluded_sectors:
        conds.append(Property.sector.notin_([s.upper() for s in filters.excluded_sectors]))

    # Project
    if filters.project:
        conds.append(Property.project_name == filters.project)

    # BHK
    if filters.bhk_in:
        conds.append(Property.configuration_bhk.in_(filters.bhk_in))
    elif filters.bhk_min is not None:
        conds.append(Property.configuration_bhk >= filters.bhk_min)

    return conds


def build_range_conditions(filters: SearchFilters) -> list[Any]:
    """Conditions for price, floor, area ranges (must be non-null to match)."""
    conds = []
    if filters.price_min_lakh is not None:
        conds.append(Property.price_lakh >= filters.price_min_lakh)
    if filters.price_max_lakh is not None:
        conds.append(Property.price_lakh <= filters.price_max_lakh)

    if filters.floor_min is not None:
        conds.append(Property.floor >= filters.floor_min)
    if filters.floor_max is not None:
        conds.append(Property.floor <= filters.floor_max)

    if filters.area_min is not None:
        conds.append(Property.area_sqft >= filters.area_min)
    if filters.area_max is not None:
        conds.append(Property.area_sqft <= filters.area_max)

    return conds


def get_sort_clause(sort_override: str | None, has_sector_filter: bool) -> tuple[list[Any], str]:
    if sort_override == "price_asc":
        return [
            Property.price_lakh.asc().nulls_last(),
            Property.listing_updated_at.desc(),
            Property.property_id.asc(),
        ], "price_asc"
    elif sort_override == "price_desc":
        return [
            Property.price_lakh.desc().nulls_last(),
            Property.listing_updated_at.desc(),
            Property.property_id.asc(),
        ], "price_desc"
    elif sort_override == "newest":
        return [
            Property.listing_updated_at.desc(),
            Property.price_lakh.asc().nulls_last(),
            Property.property_id.asc(),
        ], "newest"
    elif sort_override == "area_desc":
        return [
            Property.area_sqft.desc().nulls_last(),
            Property.price_lakh.asc().nulls_last(),
            Property.property_id.asc(),
        ], "area_desc"

    # Default sort
    if has_sector_filter:
        return [
            Property.listing_updated_at.desc(),
            Property.price_lakh.asc().nulls_last(),
            Property.property_id.asc(),
        ], "newest_first_by_sector"
    else:
        return [
            Property.price_lakh.asc().nulls_last(),
            Property.listing_updated_at.desc(),
            Property.property_id.asc(),
        ], "price_asc_default"


def property_to_dict(p: Property) -> dict[str, Any]:
    return {
        "property_id": p.property_id,
        "project_name": p.project_name,
        "location_raw": p.location_raw,
        "city": p.city,
        "sector": p.sector,
        "configuration_bhk": p.configuration_bhk,
        "price_lakh": p.price_lakh,
        "price_per_sqft": p.price_per_sqft,
        "availability": p.availability,
        "floor": p.floor,
        "area_sqft": p.area_sqft,
        "listing_updated_at": p.listing_updated_at,
        "is_active": p.is_active,
    }


def search_properties(
    session: Session,
    filters: SearchFilters,
    sort: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> SearchResult:
    """Execute search query with bound parameters and calculate unverifiable IDs."""
    base_conds = build_base_conditions(filters)
    range_conds = build_range_conditions(filters)
    all_conds = base_conds + range_conds

    has_budget_filter = filters.price_min_lakh is not None or filters.price_max_lakh is not None
    has_floor_filter = filters.floor_min is not None or filters.floor_max is not None
    has_area_filter = filters.area_min is not None or filters.area_max is not None

    # 1. Matching properties query
    sort_clauses, applied_sort = get_sort_clause(sort, bool(filters.sector))

    stmt = select(Property).where(and_(*all_conds)).order_by(*sort_clauses)
    all_matching = session.execute(stmt).scalars().all()
    total = len(all_matching)

    paged = all_matching[offset : offset + limit] if limit > 0 else all_matching
    rows = [property_to_dict(p) for p in paged]

    # 2. Check unverifiable IDs (matching base conditions but NULL in filtered field)
    unverifiable_ids: list[str] = []
    if has_budget_filter or has_floor_filter or has_area_filter:
        # Conditions for candidates matching base criteria:
        base_stmt = select(Property).where(and_(*base_conds))
        candidates = session.execute(base_stmt).scalars().all()

        matching_ids = {p.property_id for p in all_matching}

        for c in candidates:
            if c.property_id in matching_ids:
                continue

            # Check if reason for exclusion was NULL in a filtered field
            is_unverifiable = False

            if has_budget_filter and c.price_lakh is None:
                # Check if floor and area (if filtered) match
                floor_ok = True
                if filters.floor_min is not None and (
                    c.floor is None or c.floor < filters.floor_min
                ):
                    floor_ok = False
                if filters.floor_max is not None and (
                    c.floor is None or c.floor > filters.floor_max
                ):
                    floor_ok = False

                area_ok = True
                if filters.area_min is not None and (
                    c.area_sqft is None or c.area_sqft < filters.area_min
                ):
                    area_ok = False
                if filters.area_max is not None and (
                    c.area_sqft is None or c.area_sqft > filters.area_max
                ):
                    area_ok = False

                if floor_ok and area_ok:
                    is_unverifiable = True

            elif has_floor_filter and c.floor is None:
                price_ok = True
                if filters.price_min_lakh is not None and (
                    c.price_lakh is None or c.price_lakh < filters.price_min_lakh
                ):
                    price_ok = False
                if filters.price_max_lakh is not None and (
                    c.price_lakh is None or c.price_lakh > filters.price_max_lakh
                ):
                    price_ok = False

                area_ok = True
                if filters.area_min is not None and (
                    c.area_sqft is None or c.area_sqft < filters.area_min
                ):
                    area_ok = False
                if filters.area_max is not None and (
                    c.area_sqft is None or c.area_sqft > filters.area_max
                ):
                    area_ok = False

                if price_ok and area_ok:
                    is_unverifiable = True

            elif has_area_filter and c.area_sqft is None:
                price_ok = True
                if filters.price_min_lakh is not None and (
                    c.price_lakh is None or c.price_lakh < filters.price_min_lakh
                ):
                    price_ok = False
                if filters.price_max_lakh is not None and (
                    c.price_lakh is None or c.price_lakh > filters.price_max_lakh
                ):
                    price_ok = False

                floor_ok = True
                if filters.floor_min is not None and (
                    c.floor is None or c.floor < filters.floor_min
                ):
                    floor_ok = False
                if filters.floor_max is not None and (
                    c.floor is None or c.floor > filters.floor_max
                ):
                    floor_ok = False

                if price_ok and floor_ok:
                    is_unverifiable = True

            if is_unverifiable:
                unverifiable_ids.append(c.property_id)

    return SearchResult(
        rows=rows,
        total=total,
        applied_sort=applied_sort,
        unverifiable_ids=sorted(unverifiable_ids),
    )


def get_property(session: Session, property_id: str) -> dict[str, Any] | None:
    """Direct lookup by property_id returning any status truthfully."""
    p = session.query(Property).filter(Property.property_id == property_id).first()
    if not p:
        return None
    return property_to_dict(p)
