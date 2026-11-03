from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.domain.filters import SearchFilters
from app.domain.relax import explain_no_results
from app.ingestion.sync import sync_inventory

REAL_CSV_PATH = Path(__file__).parent.parent / "data" / "property_inventory.csv"


@pytest.fixture(autouse=True)
def load_real_inventory(db_session: Session):
    sync_inventory(db_session, file_path=REAL_CSV_PATH, force=True)


def test_sector_150_in_gurugram(db_session: Session):
    # Sector 150 with city Gurugram
    filters = SearchFilters(city="Gurugram", sector="150")
    explanation = explain_no_results(db_session, filters)

    # Explainer says Sector 150 exists in Noida
    assert "150" in explanation.sector_in_other_cities
    assert "Noida" in explanation.sector_in_other_cities["150"]


def test_gurugram_sector_79_unavailable_units(db_session: Session):
    # Gurugram, sector 79: available returns 0 results
    filters = SearchFilters(city="Gurugram", sector="79")
    explanation = explain_no_results(db_session, filters)

    # Explainer reports PROP-013 reserved and PROP-028 sold
    assert "reserved" in explanation.unavailable_units
    assert "PROP-013" in explanation.unavailable_units["reserved"]

    assert "sold" in explanation.unavailable_units
    assert "PROP-028" in explanation.unavailable_units["sold"]


def test_smallest_budget_change(db_session: Session):
    # Noida 3 BHK under 85 Lakh (lowest available 3 BHK in Noida is 89 Lakh, PROP-010)
    filters = SearchFilters(
        city="Noida",
        bhk_in=[3],
        price_max_lakh=Decimal("85.00"),
    )
    explanation = explain_no_results(db_session, filters)

    assert explanation.target_budget_lakh == Decimal("89.00")
    assert explanation.smallest_budget_change_lakh == Decimal("4.00")
