from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import IngestionRun, Property


@dataclass
class Catalog:
    cities: list[str] = field(default_factory=list)
    sectors_by_city: dict[str, list[str]] = field(default_factory=dict)
    sectors_all: list[str] = field(default_factory=list)
    project_names: list[str] = field(default_factory=list)
    bhk_values: list[int] = field(default_factory=list)
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    floor_min: int | None = None
    floor_max: int | None = None
    area_min: int | None = None
    area_max: int | None = None
    last_run_id: int | None = None

    def sectors_for_city(self, city: str) -> list[str]:
        return self.sectors_by_city.get(city, [])

    def cities_for_sector(self, sector: str) -> list[str]:
        matching: list[str] = []
        for city, sectors in self.sectors_by_city.items():
            if sector.upper() in [s.upper() for s in sectors]:
                matching.append(city)
        return sorted(matching)


_cached_catalog: Catalog | None = None


def get_latest_successful_run_id(session: Session) -> int | None:
    return (
        session.query(func.max(IngestionRun.id))
        .filter(IngestionRun.status.in_(["success", "unchanged_file"]))
        .scalar()
    )


def load_catalog_from_db(session: Session, run_id: int | None = None) -> Catalog:
    active_props = Property.is_active.is_(True)

    city_rows = (
        session.query(Property.city).filter(active_props).distinct().order_by(Property.city).all()
    )
    cities = [r[0] for r in city_rows if r[0]]

    sector_rows = (
        session.query(Property.city, Property.sector)
        .filter(active_props)
        .distinct()
        .order_by(Property.city, Property.sector)
        .all()
    )
    sectors_by_city: dict[str, list[str]] = {c: [] for c in cities}
    all_sectors_set: set[str] = set()
    for c, s in sector_rows:
        if c in sectors_by_city and s:
            sectors_by_city[c].append(s)
        if s:
            all_sectors_set.add(s)

    project_rows = (
        session.query(Property.project_name)
        .filter(active_props)
        .distinct()
        .order_by(Property.project_name)
        .all()
    )
    project_names = [r[0] for r in project_rows if r[0]]

    bhk_rows = (
        session.query(Property.configuration_bhk)
        .filter(active_props)
        .distinct()
        .order_by(Property.configuration_bhk)
        .all()
    )
    bhk_values = [r[0] for r in bhk_rows if r[0] is not None]

    price_min, price_max = session.query(
        func.min(Property.price_lakh), func.max(Property.price_lakh)
    ).filter(active_props).first() or (None, None)

    floor_min, floor_max = session.query(func.min(Property.floor), func.max(Property.floor)).filter(
        active_props
    ).first() or (None, None)

    area_min, area_max = session.query(
        func.min(Property.area_sqft), func.max(Property.area_sqft)
    ).filter(active_props).first() or (None, None)

    return Catalog(
        cities=cities,
        sectors_by_city=sectors_by_city,
        sectors_all=sorted(all_sectors_set),
        project_names=project_names,
        bhk_values=bhk_values,
        price_min=price_min,
        price_max=price_max,
        floor_min=floor_min,
        floor_max=floor_max,
        area_min=area_min,
        area_max=area_max,
        last_run_id=run_id,
    )


def get_catalog(session: Session, force_reload: bool = False) -> Catalog:
    """Retrieve catalog, using cached instance if latest run ID matches."""
    global _cached_catalog
    current_run_id = get_latest_successful_run_id(session)

    if (
        _cached_catalog is not None
        and not force_reload
        and _cached_catalog.last_run_id == current_run_id
    ):
        return _cached_catalog

    catalog = load_catalog_from_db(session, run_id=current_run_id)
    _cached_catalog = catalog
    return catalog
