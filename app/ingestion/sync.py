import datetime
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingestion.parser import (
    MissingHeadersError,
    ParsedProperty,
    ParseResult,
    parse_csv_file,
)
from app.models import IngestionError, IngestionRun, Property, PropertyChange

logger = logging.getLogger(__name__)

SYNC_ADVISORY_LOCK_ID = 88481234


def _values_differ(old_val: Any, new_val: Any) -> bool:
    """Check if old and new database values differ."""
    if old_val is None and new_val is None:
        return False
    if old_val is None or new_val is None:
        return True
    if isinstance(old_val, Decimal) or isinstance(new_val, Decimal):
        try:
            return Decimal(str(old_val)) != Decimal(str(new_val))
        except Exception:
            return old_val != new_val
    return old_val != new_val


def _format_val(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.isoformat()
    return str(val)


def sync_inventory(
    session: Session,
    file_path: str | Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    max_reject_ratio: float | None = None,
) -> dict[str, Any]:
    """Execute CSV-to-Postgres synchronization pipeline."""
    settings = get_settings()
    target_path = Path(file_path or settings.INVENTORY_PATH)
    reject_threshold = (
        max_reject_ratio if max_reject_ratio is not None else settings.MAX_REJECT_RATIO
    )

    # Acquire Postgres advisory xact lock so two refreshes cannot overlap
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": SYNC_ADVISORY_LOCK_ID},
    )

    now = datetime.datetime.now(datetime.UTC)
    source_str = str(target_path)

    # Check file exists and parse
    parse_result: ParseResult | None = None
    abort_reason: str | None = None

    try:
        parse_result = parse_csv_file(target_path)
    except FileNotFoundError as e:
        abort_reason = f"File missing: {e}"
    except MissingHeadersError as e:
        abort_reason = f"Missing required headers: {e}"
    except Exception as e:
        abort_reason = f"Unreadable file: {e}"

    if abort_reason is not None:
        run = IngestionRun(
            started_at=now,
            finished_at=datetime.datetime.now(datetime.UTC),
            status="aborted",
            source_path=source_str,
            file_sha256="",
            rows_read=0,
            error_message=abort_reason,
        )
        if not dry_run:
            session.add(run)
            session.commit()
            run_id = run.id
        else:
            session.rollback()
            run_id = None

        return {
            "run_id": run_id,
            "status": "aborted",
            "source_path": source_str,
            "file_sha256": "",
            "rows_read": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "deactivated": 0,
            "reactivated": 0,
            "rejected": 0,
            "error_message": abort_reason,
        }

    assert parse_result is not None

    # Check zero valid rows
    if len(parse_result.properties) == 0:
        abort_reason = "Zero valid rows in inventory file"
    else:
        # Check rejection ratio
        if parse_result.total_rows_read > 0:
            rejection_ratio = len(parse_result.errors) / parse_result.total_rows_read
            if rejection_ratio > reject_threshold:
                abort_reason = (
                    f"Rejection ratio {rejection_ratio:.2%} exceeded MAX_REJECT_RATIO "
                    f"({reject_threshold:.2%})"
                )

    if abort_reason is not None:
        run = IngestionRun(
            started_at=now,
            finished_at=datetime.datetime.now(datetime.UTC),
            status="aborted",
            source_path=source_str,
            file_sha256=parse_result.file_sha256,
            rows_read=parse_result.total_rows_read,
            rejected=len(parse_result.errors),
            error_message=abort_reason,
        )
        if not dry_run:
            session.add(run)
            session.flush()
            for err in parse_result.errors:
                session.add(
                    IngestionError(
                        run_id=run.id,
                        row_number=err.row_number,
                        property_id=err.property_id,
                        reason=err.reason,
                        raw_row=err.raw_row,
                    )
                )
            session.commit()
            run_id = run.id
        else:
            session.rollback()
            run_id = None

        return {
            "run_id": run_id,
            "status": "aborted",
            "source_path": source_str,
            "file_sha256": parse_result.file_sha256,
            "rows_read": parse_result.total_rows_read,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "deactivated": 0,
            "reactivated": 0,
            "rejected": len(parse_result.errors),
            "error_message": abort_reason,
        }

    # Optional skip: if file sha256 equals last successful run and force is false
    last_success = (
        session.query(IngestionRun)
        .filter(IngestionRun.status.in_(["success", "unchanged_file"]))
        .order_by(IngestionRun.id.desc())
        .first()
    )

    if last_success and last_success.file_sha256 == parse_result.file_sha256 and not force:
        run = IngestionRun(
            started_at=now,
            finished_at=datetime.datetime.now(datetime.UTC),
            status="unchanged_file",
            source_path=source_str,
            file_sha256=parse_result.file_sha256,
            rows_read=parse_result.total_rows_read,
            unchanged=len(parse_result.properties),
            rejected=len(parse_result.errors),
            error_message="File content sha256 unchanged from previous run",
        )
        if not dry_run:
            session.add(run)
            session.commit()
            run_id = run.id
        else:
            session.rollback()
            run_id = None

        return {
            "run_id": run_id,
            "status": "unchanged_file",
            "source_path": source_str,
            "file_sha256": parse_result.file_sha256,
            "rows_read": parse_result.total_rows_read,
            "inserted": 0,
            "updated": 0,
            "unchanged": len(parse_result.properties),
            "deactivated": 0,
            "reactivated": 0,
            "rejected": len(parse_result.errors),
            "error_message": "File content sha256 unchanged from previous run",
        }

    # Create run record
    run_status = "dry_run" if dry_run else "success"
    run = IngestionRun(
        started_at=now,
        status=run_status,
        source_path=source_str,
        file_sha256=parse_result.file_sha256,
        rows_read=parse_result.total_rows_read,
        rejected=len(parse_result.errors),
    )
    session.add(run)
    session.flush()

    # Log any parse errors from this run
    for err in parse_result.errors:
        session.add(
            IngestionError(
                run_id=run.id,
                row_number=err.row_number,
                property_id=err.property_id,
                reason=err.reason,
                raw_row=err.raw_row,
            )
        )

    # Diff calculation
    existing_props: dict[str, Property] = {p.property_id: p for p in session.query(Property).all()}
    parsed_map: dict[str, ParsedProperty] = {p.property_id: p for p in parse_result.properties}

    inserted = 0
    updated = 0
    unchanged = 0
    deactivated = 0
    reactivated = 0

    diff_fields = [
        "project_name",
        "location_raw",
        "city",
        "sector",
        "configuration_bhk",
        "price_lakh",
        "price_per_sqft",
        "availability",
        "floor",
        "area_sqft",
        "listing_updated_at",
    ]

    for prop_id, parsed in parsed_map.items():
        if prop_id not in existing_props:
            inserted += 1
            new_prop = Property(
                property_id=parsed.property_id,
                project_name=parsed.project_name,
                location_raw=parsed.location_raw,
                city=parsed.city,
                sector=parsed.sector,
                configuration_bhk=parsed.configuration_bhk,
                price_lakh=parsed.price_lakh,
                price_per_sqft=parsed.price_per_sqft,
                availability=parsed.availability,
                floor=parsed.floor,
                area_sqft=parsed.area_sqft,
                listing_updated_at=parsed.listing_updated_at,
                row_hash=parsed.row_hash,
                is_active=True,
                first_seen_at=now,
                last_seen_at=now,
                removed_at=None,
            )
            session.add(new_prop)
        else:
            existing = existing_props[prop_id]
            was_inactive = not existing.is_active

            if was_inactive:
                reactivated += 1
                existing.is_active = True
                existing.removed_at = None
                existing.last_seen_at = now

            if existing.row_hash != parsed.row_hash:
                if not was_inactive:
                    updated += 1

                for field in diff_fields:
                    old_val = getattr(existing, field)
                    new_val = getattr(parsed, field)
                    if _values_differ(old_val, new_val):
                        session.add(
                            PropertyChange(
                                run_id=run.id,
                                property_id=prop_id,
                                field=field,
                                old_value=_format_val(old_val),
                                new_value=_format_val(new_val),
                                changed_at=now,
                            )
                        )
                        setattr(existing, field, new_val)

                existing.row_hash = parsed.row_hash
                existing.last_seen_at = now
            else:
                if not was_inactive:
                    unchanged += 1
                existing.last_seen_at = now

    # Check for deactivated properties
    for prop_id, existing in existing_props.items():
        if prop_id not in parsed_map and existing.is_active:
            deactivated += 1
            existing.is_active = False
            existing.removed_at = now

    run.inserted = inserted
    run.updated = updated
    run.unchanged = unchanged
    run.deactivated = deactivated
    run.reactivated = reactivated
    run.finished_at = datetime.datetime.now(datetime.UTC)

    if dry_run:
        session.rollback()
        run_id = None
    else:
        session.commit()
        run_id = run.id

    return {
        "run_id": run_id,
        "status": run_status,
        "source_path": source_str,
        "file_sha256": parse_result.file_sha256,
        "rows_read": parse_result.total_rows_read,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "deactivated": deactivated,
        "reactivated": reactivated,
        "rejected": len(parse_result.errors),
        "error_message": None,
    }
