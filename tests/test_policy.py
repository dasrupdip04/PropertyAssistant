from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.domain.filters import SearchFilters
from app.domain.policy import (
    ActionType,
    AskCity,
    AskFollowups,
    DialogueState,
    RunSearch,
    decide_next_action,
)
from app.ingestion.sync import sync_inventory

REAL_CSV_PATH = Path(__file__).parent.parent / "data" / "property_inventory.csv"


@pytest.fixture(autouse=True)
def load_real_inventory(db_session: Session):
    sync_inventory(db_session, file_path=REAL_CSV_PATH, force=True)


def test_sector_79_no_city_asks_city(db_session: Session):
    # Sector 79 with no city gives AskCity with Noida and Gurugram
    state = DialogueState(filters=SearchFilters(sector="79"))
    action = decide_next_action(state, db_session)

    assert isinstance(action, AskCity)
    assert action.action == ActionType.ASK_CITY
    assert set(action.options) == {"Gurugram", "Noida"}


def test_gurugram_only_returns_run_search(db_session: Session):
    # Gurugram only has 3 available results (under threshold of 5)
    state = DialogueState(filters=SearchFilters(city="Gurugram"))
    action = decide_next_action(state, db_session)

    assert isinstance(action, RunSearch)
    assert action.action == ActionType.RUN_SEARCH


def test_noida_3bhk_followup_flow(db_session: Session):
    # Noida, 3 BHK only: asks budget and sector (2 questions)
    state = DialogueState(filters=SearchFilters(city="Noida", bhk_in=[3]))
    action1 = decide_next_action(state, db_session)

    assert isinstance(action1, AskFollowups)
    assert action1.slots == ["budget", "sector"]
    assert action1.include_unsure_hint is True
    assert "budget" in action1.options
    assert "sector" in action1.options

    # Budget marked unsure: asks sector, BHK is not asked again
    state2 = DialogueState(
        filters=SearchFilters(city="Noida", bhk_in=[3]),
        followups_asked=1,
        unsure_hint_given=True,
    )
    state2.filters.mark_unsure("budget")

    action2 = decide_next_action(state2, db_session)
    assert isinstance(action2, AskFollowups)
    assert "sector" in action2.slots
    assert "bhk" not in action2.slots
    assert action2.include_unsure_hint is False

    # When followups_asked >= 2: RunSearch immediately
    state3 = DialogueState(
        filters=SearchFilters(city="Noida", bhk_in=[3]),
        followups_asked=2,
    )
    action3 = decide_next_action(state3, db_session)
    assert isinstance(action3, RunSearch)
