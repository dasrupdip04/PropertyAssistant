from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.ingestion.sync import sync_inventory
from app.llm.client import FakeLLMClient
from app.llm.schema import (
    CityFilterValue,
    FilterAction,
    IntentType,
    NLUFilters,
    NLUResult,
    SectorFilterValue,
)
from app.main import app, get_llm_client

REAL_CSV_PATH = Path(__file__).parent.parent / "data" / "property_inventory.csv"


@pytest.fixture(autouse=True)
def seed_data(db_session):
    sync_inventory(db_session, file_path=REAL_CSV_PATH, force=True)


def test_api_chat_flow_and_remove_filter(client: TestClient):
    fake_llm = FakeLLMClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_llm

    try:
        # Turn 1: Search Noida Sector 150
        nlu_1 = NLUResult(
            intent=IntentType.SEARCH,
            wants_results_now=True,
            filters=NLUFilters(
                city=CityFilterValue(action=FilterAction.SET, raw_text="Noida"),
                sector=SectorFilterValue(action=FilterAction.SET, raw_text="150"),
            ),
        )
        fake_llm.enqueue_structured(nlu_1)
        fake_llm.enqueue_text("Here are listings in Noida Sector 150.")

        res1 = client.post("/chat", json={"message": "Noida sector 150"})
        assert res1.status_code == 200
        data1 = res1.json()
        session_id = data1["session_id"]
        assert len(data1["cards"]) > 0

        # Verify GET /chat/{session_id}
        res_get = client.get(f"/chat/{session_id}")
        assert res_get.status_code == 200
        get_data = res_get.json()
        assert get_data["session_id"] == session_id
        assert len(get_data["history"]) == 2  # user + assistant

        # Turn 2: Remove sector filter via POST /chat/remove-filter
        fake_llm.enqueue_text("Removed sector filter. Showing all Noida listings.")
        res_rm = client.post(
            "/chat/remove-filter",
            json={"session_id": session_id, "slot": "sector"},
        )
        assert res_rm.status_code == 200
        data_rm = res_rm.json()
        chip_slots = [c["slot"] for c in data_rm["filter_chips"]]
        assert "sector" not in chip_slots
        assert "city" in chip_slots
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


def test_api_validation_errors(client: TestClient):
    fake_llm = FakeLLMClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_llm

    try:
        # Empty message returns 400
        r_empty = client.post("/chat", json={"message": ""})
        assert r_empty.status_code == 400

        # Excessively long message returns 400
        r_long = client.post("/chat", json={"message": "x" * 1005})
        assert r_long.status_code == 400

        # Nonexistent session GET returns 404
        r_404 = client.get("/chat/00000000-0000-0000-0000-000000000000")
        assert r_404.status_code == 404
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
