"""The agent zone's only way to touch the rest of the system.

Every read (policy view, budget, history, requests, proofs) and every action
(transfer, guardian question, notification, decision log) is an HTTPS call to the
platform API. The agent has no database driver, no file access and no keys.

Two credentials, two clients:

* ``MakerAPI`` – used by the main agent. It can read context and *request* actions;
  the engine decides whether they happen.
* ``AuditorAPI`` – used only by the Auditor. It reads the dependent's context itself and
  its single write is posting a verdict, so a compromised maker (holding only the maker
  token) can neither forge an "agree" nor feed the Auditor a doctored policy or history.

Responses are not trusted either: a body over ``MAX_RESPONSE_BYTES``, invalid JSON or
anything outside the contract raises ``PlatformAPIError``, so the run fails closed.
"""

from __future__ import annotations

import json as jsonlib
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from agent_layer.contracts import (
    AgentContext,
    AgentEvent,
    DecisionRecord,
    DependentRequest,
    DependentSummary,
    GuardianAnswer,
    GuardianQuestion,
    Notification,
    PolicyDraftSubmission,
    PolicyDraftValidation,
    PolicyProposal,
    Proof,
    ProofRequest,
    Receipt,
    TransferInstruction,
    TransferResult,
    UsageReport,
    UsageStatus,
    VerdictReceipt,
    VerdictSubmission,
    WeeklyActivity,
)

T = TypeVar("T", bound=BaseModel)

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The largest legitimate response is one proof at the 5 MB limit, base64-encoded.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024 + 64 * 1024


class PlatformAPIError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"platform API error ({status}): {message}")
        self.status = status


def _id(value: str) -> str:
    """Path segments come from upstream callers; refuse anything that is not a plain id."""
    if not ID_PATTERN.match(value):
        raise PlatformAPIError(None, f"invalid identifier {value!r}")
    return value


class _Client:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/agent/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def _send(self, method: str, path: str, *, body: Any = None, headers: dict[str, str] | None = None) -> bytes:
        """Send one request and read at most ``MAX_RESPONSE_BYTES`` of the body."""
        request = self._http.build_request(method, path, json=body, headers=headers)
        resp = self._http.send(request, stream=True)
        try:
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise PlatformAPIError(resp.status_code, "response too large")
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise PlatformAPIError(resp.status_code, "response too large")
                chunks.append(chunk)
        finally:
            resp.close()
        content = b"".join(chunks)
        if resp.status_code >= 400:
            raise PlatformAPIError(resp.status_code, content[:300].decode(errors="replace"))
        return content

    @staticmethod
    def _parse(content: bytes, adapter: TypeAdapter) -> Any:
        try:
            return adapter.validate_python(jsonlib.loads(content))
        except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
            raise PlatformAPIError(None, f"response outside the contract: {type(exc).__name__}") from exc

    def _request(
        self,
        method: str,
        path: str,
        model: type[T],
        *,
        json: BaseModel | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 0,
    ) -> T:
        body: Any = json.model_dump(mode="json") if json is not None else None
        last_exc: Exception | None = None
        for _ in range(retries + 1):
            try:
                content = self._send(method, path, body=body, headers=headers)
            except httpx.TransportError as exc:
                last_exc = exc
                continue
            return self._parse(content, TypeAdapter(model))
        raise PlatformAPIError(None, f"transport failure: {last_exc}")

    def _list(self, path: str, model: type[T]) -> list[T]:
        try:
            content = self._send("GET", path)
        except httpx.TransportError as exc:
            raise PlatformAPIError(None, f"transport failure: {exc}") from exc
        return self._parse(content, TypeAdapter(list[model]))


class MakerAPI(_Client):
    # ---- reads (safe to retry)
    def get_context(self, dependent_id: str) -> AgentContext:
        return self._request("GET", f"/dependents/{_id(dependent_id)}/context", AgentContext, retries=2)

    def get_request(self, request_id: str) -> DependentRequest:
        return self._request("GET", f"/requests/{_id(request_id)}", DependentRequest, retries=2)

    def get_proof(self, proof_id: str) -> Proof:
        return self._request("GET", f"/proofs/{_id(proof_id)}", Proof, retries=2)

    def list_dependents(self) -> list[DependentSummary]:
        return self._list("/dependents", DependentSummary)

    def get_guardian_answers(self, guardian_id: str) -> list[GuardianAnswer]:
        return self._list(f"/guardians/{_id(guardian_id)}/answers", GuardianAnswer)

    def get_weekly_activity(self, dependent_id: str) -> WeeklyActivity:
        return self._request("GET", f"/dependents/{_id(dependent_id)}/weekly-activity", WeeklyActivity, retries=2)

    def get_usage(self, family_id: str) -> UsageStatus:
        return self._request("GET", f"/usage/{_id(family_id)}", UsageStatus, retries=2)

    # ---- actions (the engine decides; transfers are idempotent so one retry is safe)
    def submit_transfer(self, instruction: TransferInstruction) -> TransferResult:
        return self._request(
            "POST",
            "/transfers",
            TransferResult,
            json=instruction,
            headers={"Idempotency-Key": instruction.idempotency_key},
            retries=1,
        )

    def ask_guardian(self, question: GuardianQuestion) -> Receipt:
        return self._request("POST", "/guardian-questions", Receipt, json=question)

    def request_proof(self, req: ProofRequest) -> Receipt:
        return self._request("POST", "/proof-requests", Receipt, json=req)

    def propose_policy_update(self, proposal: PolicyProposal) -> Receipt:
        return self._request("POST", "/policy-proposals", Receipt, json=proposal)

    def notify(self, notification: Notification) -> Receipt:
        return self._request("POST", "/notifications", Receipt, json=notification)

    def record_decision(self, record: DecisionRecord) -> Receipt:
        return self._request("POST", "/decisions", Receipt, json=record)

    def log_event(self, event: AgentEvent) -> Receipt:
        return self._request("POST", "/events", Receipt, json=event)

    def report_usage(self, report: UsageReport) -> UsageStatus:
        return self._request("POST", "/usage", UsageStatus, json=report)

    def validate_policy_draft(self, submission: PolicyDraftSubmission) -> PolicyDraftValidation:
        return self._request("POST", "/policy-drafts/validate", PolicyDraftValidation, json=submission)


class AuditorAPI(_Client):
    def get_context(self, dependent_id: str) -> AgentContext:
        """The Auditor reads the dependent's context itself, with its own token, instead of
        trusting the maker's snapshot (read-only; the only read the Auditor's token has)."""
        return self._request("GET", f"/dependents/{_id(dependent_id)}/context", AgentContext, retries=2)

    def submit_verdict(self, submission: VerdictSubmission) -> VerdictReceipt:
        return self._request("POST", "/auditor/verdicts", VerdictReceipt, json=submission)
