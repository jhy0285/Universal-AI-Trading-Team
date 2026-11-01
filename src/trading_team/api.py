from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field
from sqlalchemy import select

from trading_team import learning
from trading_team.data import DataError
from trading_team.domain import PredictionQuestion, StrictModel, VenueTarget, utcnow
from trading_team.engine import Desk
from trading_team.metrics import comparisons, costs, performance
from trading_team.operations import LeaseBusy, LeaseManager
from trading_team.replay import ReplayCase, run_replay
from trading_team.settings import Settings
from trading_team.storage import (
    Account,
    AgentRecord,
    AuditEvent,
    Cycle,
    Database,
    Decision,
    EquityPoint,
    PaperFill,
    PaperOrder,
    Position,
    SnapshotRow,
    as_dict,
)


class RunRequest(StrictModel):
    account_id: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,100}$")


class KillRequest(StrictModel):
    account_id: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    active: bool
    reason: str = Field(min_length=1, max_length=500)


class BookRequest(RunRequest):
    question: PredictionQuestion
    targets: list[VenueTarget] = Field(min_length=1, max_length=10)


class LearningRunRequest(StrictModel):
    account_id: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    lookback_days: int = Field(default=7, ge=1, le=90)


class ProposalReview(StrictModel):
    account_id: str = Field(default="main", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    status: Literal["acknowledged", "dismissed", "applied"]
    note: str = Field(default="", max_length=500)


class ReplayRequest(StrictModel):
    account_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    case: ReplayCase
    use_model: bool = False


def create_app(settings: Settings | None = None, desk: Desk | None = None) -> FastAPI:
    settings = settings or Settings()
    db = desk.db if desk else Database(settings.database_url)
    desk = desk or Desk(settings, db)
    leases = LeaseManager(db)

    @asynccontextmanager
    async def lifespan(_app):
        db.initialize(settings.paper_initial_cash)
        desk.activate_config()
        yield
        await desk.close()
        db.engine.dispose()

    app = FastAPI(
        title="Universal AI Trading Team — PAPER ONLY", version="0.1.0", lifespan=lifespan
    )
    app.state.desk = desk
    security = HTTPBearer(auto_error=False)
    admin_credentials = Depends(security)

    @app.get("/providers")
    def providers():
        from trading_team.cli_registry import inventory, transport_info

        return {
            "active": settings.public(),
            "transport": transport_info(settings),
            "cli_profiles": inventory(),
            "http_providers": [
                "openai",
                "anthropic",
                "google",
                "xai",
                "ollama",
                "openai_compatible",
                "llama_cpp",
            ],
            "selection": "One global provider/model; no automatic fallback.",
        }

    def admin(credentials: HTTPAuthorizationCredentials | None = admin_credentials):
        token = settings.api_admin_token.get_secret_value()
        if not token:
            raise HTTPException(503, "ADMIN_TOKEN_NOT_CONFIGURED")
        if not credentials or not hmac.compare_digest(
            credentials.credentials.encode(), token.encode()
        ):
            raise HTTPException(
                401, "ADMIN_AUTHENTICATION_REQUIRED", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.exception_handler(LeaseBusy)
    async def busy_handler(_request, _exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "WORK_ALREADY_RUNNING"}, status_code=409)

    @app.exception_handler(DataError)
    async def data_handler(_request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": exc.code}, status_code=503)

    @app.exception_handler(ValueError)
    async def value_handler(_request, _exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "INVALID_REQUEST_OR_UNKNOWN_ACCOUNT"}, status_code=422)

    def rows(model, account_id=None, limit=200):
        query = select(model)
        if account_id is not None:
            query = query.where(model.account_id == account_id)
        query = query.order_by(model.id.desc()).limit(limit)
        with db.transaction() as s:
            return [as_dict(row) for row in s.scalars(query)]

    @app.get("/health")
    def health():
        with db.transaction() as s:
            s.get(Account, "main")
        return {"status": "ok", "paper_only": True, "time": utcnow().isoformat()}

    @app.get("/config")
    def config():
        return {
            **settings.public(),
            "capabilities": desk.capabilities(),
            "writes_enabled": bool(settings.api_admin_token.get_secret_value()),
            "configuration_change": "Update global environment and restart API + worker; probe again before running cycles.",
        }

    @app.get("/readiness")
    async def readiness():
        from trading_team.autonomy import status as automation_status

        wm = await desk.wm.readiness()
        blockers = settings.model_blockers()
        if not desk.capabilities().get("verified"):
            blockers.append("MODEL_PROBE_REQUIRED")
        model_ready = not blockers
        snapshots = desk.latest_snapshots(limit=100)
        fresh = sum(x.fresh(utcnow(), settings.market_max_age_seconds) for x in snapshots)
        if not fresh:
            blockers.append("NO_VERIFIED_FRESH_MARKET_SNAPSHOTS")
        return {
            "status": "READY" if not blockers and wm["status"] == "READY" else "degraded",
            "paper_only": True,
            "blockers": blockers,
            "world_monitor": wm,
            "book_enabled": wm["status"] == "READY" and model_ready,
            "meme_enabled": model_ready and fresh > 0,
            "risk_independent_of_llm": True,
            "live_trading_available": False,
            "automation": automation_status(desk),
        }

    @app.get("/automation")
    def automation():
        from trading_team.autonomy import status

        return status(desk)

    @app.get("/accounts")
    def accounts():
        return rows(Account)

    @app.get("/metrics")
    def metrics(account_id: str = "main"):
        return performance(db, account_id)

    @app.get("/equity")
    def equity(account_id: str = "main", limit: int = Query(200, ge=1, le=1000)):
        return rows(EquityPoint, account_id, limit)

    @app.get("/positions")
    def positions(account_id: str = "main"):
        return rows(Position, account_id)

    @app.get("/orders")
    def orders(account_id: str = "main", limit: int = Query(200, ge=1, le=1000)):
        return rows(PaperOrder, account_id, limit)

    @app.get("/fills")
    def fills(order_id: str):
        with db.transaction() as s:
            return [
                as_dict(x)
                for x in s.scalars(select(PaperFill).where(PaperFill.order_id == order_id))
            ]

    @app.get("/agents")
    def agents():
        return {
            "health": desk.supervisor.status(),
            "messages": rows(AgentRecord),
            "ai_roles": ["CHIEF", "SCAN", "VET", "BOOK"],
            "deterministic_roles": ["SIZE", "FILLS", "RISK"],
        }

    @app.get("/cycles")
    def cycles(account_id: str = "main"):
        return rows(Cycle, account_id)

    @app.get("/decisions")
    def decisions(account_id: str = "main"):
        return rows(Decision, account_id)

    @app.get("/snapshots")
    def snapshots(provenance: str = "live"):
        if provenance not in {"live", "recorded", "fixture"}:
            raise HTTPException(422, "INVALID_PROVENANCE")
        return [
            {**x.model_dump(mode="json"), "snapshot_id": x.digest}
            for x in desk.latest_snapshots(provenance)
        ]

    @app.get("/events")
    def events():
        with db.transaction() as s:
            return [
                as_dict(x)
                for x in s.scalars(
                    select(SnapshotRow)
                    .where(SnapshotRow.kind == "wm_signals")
                    .order_by(SnapshotRow.fetched_at.desc())
                    .limit(20)
                )
            ]

    @app.get("/book/discovery")
    async def discovery():
        return await desk.wm.discovery()

    @app.get("/costs")
    def cost_report():
        return costs(db)

    @app.get("/comparison")
    def comparison():
        return comparisons(db)

    @app.get("/learning")
    def learning_report(account_id: str = "main"):
        return learning.latest(db, account_id)

    @app.post("/learning/run", dependencies=[Depends(admin)])
    async def learning_run(body: LearningRunRequest):
        async with leases.hold("learning:" + body.account_id):
            return await asyncio.to_thread(
                learning.review, db, body.account_id, None, body.lookback_days
            )

    @app.post("/learning/proposals/{proposal_id}", dependencies=[Depends(admin)])
    def learning_review(proposal_id: str, body: ProposalReview):
        try:
            return learning.set_status(db, body.account_id, proposal_id, body.status, body.note)
        except LookupError:
            raise HTTPException(404, "UNKNOWN_PROPOSAL") from None

    @app.get("/audit")
    def audits(account_id: str = "main"):
        return {
            "integrity": desk.broker.integrity(account_id),
            "events": rows(AuditEvent, account_id),
        }

    @app.post("/model/probe", dependencies=[Depends(admin)])
    async def probe():
        async with leases.hold("model-probe"):
            return await desk.probe()

    @app.post("/data/collect", dependencies=[Depends(admin)])
    async def collect():
        async with leases.hold("collect"):
            result = await desk.collect()
            result["world_monitor"] = await desk.wm.readiness()
            return result

    @app.post("/cycles/run", dependencies=[Depends(admin)])
    async def run(body: RunRequest):
        async with leases.hold("cycle:" + body.account_id):
            return await desk.run_cycle(body.account_id, body.request_id)

    @app.post("/risk/run", dependencies=[Depends(admin)])
    async def risk(body: RunRequest):
        async with leases.hold("risk:" + body.account_id):
            return await desk.run_risk(body.account_id)

    @app.post("/book/evaluate", dependencies=[Depends(admin)])
    async def book(body: BookRequest):
        async with leases.hold("cycle:" + body.account_id):
            discovered = await desk.wm.discovery()
            if not any(
                x.get("id") == body.question.question_id and x.get("title") == body.question.title
                for x in discovered["markets"]
            ):
                raise HTTPException(422, "QUESTION_NOT_IN_LOCAL_WORLD_MONITOR")
            return await desk.run_book(
                body.account_id,
                body.request_id,
                body.question,
                lambda: desk.prediction.prices_for(body.question, body.targets),
            )

    @app.post("/kill", dependencies=[Depends(admin)])
    def kill(body: KillRequest):
        desk.broker.kill(body.account_id, body.active, body.reason)
        return {"account_id": body.account_id, "killed": body.active, "paper_only": True}

    @app.post("/replay", dependencies=[Depends(admin)])
    async def replay(body: ReplayRequest):
        async with leases.hold("replay"):
            return await run_replay(desk, body.case, body.account_id, use_model=body.use_model)

    return app


def app_factory():
    return create_app()
