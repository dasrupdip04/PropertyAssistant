from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, init_db
from app.ingestion.sync import sync_inventory
from app.models import IngestionError, IngestionRun


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    yield


app = FastAPI(
    title="PropertyAssistant API",
    version="0.1.0",
    description="Grounded Real-Estate Chat Assistant - Foundation",
    lifespan=lifespan,
)


def verify_admin_token(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> None:
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_TOKEN is not configured",
        )
    if x_admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
        )


DbSession = Annotated[Session, Depends(get_db)]
AdminAuth = Annotated[None, Depends(verify_admin_token)]


class RefreshRequest(BaseModel):
    force: bool = False
    dry_run: bool = False
    path: str | None = None


@app.get("/health")
def health_check(session: DbSession) -> dict[str, Any]:
    try:
        session.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unreachable: {e}",
        ) from e


@app.post("/admin/refresh")
def admin_refresh(
    session: DbSession,
    _auth: AdminAuth,
    req: RefreshRequest | None = None,
    force: bool = Query(False),
    dry_run: bool = Query(False),
    path: str | None = Query(None),
) -> dict[str, Any]:
    # Support both query params and request body
    effective_force = (req.force if req else False) or force
    effective_dry_run = (req.dry_run if req else False) or dry_run
    effective_path = (req.path if req else None) or path

    summary = sync_inventory(
        session=session,
        file_path=effective_path,
        dry_run=effective_dry_run,
        force=effective_force,
    )
    return summary


@app.get("/admin/ingestion/runs")
def list_ingestion_runs(
    session: DbSession,
    limit: int = Query(10, ge=1, le=100),
) -> list[dict[str, Any]]:
    runs = (
        session.query(IngestionRun)
        .order_by(desc(IngestionRun.id))
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "status": r.status,
            "source_path": r.source_path,
            "file_sha256": r.file_sha256,
            "rows_read": r.rows_read,
            "inserted": r.inserted,
            "updated": r.updated,
            "unchanged": r.unchanged,
            "deactivated": r.deactivated,
            "reactivated": r.reactivated,
            "rejected": r.rejected,
            "error_message": r.error_message,
        }
        for r in runs
    ]


@app.get("/admin/ingestion/runs/{run_id}/errors")
def list_ingestion_errors(
    run_id: int,
    session: DbSession,
) -> list[dict[str, Any]]:
    run = session.query(IngestionRun).filter(IngestionRun.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion run with id {run_id} not found",
        )

    errors = (
        session.query(IngestionError)
        .filter(IngestionError.run_id == run_id)
        .order_by(IngestionError.row_number)
        .all()
    )
    return [
        {
            "id": err.id,
            "run_id": err.run_id,
            "row_number": err.row_number,
            "property_id": err.property_id,
            "reason": err.reason,
            "raw_row": err.raw_row,
        }
        for err in errors
    ]
