from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.domain.filters import SearchFilters
from app.domain.search import get_property, search_properties
from app.ingestion.sync import sync_inventory

REAL_CSV_PATH = Path(__file__).parent.parent / "data" / "property_inventory.csv"


@pytest.fixture(autouse=True)
def load_real_inventory(db_session: Session):
    """Ensure real inventory is loaded into the test database."""
    sync_inventory(db_session, file_path=REAL_CSV_PATH, force=True)


def test_noida_3bhk_around_1_crore(db_session: Session):
    # Noida, 3 BHK, around 1 crore (90 to 110 lakh), no sector
    filters = SearchFilters(
        city="Noida",
        bhk_in=[3],
        price_min_lakh=Decimal("90.00"),
        price_max_lakh=Decimal("110.00"),
    )

    res = search_properties(db_session, filters)

    # In price ascending order: PROP-001 (90.00), PROP-008 (95.00), PROP-022 (102.00)
    matched_ids = [r["property_id"] for r in res.rows]
    assert matched_ids == ["PROP-001", "PROP-008", "PROP-022"]

    # PROP-009 (null price) appears in unverifiable_ids
    assert "PROP-009" in res.unverifiable_ids

    # PROP-010 (89 lakh) is excluded
    assert "PROP-010" not in matched_ids
    assert "PROP-010" not in res.unverifiable_ids

    # PROP-026 (Greater Noida West) is excluded
    assert "PROP-026" not in matched_ids
    assert "PROP-026" not in res.unverifiable_ids


def test_noida_3bhk_sector_150_no_budget(db_session: Session):
    # Noida, 3 BHK, sector 150, no budget: newest first
    filters = SearchFilters(
        city="Noida",
        sector="150",
        bhk_in=[3],
    )

    res = search_properties(db_session, filters)
    matched_ids = [r["property_id"] for r in res.rows]

    # Starts with PROP-016, PROP-011, PROP-010
    assert matched_ids[:3] == ["PROP-016", "PROP-011", "PROP-010"]

    # PROP-009 is included with null price
    assert "PROP-009" in matched_ids
    prop_9 = next(r for r in res.rows if r["property_id"] == "PROP-009")
    assert prop_9["price_lakh"] is None


def test_sold_and_reserved_filtering(db_session: Session):
    # By default, sold and reserved are not returned
    filters = SearchFilters(city="Gurugram", sector="79")
    res = search_properties(db_session, filters)
    assert res.total == 0
    assert len(res.rows) == 0

    # Explicitly requesting reserved returns PROP-013
    filters_reserved = SearchFilters(
        city="Gurugram",
        sector="79",
        availability={"available", "reserved"},
    )
    res_res = search_properties(db_session, filters_reserved)
    res_ids = [r["property_id"] for r in res_res.rows]
    assert "PROP-013" in res_ids
    assert "PROP-028" not in res_ids  # PROP-028 is sold, never returned

    # Even if sold is in availability set, it is NEVER returned
    filters_sold = SearchFilters(
        city="Gurugram",
        sector="79",
        availability={"available", "reserved", "sold"},
    )
    res_sold = search_properties(db_session, filters_sold)
    sold_ids = [r["property_id"] for r in res_sold.rows]
    assert "PROP-028" not in sold_ids


def test_exact_sector_matching_no_partial_match(db_session: Session):
    # Sector '15' must NOT match sector '150'
    filters = SearchFilters(city="Noida", sector="15")
    res = search_properties(db_session, filters)
    assert res.total == 0


def test_direct_property_lookup(db_session: Session):
    # Returns any status with its status truthfully
    p_sold = get_property(db_session, "PROP-028")
    assert p_sold is not None
    assert p_sold["property_id"] == "PROP-028"
    assert p_sold["availability"] == "sold"

    p_reserved = get_property(db_session, "PROP-013")
    assert p_reserved is not None
    assert p_reserved["availability"] == "reserved"

    p_nonexistent = get_property(db_session, "PROP-999")
    assert p_nonexistent is None
