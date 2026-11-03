from decimal import Decimal
from pathlib import Path

import pytest

from app.ingestion.parser import (
    MissingHeadersError,
    parse_csv_content,
    parse_location,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_location_sector_and_canonical_city():
    # 'Sector 16B, Greater Noida West'
    res = parse_location("Sector 16B, Greater Noida West")
    assert res is not None
    sector, city = res
    assert sector == "16B"
    assert city == "Greater Noida West"

    # Uppercased sector letter and canonical city mapping
    res = parse_location("Sector 16b, gurgaon")
    assert res is not None
    assert res == ("16B", "Gurugram")

    res = parse_location("Sector 150, Noida")
    assert res is not None
    assert res == ("150", "Noida")


def test_parse_blank_price_and_floor():
    csv_bytes = (
        b"property_id,project_name,location,configuration_bhk,price_lakh,availability,floor,area_sqft,listing_updated_at\n"
        b'P-1,Test Project,"Sector 150, Noida",3,,available,,1500,2026-10-01\n'
    )
    result = parse_csv_content(csv_bytes)
    assert len(result.errors) == 0
    assert len(result.properties) == 1
    prop = result.properties[0]
    assert prop.price_lakh is None
    assert prop.floor is None
    assert prop.price_per_sqft is None
    assert prop.area_sqft == 1500


def test_bad_availability_rejected():
    path = FIXTURES_DIR / "bad_availability.csv"
    result = parse_csv_content(path.read_bytes())
    assert len(result.properties) == 0
    assert len(result.errors) == 1
    assert "availability must be one of" in result.errors[0].reason


def test_duplicate_property_id_rejected():
    path = FIXTURES_DIR / "duplicate_id.csv"
    result = parse_csv_content(path.read_bytes())
    assert len(result.properties) == 1
    assert result.properties[0].property_id == "DUP-001"
    assert result.properties[0].project_name == "Skyline"
    assert len(result.errors) == 1
    assert "Duplicate property_id inside file" in result.errors[0].reason


def test_trailing_blank_rows_skipped():
    csv_bytes = (
        b"property_id,project_name,location,configuration_bhk,price_lakh,availability,floor,area_sqft,listing_updated_at\n"
        b'P-1,Test Project,"Sector 150, Noida",3,90,available,5,1500,2026-10-01\n'
        b"   ,   ,   ,   ,   ,   ,   ,   ,   \n"
        b",,,,,,,,\n"
        b"\n"
    )
    result = parse_csv_content(csv_bytes)
    assert result.total_rows_read == 1
    assert len(result.properties) == 1
    assert len(result.errors) == 0


def test_bom_handled():
    bom_content = (
        b"\xef\xbb\xbfproperty_id,project_name,location,configuration_bhk,price_lakh,availability,floor,area_sqft,listing_updated_at\n"
        b'BOM-1,Project BOM,"Sector 150, Noida",2,80,sold,4,1200,2026-10-01\n'
    )
    result = parse_csv_content(bom_content)
    assert len(result.properties) == 1
    assert result.properties[0].property_id == "BOM-1"
    assert result.properties[0].price_lakh == Decimal("80")


def test_missing_header_raises_error():
    path = FIXTURES_DIR / "missing_headers.csv"
    with pytest.raises(MissingHeadersError) as exc_info:
        parse_csv_content(path.read_bytes())
    assert "Missing required headers" in str(exc_info.value)
