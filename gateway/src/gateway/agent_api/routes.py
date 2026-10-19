"""The platform API the agent layer calls: `/agent/v1/*`, exactly as in the agent layer's
README ("Platform API contract") and `contracts.py`."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from gateway.agent_api import service
from gateway.agent_api.auth import AgentDep, AuditorDep, MakerDep
from gateway.contracts import agent
from gateway.contracts.crypto import CryptoClient
from gateway.core.config import Settings
from gateway.db.session import SessionDep
from gateway.engine.transfers import TransferEngine
from gateway.events import EventBus, EventType

router = APIRouter(prefix="/agent/v1", tags=["agent"])


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _crypto(request: Request) -> CryptoClient:
    crypto: CryptoClient = request.app.state.crypto
    return crypto


def _engine(request: Request) -> TransferEngine:
    engine: TransferEngine = request.app.state.engine
    return engine


def _bus(request: Request) -> EventBus:
    bus: EventBus = request.app.state.events
    return bus


SettingsDep = Annotated[Settings, Depends(_settings)]
BusDep = Annotated[EventBus, Depends(_bus)]
CryptoDep = Annotated[CryptoClient, Depends(_crypto)]
EngineDep = Annotated[TransferEngine, Depends(_engine)]


def _now() -> datetime:
    return datetime.now(UTC)


# --- reads (maker) ----------------------------------------------------------------------------


@router.get("/dependents")
async def list_dependents(_: MakerDep, session: SessionDep) -> list[agent.DependentSummary]:
    return await service.list_dependents(session)


@router.get("/dependents/{dependent_id}/context")
async def get_context(
    dependent_id: str,
    _: AgentDep,
    session: SessionDep,
    crypto: CryptoDep,
    settings: SettingsDep,
) -> agent.AgentContext:
    return await service.get_context(
        session, crypto, settings.agent_wallet_pubkey, dependent_id, _now()
    )


@router.get("/dependents/{dependent_id}/weekly-activity")
async def weekly_activity(
    dependent_id: str, _: MakerDep, session: SessionDep
) -> agent.WeeklyActivity:
    return await service.weekly_activity(session, dependent_id, _now())


@router.get("/requests/{request_id}")
async def get_request(request_id: str, _: MakerDep, session: SessionDep) -> agent.DependentRequest:
    return await service.get_request(session, request_id)


@router.get("/proofs/{proof_id}")
async def get_proof(proof_id: str, _: MakerDep, session: SessionDep) -> agent.Proof:
    return await service.get_proof(session, proof_id)


@router.get("/guardians/{guardian_id}/answers")
async def guardian_answers(
    guardian_id: str, _: MakerDep, session: SessionDep
) -> list[agent.GuardianAnswer]:
    return await service.guardian_answers(session, guardian_id)


@router.get("/usage/{family_id}")
async def get_usage(
    family_id: str, _: MakerDep, session: SessionDep, settings: SettingsDep
) -> agent.UsageStatus:
    return await service.usage_status(
        session, family_id, settings.llm_token_budget_per_guardian_per_day, _now()
    )


# --- actions (maker) --------------------------------------------------------------------------


@router.post("/transfers")
async def submit_transfer(
    body: agent.TransferInstruction,
    _: MakerDep,
    engine: EngineDep,
    session: SessionDep,
    bus: BusDep,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> agent.TransferResult:
    if idempotency_key != body.idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header mismatch.")
    result = await engine.submit(body)
    if result.status == "sent":
        members = await service.members_by_dependent(session, body.dependent_id)
        bus.publish(members.family.id, EventType.TRANSFER, {"transfer_id": result.decision_id})
    return result


@router.post("/guardian-questions")
async def ask_guardian(
    body: agent.GuardianQuestion, _: MakerDep, session: SessionDep, bus: BusDep
) -> agent.Receipt:
    return await service.ask_guardian(session, body, bus)


@router.post("/proof-requests")
async def request_proof(
    body: agent.ProofRequest, _: MakerDep, session: SessionDep
) -> agent.Receipt:
    return await service.request_proof(session, body)


@router.post("/proof-obligations/{obligation_id}/settle")
async def settle_proof_obligation(
    obligation_id: str,
    body: agent.SettleObligation,
    _: MakerDep,
    session: SessionDep,
    engine: EngineDep,
) -> agent.SettledObligation:
    """Close a receipt the dependent owed.

    The agent says which proof it read and what it found; the gateway decides whether that
    settles the obligation, so a model can never clear a debt by asserting that it is paid.
    """
    return await service.settle_obligation(
        session,
        obligation_id,
        body,
        camera_only=engine.camera_only,
        fingerprint_key=engine.fingerprint_key,
    )


@router.post("/policy-proposals")
async def propose_policy_update(
    body: agent.PolicyProposal, _: MakerDep, session: SessionDep
) -> agent.Receipt:
    return await service.propose_policy_update(session, body)


@router.post("/notifications")
async def notify(
    body: agent.Notification, _: MakerDep, session: SessionDep, bus: BusDep
) -> agent.Receipt:
    return await service.notify(session, body, bus)


@router.post("/decisions")
async def record_decision(
    body: agent.DecisionRecord, _: MakerDep, session: SessionDep, bus: BusDep
) -> agent.Receipt:
    return await service.record_decision(session, body, bus)


@router.post("/events")
async def log_event(body: agent.AgentEvent, _: MakerDep, session: SessionDep) -> agent.Receipt:
    return await service.log_event(session, body)


@router.post("/usage")
async def report_usage(
    body: agent.UsageReport, _: MakerDep, session: SessionDep, settings: SettingsDep
) -> agent.UsageStatus:
    return await service.report_usage(
        session, body, settings.llm_token_budget_per_guardian_per_day, _now()
    )


@router.post("/policy-drafts/validate")
async def validate_policy_draft(
    body: agent.PolicyDraftSubmission, _: MakerDep, session: SessionDep
) -> agent.PolicyDraftValidation:
    return await service.validate_policy_draft(session, body)


# --- Auditor only -----------------------------------------------------------------------------


@router.post("/auditor/verdicts")
async def record_verdict(
    body: agent.VerdictSubmission, _: AuditorDep, session: SessionDep
) -> agent.VerdictReceipt:
    return await service.record_verdict(session, body)
