import tempfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.ingestion.sync import sync_inventory
from app.models import IngestionRun, Property, PropertyChange
from tests.conftest import TestingSessionLocal

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REAL_CSV_PATH = Path(__file__).parent.parent / "data" / "property_inventory.csv"


def test_first_load_and_second_run(db_session: Session):
    # First load
    res1 = sync_inventory(db_session, file_path=REAL_CSV_PATH)
    assert res1["status"] == "success"
    assert res1["inserted"] == 36
    assert res1["rejected"] == 0
    assert res1["updated"] == 0
    assert res1["deactivated"] == 0

    # Verify PROP-008 has NULL floor and PROP-009 has NULL price_lakh
    prop_8 = db_session.query(Property).filter(Property.property_id == "PROP-008").first()
    assert prop_8 is not None
    assert prop_8.floor is None
    assert prop_8.price_lakh == Decimal("95")

    prop_9 = db_session.query(Property).filter(Property.property_id == "PROP-009").first()
    assert prop_9 is not None
    assert prop_9.price_lakh is None
    assert prop_9.price_per_sqft is None

    # Second run without force: status is unchanged_file
    res2 = sync_inventory(db_session, file_path=REAL_CSV_PATH, force=False)
    assert res2["status"] == "unchanged_file"
    assert res2["unchanged"] == 36
    assert res2["inserted"] == 0

    # Second run with force: status is success, all 36 unchanged
    res3 = sync_inventory(db_session, file_path=REAL_CSV_PATH, force=True)
    assert res3["status"] == "success"
    assert res3["unchanged"] == 36
    assert res3["inserted"] == 0
    assert res3["updated"] == 0


def test_edited_price_and_property_changes(db_session: Session):
    # Initial load of 2 items
    valid_csv = FIXTURES_DIR / "valid_sample.csv"
    res1 = sync_inventory(db_session, file_path=valid_csv, force=True)
    assert res1["inserted"] == 2

    # Create modified file with edited price for TEST-001
    with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as f:
        header = (
            "property_id,project_name,location,configuration_bhk,price_lakh,"
            "availability,floor,area_sqft,listing_updated_at,notes\n"
        )
        row1 = (
            'TEST-001,Skyline Towers,"Sector 16B, Greater Noida West",'
            "3,110,available,12,1650,2026-10-01,Test note\n"
        )
        row2 = (
            'TEST-002,Green Valley,"Sector 150, Noida",2,,available,,1200,2026-10-02,Blank price\n'
        )
        f.write(header + row1 + row2)
        mod_path = f.name

    try:
        res2 = sync_inventory(db_session, file_path=mod_path, force=True)
        assert res2["updated"] == 1
        assert res2["unchanged"] == 1

        # Check property changes
        changes = (
            db_session.query(PropertyChange).filter(PropertyChange.property_id == "TEST-001").all()
        )
        field_names = [c.field for c in changes]
        assert "price_lakh" in field_names
        price_change = next(c for c in changes if c.field == "price_lakh")
        assert price_change.old_value in ("85", "85.00")
        assert price_change.new_value in ("110", "110.00")

        # Verify property in DB updated
        prop = db_session.query(Property).filter(Property.property_id == "TEST-001").first()
        assert prop.price_lakh == Decimal("110")
    finally:
        Path(mod_path).unlink(missing_ok=True)


def test_deactivated_and_reactivated_lifecycle(db_session: Session):
    valid_csv = FIXTURES_DIR / "valid_sample.csv"
    sync_inventory(db_session, file_path=valid_csv, force=True)

    # File with only TEST-001 (TEST-002 removed)
    with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False) as f:
        header = (
            "property_id,project_name,location,configuration_bhk,price_lakh,"
            "availability,floor,area_sqft,listing_updated_at,notes\n"
        )
        row1 = (
            'TEST-001,Skyline Towers,"Sector 16B, Greater Noida West",'
            "3,85,available,12,1650,2026-10-01,Test note\n"
        )
        f.write(header + row1)
        sub_path = f.name

    try:
        res_remove = sync_inventory(db_session, file_path=sub_path, force=True)
        assert res_remove["deactivated"] == 1
        assert res_remove["unchanged"] == 1

        p2 = db_session.query(Property).filter(Property.property_id == "TEST-002").first()
        assert p2.is_active is False
        assert p2.removed_at is not None

        # Restore TEST-002 back
        res_restore = sync_inventory(db_session, file_path=valid_csv, force=True)
        assert res_restore["reactivated"] == 1

        db_session.refresh(p2)
        assert p2.is_active is True
        assert p2.removed_at is None
    finally:
        Path(sub_path).unlink(missing_ok=True)


def test_empty_file_aborts_and_leaves_data_intact(db_session: Session):
    # Seed data
    valid_csv = FIXTURES_DIR / "valid_sample.csv"
    sync_inventory(db_session, file_path=valid_csv, force=True)

    # Empty file
    empty_csv = FIXTURES_DIR / "empty.csv"
    res = sync_inventory(db_session, file_path=empty_csv, force=True)
    assert res["status"] == "aborted"
    assert "Zero valid rows" in res["error_message"]

    # Verify 2 properties still exist and active
    active_props = db_session.query(Property).filter(Property.is_active.is_(True)).count()
    assert active_props == 2


def test_missing_header_aborts(db_session: Session):
    missing_csv = FIXTURES_DIR / "missing_headers.csv"
    res = sync_inventory(db_session, file_path=missing_csv, force=True)
    assert res["status"] == "aborted"
    assert "Missing required headers" in res["error_message"]


def test_dry_run_commits_nothing(db_session: Session):
    res = sync_inventory(db_session, file_path=REAL_CSV_PATH, dry_run=True)
    assert res["status"] == "dry_run"
    assert res["inserted"] == 36
    assert res["run_id"] is None

    # Nothing should be in DB
    assert db_session.query(Property).count() == 0
    assert db_session.query(IngestionRun).count() == 0


def test_two_simultaneous_refreshes_do_not_corrupt_data():
    def run_sync():
        with TestingSessionLocal() as session:
            return sync_inventory(session, file_path=REAL_CSV_PATH, force=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_sync)
        f2 = executor.submit(run_sync)
        res1 = f1.result()
        res2 = f2.result()

    assert res1["status"] in ("success", "unchanged_file")
    assert res2["status"] in ("success", "unchanged_file")

    with TestingSessionLocal() as session:
        count = session.query(Property).count()
        assert count == 36
