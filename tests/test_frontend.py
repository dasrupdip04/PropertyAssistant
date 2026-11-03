from starlette.testclient import TestClient

from app.domain.catalog import get_catalog
from app.ingestion.sync import sync_inventory
from app.llm.client import FakeLLMClient
from app.llm.schema import (
    BHKFilterValue,
    CityFilterValue,
    FilterAction,
    IntentType,
    NLUFilters,
    NLUResult,
)
from app.main import app, get_llm_client


def test_serve_chat_ui_root(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "PropertyAssistant" in response.text
    assert "Grounded real-estate assistant" in response.text


def test_serve_static_assets(client: TestClient):
    css_res = client.get("/static/style.css")
    assert css_res.status_code == 200
    assert "text/css" in css_res.headers["content-type"]
    assert "--primary:" in css_res.text

    js_res = client.get("/static/chat.js")
    assert js_res.status_code == 200
    assert "javascript" in js_res.headers["content-type"]
    assert "pa_session_id" in js_res.text


def test_serve_admin_ui(client: TestClient):
    response = client.get("/admin")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "PropertyAssistant Admin" in response.text
    assert "X-Admin-Token" in response.text


def test_admin_ingestion_runs_with_token(client: TestClient):
    response = client.get("/admin/ingestion/runs", headers={"X-Admin-Token": "secret-admin-token"})
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_remove_filter_flow_via_api(client: TestClient, db_session):
    # Setup inventory
    sync_inventory(db_session, file_path="data/property_inventory.csv", force=True)
    get_catalog(db_session, force_reload=True)

    fake_llm = FakeLLMClient()
    app.dependency_overrides[get_llm_client] = lambda: fake_llm

    # 1. Search for 3bhk in Noida
    nlu_search = NLUResult(
        intent=IntentType.SEARCH,
        filters=NLUFilters(
            city=CityFilterValue(action=FilterAction.SET, raw_text="Noida"),
            bhk=BHKFilterValue(action=FilterAction.SET, values=[3]),
        ),
    )
    fake_llm.enqueue_structured(nlu_search)
    fake_llm.enqueue_text("Here are 3 BHK options in Noida.")

    chat_resp = client.post("/chat", json={"message": "3bhk in noida"})
    assert chat_resp.status_code == 200
    chat_data = chat_resp.json()
    sess_id = chat_data["session_id"]

    # 2. Call remove-filter for 'bhk'
    fake_llm.enqueue_text("I removed the BHK filter. Here are available flats in Noida.")
    rem_resp = client.post(
        "/chat/remove-filter",
        json={"session_id": sess_id, "slot": "bhk"},
    )
    assert rem_resp.status_code == 200
    rem_data = rem_resp.json()
    assert rem_data["session_id"] == sess_id
    assert len(rem_data["cards"]) > 0

    app.dependency_overrides.clear()
