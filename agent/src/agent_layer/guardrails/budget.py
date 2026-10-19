"""Per-family LLM token budget, kept by the platform (the agent has no storage)."""

from __future__ import annotations

from agent_layer.contracts import UsageReport
from agent_layer.guardrails.events import GuardrailLog
from agent_layer.llm import LLMResponse
from agent_layer.platform_api import MakerAPI


class BudgetExhausted(RuntimeError):
    pass


class TokenBudget:
    def __init__(self, api: MakerAPI, family_id: str, log: GuardrailLog):
        self._api = api
        self._family_id = family_id
        self._log = log

    def ensure_available(self) -> None:
        if self._api.get_usage(self._family_id).remaining_tokens <= 0:
            self._log.add("token_budget_exhausted", self._family_id)
            raise BudgetExhausted(self._family_id)

    def record(self, resp: LLMResponse) -> None:
        self._api.report_usage(
            UsageReport(
                family_id=self._family_id,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
            )
        )
