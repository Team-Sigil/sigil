from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from agent_layer.auditor.auditor import Auditor
from agent_layer.config import Limits
from agent_layer.contracts import AuditorVerdict, DependentRequest, IndependentAssessment
from agent_layer.maker.agent import MakerAgent
from agent_layer.platform_api import AuditorAPI, MakerAPI
from mock_platform.app import AUDITOR_TOKEN, MAKER_TOKEN, MockPlatform
from mock_platform.seed import seed_family
from mock_platform.server import BackgroundServer
from tests.fakes import ScriptedLLM

TODAY = date(2026, 10, 14)  # Wednesday of the seeded week


@pytest.fixture(scope="session")
def _server():
    platform = MockPlatform(today=TODAY)
    server = BackgroundServer(platform.app)
    url = server.start()
    yield platform, url
    server.stop()


@pytest.fixture
def platform(_server) -> MockPlatform:
    """A fresh, seeded mock platform for each test, served over real HTTP."""
    platform, _ = _server
    platform.reset()
    platform.today = TODAY
    platform.default_token_budget = 1_000_000
    seed_family(platform)
    return platform


@pytest.fixture
def base_url(_server) -> str:
    return _server[1]


@pytest.fixture
def maker_api(base_url, platform):
    api = MakerAPI(base_url, MAKER_TOKEN)
    yield api
    api.close()


@pytest.fixture
def auditor_api(base_url, platform):
    api = AuditorAPI(base_url, AUDITOR_TOKEN)
    yield api
    api.close()


AGREE = [
    IndependentAssessment(stance="supports", notes="Fits the guidance."),
    AuditorVerdict(verdict="agree", reason="Consistent with guidance and history."),
]
FLAG = [
    IndependentAssessment(stance="concerned", notes="Story does not fit the guidance."),
    AuditorVerdict(verdict="flag", reason="Looks like a relabelled game purchase.", concerns=["category"]),
]


@pytest.fixture
def make_agent(maker_api, auditor_api) -> Callable[..., tuple[MakerAgent, ScriptedLLM, ScriptedLLM]]:
    def build(maker_steps=(), auditor_steps=(), limits: Limits | None = None):
        maker_llm = ScriptedLLM(list(maker_steps), model="maker-model")
        auditor_llm = ScriptedLLM(list(auditor_steps), model="auditor-model")
        agent = MakerAgent(maker_llm, maker_api, Auditor(auditor_llm, auditor_api), limits)
        return agent, maker_llm, auditor_llm

    return build


@pytest.fixture
def add_request(platform) -> Callable[..., DependentRequest]:
    def add(rid: str = "r1", message: str = "Can I have money for school notebooks?", amount: float | None = 10,
            **kw) -> DependentRequest:
        from agent_layer.money import to_base

        req = DependentRequest(
            id=rid, dependent_id="d1", message=message,
            amount_requested=to_base(amount) if amount is not None else None, **kw,
        )
        platform.add_request(req)
        return req

    return add
