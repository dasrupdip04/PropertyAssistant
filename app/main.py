import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from app.chat.models import ConversationTurn, SessionModel
from app.chat.service import ChatResponsePayload, handle_turn, remove_filter
from app.config import Settings, get_settings
from app.db import get_db, init_db
from app.ingestion.sync import sync_inventory
from app.llm.client import LLMClient
from app.models import IngestionError, IngestionRun

_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        from app.llm.client import GeminiClient

        _llm_client = GeminiClient()
    return _llm_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    yield


app = FastAPI(
    title="PropertyAssistant API",
    version="0.2.0",
    description="Grounded Real-Estate Chat Assistant - Chat Engine",
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=FileResponse)
def serve_chat_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", response_class=FileResponse)
def serve_admin_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = exc.errors()
    msg = (
        errors[0].get("msg", "Invalid request parameters")
        if errors
        else "Invalid request parameters"
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": msg},
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
LLMDep = Annotated[LLMClient, Depends(get_llm_client)]


class RefreshRequest(BaseModel):
    force: bool = False
    dry_run: bool = False
    path: str | None = None


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


class RemoveFilterRequest(BaseModel):
    session_id: str
    slot: str


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


@app.post("/chat")
def chat_endpoint(
    req: ChatRequest,
    session: DbSession,
    llm: LLMDep,
) -> ChatResponsePayload:
    try:
        return handle_turn(
            session_id=req.session_id,
            message=req.message,
            db=session,
            llm_client=llm,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Chat error: {e}"
        ) from e


@app.post("/chat/remove-filter")
def remove_filter_endpoint(
    req: RemoveFilterRequest,
    session: DbSession,
    llm: LLMDep,
) -> ChatResponsePayload:
    try:
        return remove_filter(
            session_id=req.session_id,
            slot=req.slot,
            db=session,
            llm_client=llm,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Filter error: {e}"
        ) from e


@app.get("/chat/{session_id}")
def get_chat_session(
    session_id: str,
    session: DbSession,
) -> dict[str, Any]:
    try:
        sess_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session_id format"
        ) from None

    s_model = session.query(SessionModel).filter(SessionModel.id == sess_uuid).first()
    if not s_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Session '{session_id}' not found"
        )

    turns = (
        session.query(ConversationTurn)
        .filter(ConversationTurn.session_id == sess_uuid)
        .order_by(ConversationTurn.id.asc())
        .all()
    )

    return {
        "session_id": str(s_model.id),
        "created_at": s_model.created_at.isoformat() if s_model.created_at else None,
        "state": s_model.state,
        "history": [
            {
                "id": t.id,
                "role": t.role,
                "content": t.content,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "debug": t.debug,
            }
            for t in turns
        ],
    }


@app.post("/admin/refresh")
def admin_refresh(
    session: DbSession,
    _auth: AdminAuth,
    req: RefreshRequest | None = None,
    force: bool = Query(False),
    dry_run: bool = Query(False),
    path: str | None = Query(None),
) -> dict[str, Any]:
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
    _token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> list[dict[str, Any]]:
    runs = session.query(IngestionRun).order_by(desc(IngestionRun.id)).limit(limit).all()
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
