from decimal import Decimal

from app.domain.catalog import Catalog
from app.domain.normalizers import (
    ResolutionStatus,
    compute_area_bands,
    get_floor_band,
    get_floor_band_range,
    normalize_sector,
    parse_bhk,
    parse_budget_string,
    resolve_city,
    resolve_project,
)


def sample_catalog() -> Catalog:
    return Catalog(
        cities=["Greater Noida West", "Gurugram", "Noida"],
        sectors_by_city={
            "Noida": ["150", "62", "137", "79"],
            "Gurugram": ["57", "65", "79"],
            "Greater Noida West": ["1", "16B"],
        },
        sectors_all=["1", "16B", "57", "62", "65", "79", "137", "150"],
        project_names=["Maple Heights", "Orchid Residency", "Cedar Court", "Skyline Vista"],
        bhk_values=[2, 3, 4],
    )


def test_resolve_city_cases():
    catalog = sample_catalog()

    # Exact match
    res_noida = resolve_city("Noida", catalog)
    assert res_noida.status == ResolutionStatus.EXACT
    assert res_noida.value == "Noida"

    # Alias: ggn -> Gurugram
    res_ggn = resolve_city("ggn", catalog)
    assert res_ggn.status == ResolutionStatus.ALIAS
    assert res_ggn.value == "Gurugram"

    # Alias: Gurgaon -> Gurugram
    res_gurgaon = resolve_city("Gurgaon", catalog)
    assert res_gurgaon.status == ResolutionStatus.ALIAS
    assert res_gurgaon.value == "Gurugram"

    # Typo: gurgoan -> Gurugram
    res_typo = resolve_city("gurgoan", catalog)
    assert res_typo.status == ResolutionStatus.FUZZY
    assert res_typo.value == "Gurugram"

    # Noida vs Greater Noida West: never let 'Noida' match 'Greater Noida West'
    res_noida_exact = resolve_city("noida", catalog)
    assert res_noida_exact.value == "Noida"
    assert res_noida_exact.value != "Greater Noida West"

    res_gnw = resolve_city("greater noida west", catalog)
    assert res_gnw.value == "Greater Noida West"
    assert res_gnw.value != "Noida"

    # Unknown city: Pune
    res_pune = resolve_city("Pune", catalog)
    assert res_pune.status == ResolutionStatus.UNKNOWN
    assert res_pune.value is None


def test_normalize_sector():
    # Various inputs
    assert normalize_sector("Sec 150") == "150"
    assert normalize_sector("sector-150") == "150"
    assert normalize_sector("sctr 16b") == "16B"
    assert normalize_sector("150") == "150"
    assert normalize_sector("Sec 16b") == "16B"
    assert normalize_sector("15") == "15"

    # Invalid input
    assert normalize_sector("Unknown") is None
    assert normalize_sector("") is None


def test_resolve_project():
    catalog = sample_catalog()

    # Exact
    res_exact = resolve_project("Maple Heights", catalog)
    assert res_exact.value == "Maple Heights"

    # Fuzzy match
    res_fuzzy = resolve_project("maple height", catalog)
    assert res_fuzzy.value == "Maple Heights"

    # Unknown
    res_unknown = resolve_project("Nonexistent Tower", catalog)
    assert res_unknown.status == ResolutionStatus.UNKNOWN


def test_parse_budget_units_and_qualifiers():
    # '1 cr' -> 100 lakh
    b1 = parse_budget_string("1 cr")
    assert b1.high_lakh == Decimal("100")

    # '1.2 crore' -> 120 lakh
    b2 = parse_budget_string("1.2 crore")
    assert b2.high_lakh == Decimal("120.0")

    # '90 lakh' -> 90 lakh
    b3 = parse_budget_string("90 lakh")
    assert b3.high_lakh == Decimal("90")

    # '90L' -> 90 lakh
    b4 = parse_budget_string("90L")
    assert b4.high_lakh == Decimal("90")

    # 'under 1 cr' -> high_lakh = 100
    b5 = parse_budget_string("under 1 cr")
    assert b5.low_lakh is None
    assert b5.high_lakh == Decimal("100")

    # 'between 90 and 110'
    b6 = parse_budget_string("between 90 and 110")
    assert b6.low_lakh == Decimal("90")
    assert b6.high_lakh == Decimal("110")

    # 'around 1 cr' (10% default -> 90 to 110)
    b7 = parse_budget_string("around 1 cr")
    assert b7.low_lakh == Decimal("90.00")
    assert b7.high_lakh == Decimal("110.00")
    assert any("treated around 1 cr" in a for a in b7.assumptions)

    # Bare '2' -> treated as crore (200 lakh)
    b8 = parse_budget_string("2")
    assert b8.high_lakh == Decimal("200")
    assert any("treated bare 2 as 2 crore (200 lakh)" in a for a in b8.assumptions)

    # Bare '95' -> treated as lakh (95 lakh)
    b9 = parse_budget_string("95")
    assert b9.high_lakh == Decimal("95")
    assert any("treated bare 95 as 95 lakh" in a for a in b9.assumptions)


def test_parse_bhk():
    catalog = sample_catalog()

    # Valid in catalog
    res = parse_bhk(3, catalog)
    assert res.bhk_values == [3]
    assert len(res.missing_from_catalog) == 0

    # Valid integer but missing from catalog (5 BHK)
    res5 = parse_bhk("5 BHK", catalog)
    assert res5.bhk_values == [5]
    assert res5.missing_from_catalog == [5]
    assert any("Catalog has no listings for 5 BHK" in a for a in res5.assumptions)

    # Out of range (> 10)
    res_inv = parse_bhk(12, catalog)
    assert res_inv.bhk_values == []
    assert 12 in res_inv.invalid_values


def test_floor_and_area_helpers():
    assert get_floor_band(3) == "low"
    assert get_floor_band(10) == "mid"
    assert get_floor_band(18) == "high"

    assert get_floor_band_range("low") == (1, 5)
    assert get_floor_band_range("mid") == (6, 15)
    assert get_floor_band_range("high") == (16, None)

    areas = [1200, 1400, 1600, 1800, 2000, 2200]
    bands = compute_area_bands(areas)
    assert len(bands) == 4
