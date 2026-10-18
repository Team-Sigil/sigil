from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import itertools
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from agent_layer.auditor.triggers import trusted_triggers
from agent_layer.contracts import PROOF_FLAGS_DISQUALIFYING
from agent_layer.contracts import (
    AgentContext,
    AgentEvent,
    Budget,
    Category,
    DecisionRecord,
    DependentRequest,
    GuardianAnswer,
    GuardianQuestion,
    HistoryItem,
    Notification,
    Outcome,
    PolicyDraftSubmission,
    PolicyProposal,
    PolicyView,
    Proof,
    ProofFindingsRecord,
    ProofRef,
    ProofRequest,
    ThreadMessage,
    TransferInstruction,
    TransferResult,
    UsageReport,
    VerdictSubmission,
)
from agent_layer.money import to_base

def proof_digest(proof: Proof) -> str | None:
    """SHA-256 of a proof's bytes: the decoded image, or the UTF-8 text."""
    try:
        raw = base64.b64decode(proof.data_b64, validate=True) if proof.data_b64 else (proof.text or "").encode()
    except (binascii.Error, ValueError):
        return None
    return hashlib.sha256(raw).hexdigest() if raw else None


def counts_as_proof(proof: Proof, *, camera_only: bool = False, origin: Proof | None = None) -> bool:
    """An image, or text the platform extracted from one; never typed text, a duplicate
    or an AI-generated image. With ``camera_only``, only a verified in-app capture
    (``origin`` is the photo an ``ocr`` text was read from)."""
    if set(proof.flags) & PROOF_FLAGS_DISQUALIFYING or proof.source == "typed":
        return False
    is_image = proof.media_type.startswith("image/")
    if not (is_image or proof.source == "ocr"):
        return False
    if not camera_only:
        return True
    if is_image:
        return proof.source == "photo"
    return (
        origin is not None and origin.source == "photo" and origin.request_id == proof.request_id
        and not set(origin.flags) & PROOF_FLAGS_DISQUALIFYING
    )


def receipt_fingerprint(f: ProofFindingsRecord) -> str:
    """The same receipt read twice gives the same fingerprint, whatever the photo."""
    def norm(v: str | None) -> str:
        return re.sub(r"\W+", "", (v or "").casefold())
    return "|".join([norm(f.merchant), re.sub(r"\D", "", f.date), str(f.total), norm(f.receipt_number)])


MAKER_TOKEN = "maker-test-token"
AUDITOR_TOKEN = "auditor-test-token"
ADMIN_TOKEN = "mock-admin-token"  # mock-only routes under /mock


@dataclass
class DependentState:
    dependent_id: str
    guardian_id: str
    name: str
    policy: PolicyView
    week_start: date
    spent_this_week: int = 0
    frozen: bool = False
    history: list[HistoryItem] = field(default_factory=list)
    dependent_locale: str | None = None
    guardian_locale: str | None = None


@dataclass
class StoredVerdict:
    submission: VerdictSubmission
    used: bool = False


class MockPlatform:
    def __init__(self, today: date | None = None, token_budget: int = 1_000_000):
        self.lock = threading.RLock()
        self.today = today or date(2026, 10, 14)  # a Wednesday
        self.default_token_budget = token_budget
        self.reset()
        self.app = self._build_app()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        with self.lock:
            self.dependents: dict[str, DependentState] = {}
            self.delegated: dict[str, int] = {}
            self.requests: dict[str, DependentRequest] = {}
            self.proofs: dict[str, Proof] = {}
            # Proof reuse, per guardian family: proof ids, content hashes and receipt
            # fingerprints that an earlier transfer already relied on.
            self.used_proofs: set[tuple[str, str]] = set()
            # The engine's side of AGENT_CAMERA_ONLY_PROOFS: both must use the same rule.
            self.camera_only_proofs = False
            self.answers: dict[str, list[GuardianAnswer]] = {}
            self.verdicts: dict[str, StoredVerdict] = {}
            self.transfers: dict[str, TransferResult] = {}
            self.sent: list[TransferInstruction] = []
            self.questions: dict[str, GuardianQuestion] = {}
            self.proof_requests: list[ProofRequest] = []
            self.proposals: dict[str, PolicyProposal] = {}
            self.notifications: dict[str, Notification] = {}
            self.undeduped_notifications: list[Notification] = []
            self.decisions: list[DecisionRecord] = []
            self.events: list[AgentEvent] = []
            self.usage: dict[str, int] = {}
            self.token_budget: dict[str, int] = {}
            self.http_log: list[tuple[str, str, str]] = []  # (token kind, method, path)
            self._ids = itertools.count(1)

    def _next(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    def add_dependent(self, state: DependentState, delegated: int | None = None) -> None:
        with self.lock:
            self.dependents[state.dependent_id] = state
            if delegated is not None:
                self.delegated[state.guardian_id] = delegated

    def add_request(self, request: DependentRequest) -> None:
        with self.lock:
            self.requests[request.id] = request

    def add_proof(self, proof: Proof) -> None:
        with self.lock:
            self.proofs[proof.id] = proof

    def proof(self, proof_id: str) -> Proof | None:
        """A stored proof with its integrity metadata. A real platform sets these at
        upload; for proofs added directly in tests, the mock derives them: the request
        is the one request that references the proof."""
        pr = self.proofs.get(proof_id)
        if pr is None:
            return None
        update: dict = {}
        if pr.request_id is None and pr.dependent_id is None:
            owners = [r for r in self.requests.values() if any(ref.id == proof_id for ref in r.proofs)]
            if len(owners) == 1:
                update |= {"request_id": owners[0].id, "dependent_id": owners[0].dependent_id}
        if pr.sha256 is None and (digest := proof_digest(pr)):
            update["sha256"] = digest
        return pr.model_copy(update=update) if update else pr

    def _proof_keys(self, guardian_id: str, pr: Proof, findings: ProofFindingsRecord | None) -> list[tuple[str, str]]:
        keys = [(guardian_id, f"id:{pr.id}")]
        if pr.sha256:
            keys.append((guardian_id, f"sha256:{pr.sha256}"))
        if findings:
            keys.append((guardian_id, f"receipt:{receipt_fingerprint(findings)}"))
        return keys

    def context(self, dependent_id: str) -> AgentContext:
        d = self.dependents[dependent_id]
        cap = d.policy.weekly_cap
        return AgentContext(
            dependent_id=d.dependent_id,
            guardian_id=d.guardian_id,
            dependent_name=d.name,
            policy=d.policy,
            budget=Budget(
                week_start=d.week_start,
                spent_this_week=d.spent_this_week,
                remaining_this_week=max(0, cap - d.spent_this_week),
            ),
            delegated_amount=self.delegated.get(d.guardian_id, 0),
            frozen=d.frozen,
            history=d.history,
            dependent_locale=d.dependent_locale,
            guardian_locale=d.guardian_locale,
        )

    # ----------------------------------------------------------------- engine

    def engine_transfer(self, t: TransferInstruction) -> TransferResult:
        """The engine's checks, in a fixed order, deny by default."""
        with self.lock:
            if t.idempotency_key in self.transfers:
                prior = self.transfers[t.idempotency_key]
                if prior.status == "sent":
                    return prior.model_copy(update={"status": "duplicate"})
                return prior

            def block(reason: str) -> TransferResult:
                result = TransferResult(status="blocked", decision_id=self._next("dec"), reason=reason)
                self.transfers[t.idempotency_key] = result
                d = self.dependents.get(t.dependent_id)
                if d:
                    d.history.append(
                        HistoryItem(at=self._now(), kind="blocked", amount=t.amount, category=t.category,
                                    purpose=t.purpose)
                    )
                return result

            d = self.dependents.get(t.dependent_id)
            if d is None:
                return block("unknown dependent")
            ctx = self.context(t.dependent_id)
            if t.policy_version != d.policy.version:
                return block("stale policy version")
            if d.frozen:
                return block("frozen by guardian")
            if t.category in d.policy.blocked_categories:
                return block("blocked category")
            if t.amount > d.policy.max_per_request:
                return block("over max per request")
            if t.amount > ctx.budget.remaining_this_week:
                return block("over weekly cap")
            if t.amount > ctx.delegated_amount:
                return block("insufficient delegation")
            request = self.requests.get(t.request_id) if t.request_id else None
            if t.trigger == "request" and request is None:
                return block("unknown request")
            needs_review = trusted_triggers(
                amount=t.amount,
                category=t.category,
                policy=d.policy,
                history=d.history,
                trigger=t.trigger,
                emergency_claimed=bool(request and request.emergency_claimed),
            )
            if needs_review:
                stored = self.verdicts.get(t.auditor_verdict_id or "")
                if stored is None:
                    return block("auditor verdict required")
                v = stored.submission
                if (
                    stored.used
                    or v.verdict != "agree"
                    or v.idempotency_key != t.idempotency_key
                    or v.amount != t.amount
                    or v.category != t.category
                    or v.dependent_id != t.dependent_id
                ):
                    return block("auditor verdict invalid")
            # Proof: the engine's own records decide, not the agent's claims.
            proofs = [self.proof(pid) for pid in t.proof_ids]
            if any(pr is None for pr in proofs):
                return block("unknown proof")
            if any(pr.request_id != t.request_id or pr.dependent_id != t.dependent_id for pr in proofs):
                return block("proof not for this request")
            if t.trigger == "request" and t.amount >= d.policy.proof_threshold:
                def counts(pr: Proof) -> bool:
                    origin = self.proof(pr.derived_from) if pr.derived_from else None
                    return counts_as_proof(pr, camera_only=self.camera_only_proofs, origin=origin)

                usable = [
                    pr for pr in proofs
                    if counts(pr) and not set(self._proof_keys(d.guardian_id, pr, t.proof_findings)) & self.used_proofs
                ]
                if not usable:
                    return block("proof already used" if any(counts(pr) for pr in proofs) else "proof required")
                f = t.proof_findings
                if f is None or not f.matches_claim or f.total < t.amount:
                    return block("proof does not cover the transfer")
            if needs_review:
                stored.used = True

            for pr in proofs:  # in the same step as the transfer: a proof backs one transfer
                self.used_proofs.update(self._proof_keys(d.guardian_id, pr, t.proof_findings))
            d.spent_this_week += t.amount
            self.delegated[d.guardian_id] -= t.amount
            d.history.append(
                HistoryItem(at=self._now(), kind="transfer", amount=t.amount, category=t.category, purpose=t.purpose)
            )
            if request:
                self.requests[request.id] = request.model_copy(update={"already_sent": request.already_sent + t.amount})
            self.sent.append(t)
            result = TransferResult(
                status="sent",
                decision_id=self._next("dec"),
                amount=t.amount,
                tx_signature=f"devnet_tx_{len(self.sent)}",
            )
            self.transfers[t.idempotency_key] = result
            return result

    def _now(self) -> datetime:
        return datetime.combine(self.today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    # -------------------------------------------------------------------- app

    def _build_app(self) -> FastAPI:  # noqa: C901 - a flat list of routes
        app = FastAPI(title="Mock platform API")
        p = self

        def caller(authorization: str) -> str:
            if hmac.compare_digest(authorization, f"Bearer {MAKER_TOKEN}"):
                return "maker"
            if hmac.compare_digest(authorization, f"Bearer {AUDITOR_TOKEN}"):
                return "auditor"
            raise HTTPException(401, "unauthorized")

        def maker_only(authorization: str) -> None:
            if caller(authorization) != "maker":
                raise HTTPException(403, "maker credential required")

        @app.middleware("http")
        async def log_requests(request: Request, call_next):
            auth = request.headers.get("authorization", "")
            kind = "maker" if MAKER_TOKEN in auth else "auditor" if AUDITOR_TOKEN in auth else "none"
            with p.lock:
                p.http_log.append((kind, request.method, request.url.path))
            return await call_next(request)

        base = "/agent/v1"

        @app.get(base + "/dependents")
        def list_dependents(authorization: str = Header("")) -> list[dict]:
            maker_only(authorization)
            return [{"dependent_id": d.dependent_id, "guardian_id": d.guardian_id} for d in p.dependents.values()]

        @app.get(base + "/dependents/{dep}/context")
        def get_context(dep: str, authorization: str = Header("")) -> AgentContext:
            maker_only(authorization)
            if dep not in p.dependents:
                raise HTTPException(404, "no such dependent")
            return p.context(dep)

        @app.get(base + "/dependents/{dep}/weekly-activity")
        def weekly(dep: str, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            d = p.dependents[dep]
            items = [h for h in d.history if h.at.date() >= d.week_start]
            return {"dependent_id": dep, "week_start": d.week_start, "items": [i.model_dump(mode="json") for i in items],
                    "weekly_cap": d.policy.weekly_cap}

        @app.get(base + "/requests/{rid}")
        def get_request(rid: str, authorization: str = Header("")) -> DependentRequest:
            maker_only(authorization)
            if rid not in p.requests:
                raise HTTPException(404, "no such request")
            return p.requests[rid]

        @app.get(base + "/proofs/{pid}")
        def get_proof(pid: str, authorization: str = Header("")) -> Proof:
            maker_only(authorization)
            proof = p.proof(pid)
            if proof is None:
                raise HTTPException(404, "no such proof")
            return proof

        @app.get(base + "/guardians/{gid}/answers")
        def answers(gid: str, authorization: str = Header("")) -> list[GuardianAnswer]:
            maker_only(authorization)
            return p.answers.get(gid, [])

        @app.get(base + "/usage/{family}")
        def get_usage(family: str, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            budget = p.token_budget.get(family, p.default_token_budget)
            return {"remaining_tokens": budget - p.usage.get(family, 0)}

        @app.post(base + "/usage")
        def report_usage(body: UsageReport, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            with p.lock:
                p.usage[body.family_id] = p.usage.get(body.family_id, 0) + body.input_tokens + body.output_tokens
                budget = p.token_budget.get(body.family_id, p.default_token_budget)
                return {"remaining_tokens": budget - p.usage[body.family_id]}

        @app.post(base + "/transfers")
        def transfers(
            body: TransferInstruction,
            authorization: str = Header(""),
            idempotency_key: str = Header("", alias="Idempotency-Key"),
        ) -> TransferResult:
            maker_only(authorization)
            if idempotency_key != body.idempotency_key:
                raise HTTPException(400, "Idempotency-Key header mismatch")
            return p.engine_transfer(body)

        @app.post(base + "/auditor/verdicts")
        def verdicts(body: VerdictSubmission, authorization: str = Header("")) -> dict:
            if caller(authorization) != "auditor":
                raise HTTPException(403, "auditor credential required")
            with p.lock:
                vid = p._next("verdict")
                p.verdicts[vid] = StoredVerdict(body)
            return {"verdict_id": vid}

        def dedupe(store: dict[str, Any], key: str, item: Any) -> dict:
            with p.lock:
                store.setdefault(key, item)
            return {"id": key}

        @app.post(base + "/guardian-questions")
        def questions(body: GuardianQuestion, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            return dedupe(p.questions, body.dedupe_key, body)

        @app.post(base + "/proof-requests")
        def proof_requests(body: ProofRequest, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            p.proof_requests.append(body)
            return {"id": p._next("proofreq")}

        @app.post(base + "/policy-proposals")
        def proposals(body: PolicyProposal, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            return dedupe(p.proposals, body.dedupe_key, body)

        @app.post(base + "/notifications")
        def notifications(body: Notification, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            if body.dedupe_key is None:
                p.undeduped_notifications.append(body)
                return {"id": p._next("note")}
            return dedupe(p.notifications, body.dedupe_key, body)

        @app.post(base + "/decisions")
        def decisions(body: DecisionRecord, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            with p.lock:
                p.decisions.append(body)
                # Off-topic messages appear in history so the weekly summary can flag them.
                # (Other non-money decisions don't yet: see "Known gaps" in the README.)
                d = p.dependents.get(body.dependent_id)
                if d is not None and body.outcome is Outcome.OUT_OF_SCOPE:
                    d.history.append(HistoryItem(at=p._now(), kind="out_of_scope", amount=0, category=Category.OTHER))
            return {"id": p._next("decision")}

        @app.post(base + "/events")
        def events(body: AgentEvent, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            p.events.append(body)
            return {"id": p._next("event")}

        @app.post(base + "/policy-drafts/validate")
        def validate(body: PolicyDraftSubmission, authorization: str = Header("")) -> dict:
            maker_only(authorization)
            d = body.draft
            errors = []
            if Category.ALLOWANCE in d.blocked_categories:
                errors.append("the allowance category cannot be blocked")
            try:
                for name in ("weekly_cap", "max_per_request", "proof_threshold"):
                    to_base(getattr(d, name))
            except ValueError as exc:
                errors.append(str(exc))
            return {"valid": not errors, "errors": errors}

        # ---- mock-only admin routes: they stand in for the gateway and the guardian's
        # wallet so a walkthrough can create requests, upload proofs and freeze.
        def admin_only(authorization: str) -> None:
            if not hmac.compare_digest(authorization, f"Bearer {ADMIN_TOKEN}"):
                raise HTTPException(401, "admin token required")

        @app.post("/mock/requests")
        def mock_add_request(body: DependentRequest, authorization: str = Header("")) -> DependentRequest:
            admin_only(authorization)
            if body.dependent_id not in p.dependents:
                raise HTTPException(404, "no such dependent")
            p.add_request(body)
            return body

        @app.post("/mock/requests/{rid}/proofs")
        def mock_add_proof(rid: str, body: Proof, authorization: str = Header("")) -> DependentRequest:
            admin_only(authorization)
            with p.lock:
                req = p.requests[rid]
                # What a real platform does at upload: bind the proof to its request,
                # hash it and flag exact duplicates within the family.
                guardian = p.dependents[req.dependent_id].guardian_id
                digest = proof_digest(body)
                family = {dep.dependent_id for dep in p.dependents.values() if dep.guardian_id == guardian}
                seen = any(
                    other.sha256 == digest or proof_digest(other) == digest
                    for other in p.proofs.values() if other.dependent_id in family
                )
                body = body.model_copy(update={
                    "request_id": rid, "dependent_id": req.dependent_id, "uploaded_at": p._now(), "sha256": digest,
                    "flags": sorted({*body.flags, "duplicate"}) if digest and seen else body.flags,
                })
                p.add_proof(body)
                updated = req.model_copy(update={
                    "proofs": [*req.proofs, ProofRef(id=body.id, media_type=body.media_type)],
                    "thread": [*req.thread, ThreadMessage(role="dependent", text="(uploaded a proof)", at=p._now())],
                })
                p.requests[rid] = updated
            return updated

        @app.post("/mock/dependents/{dep}/freeze")
        def mock_freeze(dep: str, authorization: str = Header("")) -> dict:
            admin_only(authorization)
            p.dependents[dep].frozen = True
            return {"dependent_id": dep, "frozen": True}

        @app.post("/mock/dependents/{dep}/unfreeze")
        def mock_unfreeze(dep: str, authorization: str = Header("")) -> dict:
            admin_only(authorization)
            p.dependents[dep].frozen = False
            return {"dependent_id": dep, "frozen": False}

        @app.get("/mock/state")
        def mock_state(authorization: str = Header("")) -> dict:
            admin_only(authorization)
            with p.lock:
                return {
                    "transfers": [t.model_dump(mode="json") for t in p.sent],
                    "guardian_questions": [q.model_dump(mode="json") for q in p.questions.values()],
                    "proof_requests": [r.model_dump(mode="json") for r in p.proof_requests],
                    "verdicts": {k: v.submission.model_dump(mode="json") for k, v in p.verdicts.items()},
                    "decisions": [d.model_dump(mode="json") for d in p.decisions],
                    "spent_this_week": {k: d.spent_this_week for k, d in p.dependents.items()},
                    "delegated": p.delegated,
                }

        return app
