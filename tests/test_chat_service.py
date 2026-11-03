from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.chat.service import handle_turn
from app.ingestion.sync import sync_inventory
from app.llm.client import FakeLLMClient, LLMError
from app.llm.schema import (
    AskedAttribute,
    BHKFilterValue,
    BudgetAmountUnit,
    BudgetFilterValue,
    BudgetQualifier,
    CityFilterValue,
    FilterAction,
    IntentType,
    NLUFilters,
    NLUResult,
    Reference,
    ReferenceKind,
    SectorFilterValue,
)

REAL_CSV_PATH = Path(__file__).parent.parent / "data" / "property_inventory.csv"


@pytest.fixture(autouse=True)
def load_real_inventory(db_session: Session):
    sync_inventory(db_session, file_path=REAL_CSV_PATH, force=True)


def test_conversation_flow_a_to_c(db_session: Session):
    fake_llm = FakeLLMClient()

    # Turn 1: "3bhk in noida"
    nlu_1 = NLUResult(
        intent=IntentType.SEARCH,
        starts_new_search=True,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Noida"),
            bhk=BHKFilterValue(action=FilterAction.SET, values=[3]),
        ),
    )
    fake_llm.enqueue_structured(nlu_1)
    fake_llm.enqueue_text(
        "I found 3 BHK options in Noida. What budget and sector would you prefer? Not sure is fine."
    )

    resp_1 = handle_turn(
        session_id=None,
        message="3bhk in noida",
        db=db_session,
        llm_client=fake_llm,
    )
    session_id = resp_1.session_id
    assert "budget" in resp_1.reply.lower() or "sector" in resp_1.reply.lower()

    # Turn 2: "around 1 cr, not sure about sector"
    nlu_2 = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(
            budget=BudgetFilterValue(
                action=FilterAction.SET,
                qualifier=BudgetQualifier.AROUND,
                high=BudgetAmountUnit(amount=1.0, unit="cr"),
                raw_text="around 1 cr",
            ),
            sector=SectorFilterValue(action=FilterAction.UNSURE),
        ),
    )
    fake_llm.enqueue_structured(nlu_2)
    fake_llm.enqueue_text(
        "Here are listings matching 90 to 110 lakh: PROP-001, PROP-008, PROP-022. "
        "Note PROP-009 has no price listed."
    )

    resp_2 = handle_turn(
        session_id=session_id,
        message="around 1 cr, not sure about sector",
        db=db_session,
        llm_client=fake_llm,
    )

    assert len(resp_2.cards) == 3
    card_ids = [c.property_id for c in resp_2.cards]
    assert card_ids == ["PROP-001", "PROP-008", "PROP-022"]
    assert "90 to 110" in resp_2.reply
    assert "PROP-009" in resp_2.reply

    # Flow (b): "what floor is the second one on" -> returns 'not listed' for PROP-008
    nlu_3 = NLUResult(
        intent=IntentType.PROPERTY_QUESTION,
        reference=Reference(kind=ReferenceKind.ORDINAL, ordinal=2),
        asked_attributes=[AskedAttribute.FLOOR],
    )
    fake_llm.enqueue_structured(nlu_3)
    fake_llm.enqueue_text("For PROP-008 at Maple Heights, the floor is not listed.")

    resp_3 = handle_turn(
        session_id=session_id,
        message="what floor is the second one on",
        db=db_session,
        llm_client=fake_llm,
    )
    assert "PROP-008" in resp_3.reply
    assert "not listed" in resp_3.reply.lower()

    # Flow (c): "third one's price" -> returns 102 lakh
    nlu_4 = NLUResult(
        intent=IntentType.PROPERTY_QUESTION,
        reference=Reference(kind=ReferenceKind.ORDINAL, ordinal=3),
        asked_attributes=[AskedAttribute.PRICE],
    )
    fake_llm.enqueue_structured(nlu_4)
    fake_llm.enqueue_text("The price for PROP-022 is 102 lakh.")

    resp_4 = handle_turn(
        session_id=session_id,
        message="third one's price",
        db=db_session,
        llm_client=fake_llm,
    )
    assert "PROP-022" in resp_4.reply
    assert "102" in resp_4.reply


def test_ordinal_before_search_and_out_of_range(db_session: Session):
    fake_llm = FakeLLMClient()

    # Flow (e): an ordinal before any search returns clarify_reference
    nlu_e = NLUResult(
        intent=IntentType.PROPERTY_QUESTION,
        reference=Reference(kind=ReferenceKind.ORDINAL, ordinal=1),
        asked_attributes=[AskedAttribute.PRICE],
    )
    fake_llm.enqueue_structured(nlu_e)
    resp_e = handle_turn(
        session_id=None,
        message="what is the first one's price",
        db=db_session,
        llm_client=fake_llm,
    )
    assert "No properties have been shown yet" in resp_e.reply

    # Flow (d): 'the tenth one' with 3 shown returns clarify_reference
    # First do a search showing 3
    nlu_search = NLUResult(
        intent=IntentType.SEARCH,
        wants_results_now=True,
        filters=NLUFilters(city=CityFilterValue(action=FilterAction.SET, raw_text="Gurugram")),
    )
    fake_llm.enqueue_structured(nlu_search)
    fake_llm.enqueue_text("Here are properties in Gurugram: PROP-015, PROP-029, PROP-030.")
    resp_search = handle_turn(
        session_id=None,
        message="show me Gurugram",
        db=db_session,
        llm_client=fake_llm,
    )
    assert len(resp_search.cards) == 3

    nlu_10 = NLUResult(
        intent=IntentType.PROPERTY_QUESTION,
        reference=Reference(kind=ReferenceKind.ORDINAL, ordinal=10),
        asked_attributes=[AskedAttribute.PRICE],
    )
    fake_llm.enqueue_structured(nlu_10)
    resp_10 = handle_turn(
        session_id=resp_search.session_id,
        message="tell me about the tenth one",
        db=db_session,
        llm_client=fake_llm,
    )
    assert "Position 10 is not among the currently shown properties" in resp_10.reply


def test_sector_150_gurugram_no_results(db_session: Session):
    # Flow (f): 'Sector 150 Gurugram' returns no_results, offers Noida's Sector 150 as suggestion
    fake_llm = FakeLLMClient()
    nlu = NLUResult(
        intent=IntentType.SEARCH,
        wants_results_now=True,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Gurugram"),
            sector=SectorFilterValue(action=FilterAction.SET, raw_text="150"),
        ),
    )
    fake_llm.enqueue_structured(nlu)
    fake_llm.enqueue_text(
        "No matching properties found in Gurugram. Sector 150 is located in Noida. "
        "Would you like to view Noida?"
    )

    resp = handle_turn(
        session_id=None,
        message="Sector 150 Gurugram",
        db=db_session,
        llm_client=fake_llm,
    )
    assert len(resp.cards) == 0
    assert "Noida" in resp.reply
    assert "Sector 150" in resp.reply


def test_gurugram_sector_79_reports_sold_and_reserved(db_session: Session):
    # Flow (g): Gurugram, Sector 79 returns no_results and explainer reports reserved & sold
    fake_llm = FakeLLMClient()
    nlu = NLUResult(
        intent=IntentType.SEARCH,
        wants_results_now=True,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Gurugram"),
            sector=SectorFilterValue(action=FilterAction.SET, raw_text="79"),
        ),
    )
    fake_llm.enqueue_structured(nlu)
    fake_llm.enqueue_text(
        "No available properties in Gurugram Sector 79 right now. "
        "PROP-013 is reserved and PROP-028 is sold."
    )

    resp = handle_turn(
        session_id=None,
        message="Gurugram Sector 79",
        db=db_session,
        llm_client=fake_llm,
    )
    assert len(resp.cards) == 0
    assert "PROP-013" in resp.reply
    assert "PROP-028" in resp.reply


def test_refinement_city_change_researches_immediately(db_session: Session):
    # Flow (h): after results, 'make it Gurugram' sets city and re-searches immediately
    fake_llm = FakeLLMClient()

    # Step 1: get results for Noida 2 BHK in Sector 150
    nlu_1 = NLUResult(
        intent=IntentType.SEARCH,
        wants_results_now=True,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Noida"),
            sector=SectorFilterValue(action=FilterAction.SET, raw_text="150"),
            bhk=BHKFilterValue(action=FilterAction.SET, values=[2]),
        ),
    )
    fake_llm.enqueue_structured(nlu_1)
    fake_llm.enqueue_text("Here are 2 BHK properties in Noida Sector 150.")

    resp_1 = handle_turn(
        session_id=None,
        message="2bhk in noida sector 150",
        db=db_session,
        llm_client=fake_llm,
    )
    assert len(resp_1.cards) > 0

    # Step 2: "make it Gurugram"
    nlu_2 = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Gurugram"),
        ),
    )
    fake_llm.enqueue_structured(nlu_2)
    fake_llm.enqueue_text("Updated to Gurugram. Here are the available listings.")

    resp_2 = handle_turn(
        session_id=resp_1.session_id,
        message="make it Gurugram",
        db=db_session,
        llm_client=fake_llm,
    )
    # Re-search happened immediately: cards returned, no follow-up question
    assert len(resp_2.cards) > 0
    for c in resp_2.cards:
        assert c.city == "Gurugram"


def test_show_more_cumulative_positions(db_session: Session):
    # Flow (i): 'show more' pagination keeps cumulative positions
    fake_llm = FakeLLMClient()

    nlu_1 = NLUResult(
        intent=IntentType.SEARCH,
        wants_results_now=True,
        filters=NLUFilters(city=CityFilterValue(action=FilterAction.SET, raw_text="Noida")),
    )
    fake_llm.enqueue_structured(nlu_1)
    fake_llm.enqueue_text("Here are the first 5 properties in Noida.")

    resp_1 = handle_turn(
        session_id=None, message="Noida properties", db=db_session, llm_client=fake_llm
    )
    assert len(resp_1.cards) == 5
    assert [c.position for c in resp_1.cards] == [1, 2, 3, 4, 5]

    # Show more
    nlu_more = NLUResult(intent=IntentType.SHOW_MORE)
    fake_llm.enqueue_structured(nlu_more)
    fake_llm.enqueue_text("Here are the next 5 properties in Noida.")

    resp_2 = handle_turn(
        session_id=resp_1.session_id, message="show more", db=db_session, llm_client=fake_llm
    )
    assert len(resp_2.cards) == 5
    assert [c.position for c in resp_2.cards] == [6, 7, 8, 9, 10]


def test_reset_and_special_attributes(db_session: Session):
    fake_llm = FakeLLMClient()

    # Flow (k): 'tell me about PROP-003' -> answers with status sold
    nlu_k = NLUResult(
        intent=IntentType.PROPERTY_QUESTION,
        reference=Reference(kind=ReferenceKind.PROPERTY_ID, property_ids=["PROP-003"]),
        asked_attributes=[AskedAttribute.STATUS, AskedAttribute.PRICE],
    )
    fake_llm.enqueue_structured(nlu_k)
    fake_llm.enqueue_text("PROP-003 at Maple Heights is currently sold for 110 lakh.")

    resp_k = handle_turn(
        session_id=None, message="tell me about PROP-003", db=db_session, llm_client=fake_llm
    )
    assert "PROP-003" in resp_k.reply
    assert "sold" in resp_k.reply.lower()

    # Flow (l): 'is it near the metro' -> answers 'not in our data'
    nlu_l = NLUResult(
        intent=IntentType.PROPERTY_QUESTION,
        reference=Reference(kind=ReferenceKind.PROPERTY_ID, property_ids=["PROP-001"]),
        asked_attributes=[AskedAttribute.OTHER],
    )
    fake_llm.enqueue_structured(nlu_l)
    fake_llm.enqueue_text(
        "Metro distance and neighborhood amenities are not in our data for PROP-001."
    )

    resp_l = handle_turn(
        session_id=resp_k.session_id,
        message="is it near the metro",
        db=db_session,
        llm_client=fake_llm,
    )
    assert "not in our data" in resp_l.reply.lower()

    # Flow (j): reset clears state
    nlu_j = NLUResult(intent=IntentType.RESET)
    fake_llm.enqueue_structured(nlu_j)
    fake_llm.enqueue_text("Your search has been reset. What are you looking for?")

    resp_j = handle_turn(
        session_id=resp_k.session_id, message="start over", db=db_session, llm_client=fake_llm
    )
    assert len(resp_j.cards) == 0
    assert len(resp_j.filter_chips) == 0


def test_grounding_violation_retry_and_fallback(db_session: Session):
    fake_llm = FakeLLMClient()

    nlu = NLUResult(
        intent=IntentType.SEARCH,
        wants_results_now=True,
        filters=NLUFilters(city=CityFilterValue(action=FilterAction.SET, raw_text="Gurugram")),
    )
    fake_llm.enqueue_structured(nlu)

    # First attempt hallucinates fake property PROP-999
    fake_llm.enqueue_text("I found PROP-999 in Gurugram for 500 lakh!")
    # Retry attempt also hallucinates
    fake_llm.enqueue_text("Still recommending PROP-999!")

    resp = handle_turn(session_id=None, message="Gurugram", db=db_session, llm_client=fake_llm)
    # Should fall back to template
    assert "PROP-999" not in resp.reply
    assert "I found 3 matching listings including PROP-015, PROP-029, PROP-030" in resp.reply


def test_failures_nlu_and_responder(db_session: Session):
    fake_llm = FakeLLMClient()

    # NLU failure returns friendly message with 200 payload
    fake_llm.enqueue_structured(LLMError("Simulated NLU crash"))
    resp_fail = handle_turn(
        session_id=None, message="Find me a house", db=db_session, llm_client=fake_llm
    )
    assert "rephrase" in resp_fail.reply.lower()

    # Empty message raises ValueError
    with pytest.raises(ValueError, match="Message cannot be empty"):
        handle_turn(session_id=None, message="", db=db_session, llm_client=fake_llm)

    # 5000 character message raises ValueError
    with pytest.raises(ValueError, match="Message exceeds maximum length"):
        handle_turn(session_id=None, message="a" * 5001, db=db_session, llm_client=fake_llm)
