from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.chat.grounding import verify_grounding
from app.chat.service import handle_turn
from app.config import get_settings
from app.domain.normalizers import format_band_label, format_display_price
from app.domain.policy import compute_friendly_price_bands
from app.ingestion.sync import sync_inventory
from app.llm.client import FakeLLMClient, LLMError, session_limiter
from app.llm.schema import (
    BHKFilterValue,
    CityFilterValue,
    FilterAction,
    IntentType,
    NLUFilters,
    NLUResult,
)

REAL_CSV_PATH = Path(__file__).parent.parent / "data" / "property_inventory.csv"


@pytest.fixture(autouse=True)
def load_real_inventory(db_session: Session):
    sync_inventory(db_session, file_path=REAL_CSV_PATH, force=True)


def test_grounding_hole_ask_followups_rejects_budget(db_session: Session):
    """Test 0a: ask_followups rejects unrequested budget assumptions in responder."""
    fake_llm = FakeLLMClient()
    nlu_1 = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Noida"),
            bhk=BHKFilterValue(action=FilterAction.SET, values=[3]),
        ),
    )
    fake_llm.enqueue_structured(nlu_1)
    # Responder attempts to hallucinate an unrequested budget assumption
    fake_llm.enqueue_text(
        "Assuming your budget is around 95 to 119 lakh, which sector do you want?"
    )
    # Retry also attempts hallucination
    fake_llm.enqueue_text("Assuming your budget is 95 lakh, let me know!")

    resp = handle_turn(
        session_id=None,
        message="3bhk in noida",
        db=db_session,
        llm_client=fake_llm,
    )
    # Grounding check must reject the budget mention and fall back to template
    assert "95" not in resp.reply
    assert "119" not in resp.reply
    assert "assuming your budget" not in resp.reply.lower()
    assert "budget" in resp.reply.lower() or "sector" in resp.reply.lower()

    # Direct verify_grounding check on ask_followups plan
    ask_plan = {
        "type": "ask_followups",
        "slots": ["budget", "sector"],
        "applied_filters": {"city": "Noida", "bhk_in": [3]},
    }
    is_ok, err = verify_grounding(
        "Assuming your budget is 95 to 119 lakh, what sector do you prefer?", ask_plan
    )
    assert not is_ok
    assert "budget" in err.lower() or "price" in err.lower()


def test_deterministic_notes_and_shared_formatter(db_session: Session):
    """Test 0b & 0c: deterministic notes built in code and shared price formatter."""
    # Shared formatter test
    assert format_display_price(90.0) == "₹90 L"
    assert format_display_price(100.0) == "₹1 Cr"
    assert format_display_price(120.0) == "₹1.2 Cr"
    assert format_display_price(102.5) == "₹1.02 Cr"
    assert format_display_price(None) == "Price not listed"

    # Band labels
    assert format_band_label(None, 90.0) == "Under ₹90 L"
    assert format_band_label(90.0, 100.0) == "₹90 L – ₹1 Cr"
    assert format_band_label(100.0, 120.0) == "₹1 – 1.2 Cr"
    assert format_band_label(120.0, None) == "Above ₹1.2 Cr"

    # Friendly price bands calculation
    bands = compute_friendly_price_bands([89.0, 90.0, 95.0, 102.0, 119.0])
    assert len(bands) == 4
    assert bands[0]["label"] == "Under ₹90 L"
    assert bands[-1]["label"] == "Above ₹1.1 Cr" or "Above" in bands[-1]["label"]

    # End-to-end notes generation
    fake_llm = FakeLLMClient()
    nlu = NLUResult(
        intent=IntentType.SEARCH,
        wants_results_now=True,
        filters=NLUFilters(city=CityFilterValue(action=FilterAction.SET, raw_text="Noida")),
    )
    fake_llm.enqueue_structured(nlu)
    fake_llm.enqueue_text("Here are properties in Noida matching your criteria.")

    resp = handle_turn(
        session_id=None,
        message="Noida flats",
        db=db_session,
        llm_client=fake_llm,
    )
    assert len(resp.cards) > 0
    assert "Showing available listings only" in resp.notes
    assert len(resp.notes) >= 1
    # Check quick replies on results
    assert "Show cheaper" in resp.quick_replies
    assert "Change budget" in resp.quick_replies
    assert "New search" in resp.quick_replies


def test_session_rate_limiter(db_session: Session):
    """Test 0e: in-process sliding window rate limiter prevents > 12 req/min."""
    fake_llm = FakeLLMClient()
    sess_id = "test-rate-limit-session"
    session_limiter.reset(sess_id)

    # Exhaust 12 allowed requests
    for _ in range(12):
        allowed = session_limiter.is_allowed(sess_id)
        assert allowed is True

    # 13th request should be blocked
    blocked = session_limiter.is_allowed(sess_id)
    assert blocked is False

    # handle_turn should return friendly warning without invoking NLU
    resp = handle_turn(
        session_id=sess_id,
        message="hello",
        db=db_session,
        llm_client=fake_llm,
    )
    assert "too quickly" in resp.reply.lower()
    assert len(fake_llm.calls) == 0
    session_limiter.reset(sess_id)


def test_simulated_429_graceful_handling(db_session: Session):
    """Test 0e: 429 returns friendly message without customer-facing crash."""
    fake_llm = FakeLLMClient()
    fake_llm.enqueue_structured(LLMError("429 RESOURCE_EXHAUSTED retryDelay: '5s'"))

    resp = handle_turn(
        session_id=None,
        message="Search 3 BHK",
        db=db_session,
        llm_client=fake_llm,
    )
    assert "rephrase" in resp.reply.lower()
    assert "429" not in resp.reply
    assert "resource_exhausted" not in resp.reply.lower()


def test_debug_flag_payload(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """Test 0g: DEBUG flag controls inclusion of debug metadata."""
    settings = get_settings()

    # Case 1: DEBUG is False
    monkeypatch.setattr(settings, "DEBUG", False)
    fake_llm_1 = FakeLLMClient()
    nlu_1 = NLUResult(intent=IntentType.SMALLTALK)
    fake_llm_1.enqueue_structured(nlu_1)
    fake_llm_1.enqueue_text("Hello! How can I help you?")

    resp_1 = handle_turn(
        session_id=None,
        message="hello",
        db=db_session,
        llm_client=fake_llm_1,
    )
    assert resp_1.debug is None

    # Case 2: DEBUG is True
    monkeypatch.setattr(settings, "DEBUG", True)
    fake_llm_2 = FakeLLMClient()
    nlu_2 = NLUResult(intent=IntentType.SMALLTALK)
    fake_llm_2.enqueue_structured(nlu_2)
    fake_llm_2.enqueue_text("Hello! How can I help you?")

    resp_2 = handle_turn(
        session_id=None,
        message="hello",
        db=db_session,
        llm_client=fake_llm_2,
    )
    assert resp_2.debug is not None
    assert "intent" in resp_2.debug
    assert "plan_type" in resp_2.debug
    assert "latency_ms" in resp_2.debug
