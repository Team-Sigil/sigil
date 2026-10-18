"""Client for the agent layer's inbound API (its README "Inbound API" and guide.md).

The agent may add response fields and outcomes within v1, so responses are read leniently:
unknown fields are dropped and the outcome stays a plain string.
"""

import logging
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.contracts.agent import PolicyDraft, PolicyDraftValidation
from gateway.core.errors import DomainError

logger = logging.getLogger(__name__)

# `process` runs one or two agents and may take minutes (agent guide §8).
PROCESS_TIMEOUT_SECONDS = 180.0
MAX_RESPONSE_BYTES = 256 * 1024


class AgentUnavailableError(DomainError):
    """The agent service could not be reached or answered outside its contract."""

    code = "AGENT_UNAVAILABLE"
    status = 502
    message = "The agent is unavailable; try again shortly."


def _unavailable(detail: str) -> AgentUnavailableError:
    """Logs the detail; the caller only ever sees the generic message (SPEC SEC-19)."""
    logger.warning("Agent service call failed: %s", detail)
    return AgentUnavailableError()


class AgentDecision(BaseModel):
    """The agent's decision for one run (`DecisionRecord`)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    dependent_id: str
    request_id: str | None
    outcome: str
    reply_to_dependent: str = Field(max_length=2_000)
    summary_for_guardian: str = Field(max_length=2_000)
    amount_sent: int = Field(ge=0)
    policy_version: int | None
    guardrail_events: list[str] = []


INTERVIEW_PROTOCOL = "sigil.policy-interview/1"
MAX_INTERVIEW_TURNS = 50

TurnKind = Literal["question", "confirmation", "review"]


class ChatTurn(BaseModel):
    """One turn of the policy interview. Agent turns carry the `kind` the agent reported."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["guardian", "agent"]
    text: str = Field(max_length=4_000)
    kind: TurnKind | None = None


class ChecklistItem(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(max_length=40)
    label: str = Field(max_length=200)
    kind: str = Field(max_length=20)
    required: bool
    status: str = Field(max_length=20)
    summary: str = Field(default="", max_length=400)


class InterviewProgress(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    required_total: int = Field(ge=0)
    required_done: int = Field(ge=0)


class InterviewTurn(BaseModel):
    """The agent's reply to one interview turn (policy-protocol.md)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    protocol: str
    status: Literal["needs_input", "ready", "unavailable"]
    phase: str = Field(max_length=20)
    question: str | None = Field(default=None, max_length=4_000)
    question_kind: TurnKind | None = None
    checklist: list[ChecklistItem] = Field(default=[], max_length=50)
    progress: InterviewProgress
    draft: PolicyDraft | None = None
    validation: PolicyDraftValidation | None = None
    guardrail_events: list[str] = []


class AgentService(Protocol):
    async def process_request(self, request_id: str) -> AgentDecision: ...

    async def interview_turn(
        self,
        guardian_id: str,
        dependent_id: str,
        conversation: list[ChatTurn],
        locale: str | None,
    ) -> InterviewTurn: ...

    async def interview_protocol(self, locale: str | None) -> dict[str, Any]: ...


class HttpAgentClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=PROCESS_TIMEOUT_SECONDS,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise _unavailable(f"transport failure: {type(exc).__name__}") from exc
        if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
            raise _unavailable(f"agent service answered {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise _unavailable("agent service sent invalid JSON") from exc

    async def process_request(self, request_id: str) -> AgentDecision:
        data = await self._post(f"/v1/requests/{request_id}/process")
        try:
            return AgentDecision.model_validate(data)
        except ValidationError as exc:
            raise _unavailable("decision outside the contract") from exc

    async def interview_turn(
        self,
        guardian_id: str,
        dependent_id: str,
        conversation: list[ChatTurn],
        locale: str | None,
    ) -> InterviewTurn:
        data = await self._post(
            "/v1/policy-interviews/turn",
            {
                "guardian_id": guardian_id,
                "dependent_id": dependent_id,
                "conversation": [turn.model_dump(exclude_none=True) for turn in conversation],
                "locale": locale,
            },
        )
        try:
            draft = data.get("draft")
            if isinstance(draft, dict):
                # PolicyDraft forbids unknown fields; keep only the ones it knows.
                data = data | {
                    "draft": {k: v for k, v in draft.items() if k in PolicyDraft.model_fields}
                }
            turn = InterviewTurn.model_validate(data)
        except (ValidationError, AttributeError, TypeError) as exc:
            raise _unavailable("interview turn outside the contract") from exc
        if turn.protocol != INTERVIEW_PROTOCOL:
            raise _unavailable(f"unsupported interview protocol {turn.protocol!r}")
        if turn.status == "needs_input" and (not turn.question or turn.question_kind is None):
            raise _unavailable("interview question without text or kind")
        if turn.status == "ready" and turn.draft is None:
            raise _unavailable("interview ready without a draft")
        return turn

    async def interview_protocol(self, locale: str | None) -> dict[str, Any]:
        try:
            response = await self._http.get(
                "/v1/policy-interviews/protocol", params={"locale": locale} if locale else None
            )
        except httpx.HTTPError as exc:
            raise _unavailable(f"transport failure: {type(exc).__name__}") from exc
        if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
            raise _unavailable(f"agent service answered {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise _unavailable("agent service sent invalid JSON") from exc
        if not isinstance(data, dict) or data.get("protocol") != INTERVIEW_PROTOCOL:
            raise _unavailable("unsupported interview protocol")
        return data
