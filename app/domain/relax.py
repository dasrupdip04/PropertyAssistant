from copy import deepcopy
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.catalog import get_catalog
from app.domain.filters import SearchFilters
from app.domain.search import build_base_conditions, build_range_conditions
from app.models import Property


class DroppedConstraint(BaseModel):
    constraint: str
    count: int
    example_ids: list[str] = Field(default_factory=list)


class NoMatchExplanation(BaseModel):
    dropped_constraints: list[DroppedConstraint] = Field(default_factory=list)
    sector_in_other_cities: dict[str, list[str]] = Field(default_factory=dict)
    unavailable_units: dict[str, list[str]] = Field(default_factory=dict)
    smallest_budget_change_lakh: Decimal | None = None
    target_budget_lakh: Decimal | None = None


def explain_no_results(session: Session, filters: SearchFilters) -> NoMatchExplanation:
    """Analyze zero-match query and generate structured diagnostic facts."""
    catalog = get_catalog(session)
    active_constraints = filters.active_constraints()
    dropped_results: list[DroppedConstraint] = []

    # 1. Dropping each constraint individually
    for c in active_constraints:
        sub_filters = deepcopy(filters)
        if c == "city":
            sub_filters.city = None
        elif c == "sector":
            sub_filters.sector = None
        elif c == "project":
            sub_filters.project = None
        elif c == "bhk":
            sub_filters.bhk_in = None
            sub_filters.bhk_min = None
        elif c == "budget":
            sub_filters.price_min_lakh = None
            sub_filters.price_max_lakh = None
        elif c == "floor":
            sub_filters.floor_min = None
            sub_filters.floor_max = None
        elif c == "area":
            sub_filters.area_min = None
            sub_filters.area_max = None

        conds = build_base_conditions(sub_filters) + build_range_conditions(sub_filters)
        stmt = select(Property).where(*conds).order_by(Property.property_id.asc())
        matched = session.execute(stmt).scalars().all()
        count = len(matched)
        example_ids = [p.property_id for p in matched[:3]]

        dropped_results.append(
            DroppedConstraint(
                constraint=c,
                count=count,
                example_ids=example_ids,
            )
        )

    # 2. Check if sector exists in another city
    sector_other_cities: dict[str, list[str]] = {}
    if filters.sector and filters.city:
        all_cities_for_sector = catalog.cities_for_sector(filters.sector)
        other_cities = [c for c in all_cities_for_sector if c.lower() != filters.city.lower()]
        if other_cities:
            sector_other_cities[filters.sector.upper()] = other_cities

    # 3. Check matching units that are reserved or sold
    # Query with all active non-availability constraints (checking 'reserved' and 'sold')
    avail_conds = [Property.is_active.is_(True)]
    if filters.city:
        avail_conds.append(Property.city == filters.city)
    if filters.sector:
        avail_conds.append(Property.sector == filters.sector.upper())
    if filters.project:
        avail_conds.append(Property.project_name == filters.project)
    if filters.bhk_in:
        avail_conds.append(Property.configuration_bhk.in_(filters.bhk_in))
    elif filters.bhk_min is not None:
        avail_conds.append(Property.configuration_bhk >= filters.bhk_min)

    avail_conds.extend(build_range_conditions(filters))
    avail_conds.append(Property.availability.in_(["reserved", "sold"]))

    unavail_stmt = (
        select(Property)
        .where(*avail_conds)
        .order_by(Property.availability.asc(), Property.property_id.asc())
    )
    unavail_props = session.execute(unavail_stmt).scalars().all()

    unavailable_units: dict[str, list[str]] = {}
    for p in unavail_props:
        status_key = p.availability.lower()
        if status_key not in unavailable_units:
            unavailable_units[status_key] = []
        unavailable_units[status_key].append(p.property_id)

    # 4. Smallest budget change that would yield a result
    budget_gap: Decimal | None = None
    target_budget: Decimal | None = None

    if filters.price_max_lakh is not None:
        # What is lowest price of available properties matching everything else?
        no_budget_filters = deepcopy(filters)
        no_budget_filters.price_min_lakh = None
        no_budget_filters.price_max_lakh = None

        conds = build_base_conditions(no_budget_filters) + build_range_conditions(no_budget_filters)
        stmt = (
            select(func.min(Property.price_lakh))
            .where(*conds)
            .where(Property.price_lakh.is_not(None))
        )
        min_price = session.execute(stmt).scalar()
        if min_price is not None and min_price > filters.price_max_lakh:
            target_budget = min_price
            budget_gap = min_price - filters.price_max_lakh

    return NoMatchExplanation(
        dropped_constraints=dropped_results,
        sector_in_other_cities=sector_other_cities,
        unavailable_units=unavailable_units,
        smallest_budget_change_lakh=budget_gap,
        target_budget_lakh=target_budget,
    )
