"""The agent zone's inbound HTTP API.

The gateway calls this service; the service only calls the platform API back. Nothing
here touches a database or the filesystem.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent_layer.auditor.auditor import Auditor
from agent_layer.config import Settings
from agent_layer.contracts import DecisionRecord, PolicyDraft, PolicyDraftValidation
from agent_layer.flows.policy import ChatTurn, draft_policy
from agent_layer.llm import LLM, AnthropicLLM
from agent_layer.maker.agent import MakerAgent
from agent_layer.platform_api import AuditorAPI, MakerAPI, PlatformAPIError, ID_PATTERN
from agent_layer.scheduler import Jobs


@dataclass
class AgentRuntime:
    settings: Settings
    maker_api: MakerAPI
    maker_llm: LLM
    maker: MakerAgent
    jobs: Jobs


def build_runtime(
    settings: Settings,
    *,
    maker_llm: LLM | None = None,
    auditor_llm: LLM | None = None,
    maker_api: MakerAPI | None = None,
    auditor_api: AuditorAPI | None = None,
) -> AgentRuntime:
    maker_llm = maker_llm or AnthropicLLM(settings.maker_model, effort=settings.maker_effort, max_tokens=settings.max_tokens)
    auditor_llm = auditor_llm or AnthropicLLM(
        settings.auditor_model, effort=settings.auditor_effort, max_tokens=settings.max_tokens
    )
    maker_api = maker_api or MakerAPI(settings.platform_base_url, settings.maker_token)
    auditor_api = auditor_api or AuditorAPI(settings.platform_base_url, settings.auditor_token)
    maker = MakerAgent(maker_llm, maker_api, Auditor(auditor_llm, auditor_api), settings.limits)
    return AgentRuntime(settings, maker_api, maker_llm, maker, Jobs(maker, maker_api, maker_llm))


class PolicyDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guardian_id: str = Field(pattern=ID_PATTERN.pattern)
    dependent_id: str = Field(pattern=ID_PATTERN.pattern)
    conversation: list[ChatTurn] = Field(min_length=1, max_length=50)


class PolicyDraftResponse(BaseModel):
    draft: PolicyDraft | None
    validation: PolicyDraftValidation


def create_app(runtime: AgentRuntime) -> FastAPI:
    app = FastAPI(title="Agent layer", docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: str = Header(default="")) -> None:
        expected = runtime.settings.inbound_token
        # Fail closed: without a configured token nothing is accepted.
        if not expected or not hmac.compare_digest(authorization, f"Bearer {expected}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/requests/{request_id}/process", dependencies=[Depends(authorize)])
    def process_request(request_id: str) -> DecisionRecord:
        if not ID_PATTERN.match(request_id):
            raise HTTPException(status_code=422, detail="invalid request id")
        try:
            return runtime.maker.handle_request(request_id)
        except PlatformAPIError as exc:
            raise HTTPException(status_code=502, detail="platform API unavailable") from exc

    @app.post("/v1/policy-drafts", dependencies=[Depends(authorize)])
    def policy_draft(body: PolicyDraftRequest) -> PolicyDraftResponse:
        try:
            result = draft_policy(
                runtime.maker_llm,
                runtime.maker_api,
                guardian_id=body.guardian_id,
                dependent_id=body.dependent_id,
                conversation=body.conversation,
                limits=runtime.settings.limits,
            )
        except PlatformAPIError as exc:
            raise HTTPException(status_code=502, detail="platform API unavailable") from exc
        return PolicyDraftResponse(draft=result.draft, validation=result.validation)

    @app.post("/v1/jobs/{name}", dependencies=[Depends(authorize)])
    def run_job(name: str) -> dict:
        job = runtime.jobs.by_name().get(name)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        try:
            job()
        except PlatformAPIError as exc:
            raise HTTPException(status_code=502, detail="platform API unavailable") from exc
        return {"ran": name}

    return app
