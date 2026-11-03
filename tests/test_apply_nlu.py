from app.chat.apply import NLUIssueType, apply_nlu
from app.chat.state import ChatState
from app.domain.catalog import Catalog
from app.domain.filters import SlotStatus
from app.llm.schema import (
    BHKFilterValue,
    CityFilterValue,
    FilterAction,
    IntentType,
    NLUFilters,
    NLUResult,
    SectorFilterValue,
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
        project_names=["Maple Heights", "Cedar Court"],
        bhk_values=[2, 3, 4],
    )


def test_ggn_resolves_to_gurugram():
    catalog = sample_catalog()
    state = ChatState()
    nlu = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(city=CityFilterValue(action=FilterAction.SET, raw_text="ggn")),
    )
    new_state, assumptions, issues = apply_nlu(state, nlu, catalog)
    assert not issues
    assert new_state.filters.city == "Gurugram"
    assert new_state.filters.get_slot_status("city") == SlotStatus.SPECIFIED


def test_change_of_city_clears_sector():
    catalog = sample_catalog()
    state = ChatState()
    state.filters.city = "Noida"
    state.filters.sector = "150"
    state.filters.set_slot_status("city", SlotStatus.SPECIFIED)
    state.filters.set_slot_status("sector", SlotStatus.SPECIFIED)

    nlu = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(city=CityFilterValue(action=FilterAction.SET, raw_text="Gurugram")),
    )
    new_state, assumptions, issues = apply_nlu(state, nlu, catalog)
    assert not issues
    assert new_state.filters.city == "Gurugram"
    assert new_state.filters.sector is None
    assert new_state.filters.get_slot_status("sector") == SlotStatus.UNSPECIFIED


def test_unknown_city_pune_yields_issue():
    catalog = sample_catalog()
    state = ChatState()
    nlu = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(city=CityFilterValue(action=FilterAction.SET, raw_text="Pune")),
    )
    new_state, assumptions, issues = apply_nlu(state, nlu, catalog)
    assert len(issues) == 1
    assert issues[0].issue_type == NLUIssueType.UNKNOWN_CITY
    assert issues[0].slot == "city"
    assert new_state.filters.city is None


def test_5_bhk_yields_issue_listing_available():
    catalog = sample_catalog()
    state = ChatState()
    nlu = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(bhk=BHKFilterValue(action=FilterAction.SET, values=[5])),
    )
    new_state, assumptions, issues = apply_nlu(state, nlu, catalog)
    assert len(issues) == 1
    assert issues[0].issue_type == NLUIssueType.UNAVAILABLE_BHK
    assert issues[0].available_options == [2, 3, 4]
    assert new_state.filters.bhk_in is None


def test_unsure_marks_slot_and_never_asks_again():
    catalog = sample_catalog()
    state = ChatState()
    nlu = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Noida"),
            sector=SectorFilterValue(action=FilterAction.UNSURE),
        ),
    )
    new_state, assumptions, issues = apply_nlu(state, nlu, catalog)
    assert not issues
    assert new_state.filters.city == "Noida"
    assert new_state.filters.get_slot_status("sector") == SlotStatus.UNSURE
