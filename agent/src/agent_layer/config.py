"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return raw.strip().lower() in ("1", "true", "yes", "on") if raw else default


@dataclass(frozen=True)
class Limits:
    """Guardrail limits. Defaults are deliberately tight; raise them per deployment."""

    max_message_chars: int = 2_000
    max_proofs_per_request: int = 3
    max_proof_bytes: int = 5 * 1024 * 1024
    max_proof_text_chars: int = 4_000
    max_thread_messages: int = 20
    max_loop_turns: int = 8
    max_tool_calls_per_run: int = 10
    max_transfers_per_run: int = 1
    max_guardian_questions_per_run: int = 1
    max_reply_chars: int = 600
    # An off-topic reply only needs to redirect; longer means the agent is answering it.
    # Measured with Han characters counting 3 (about 65 Chinese characters).
    max_out_of_scope_reply_chars: int = 200
    max_summary_chars: int = 2_500
    # Platform-sourced text that reaches a prompt (zero trust: capped like user input).
    max_guidance_chars: int = 4_000
    max_name_chars: int = 80
    max_purpose_chars: int = 200
    max_history_items: int = 15
    max_guardian_answer_chars: int = 1_000
    max_event_chars: int = 300
    max_platform_reason_chars: int = 300
    # Only verified in-app captures (source "photo"), and text read from them, count as
    # proof. Turn on once the app's camera flow ships; the engine must use the same rule.
    camera_only_proofs: bool = False
    # Wall-clock limit for one run, below the gateway's 180 s timeout. Each model call gets
    # the time left as its timeout; when it runs out, the run ends in a safe fallback.
    max_run_seconds: float = 150.0
    # Fraction of the weekly cap used as the review threshold when the guardian set none.
    default_review_fraction: float = 0.25


Provider = Literal["anthropic", "openrouter"]
PROVIDERS: tuple[Provider, ...] = ("anthropic", "openrouter")
DEFAULT_MODELS: dict[Provider, str] = {
    "anthropic": "claude-opus-5-5",
    "openrouter": "nvidia/nemotron-3-super-120b-a12b",
}


def _provider(name: str, default: Provider) -> Provider:
    raw = (os.environ.get(name) or default).strip().lower()
    if raw not in PROVIDERS:
        raise ValueError(f"{name} must be one of {', '.join(PROVIDERS)}, not {raw!r}")
    return raw  # type: ignore[return-value]


@dataclass(frozen=True)
class Settings:
    platform_base_url: str
    maker_token: str
    auditor_token: str
    maker_model: str = "claude-opus-5-5"
    auditor_model: str = "claude-opus-5-5"
    maker_effort: str = "high"
    auditor_effort: str = "high"
    max_tokens: int = 16_000
    inbound_token: str = ""
    limits: Limits = field(default_factory=Limits)
    # Which model API each agent uses; switchable per agent.
    maker_provider: Provider = "anthropic"
    auditor_provider: Provider = "anthropic"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Whether the OpenRouter model can read images. Off by default: the default model
    # (Nemotron 3 Super) is text-only, and a model that can't see a receipt mustn't vouch for it.
    openrouter_vision: bool = False
    # Whether the OpenRouter model can read PDFs (signed shop receipts). Off by default.
    openrouter_documents: bool = False

    @property
    def providers(self) -> set[Provider]:
        return {self.maker_provider, self.auditor_provider}

    @classmethod
    def from_env(cls) -> "Settings":
        limits = Limits(
            max_loop_turns=_env_int("AGENT_MAX_LOOP_TURNS", Limits.max_loop_turns),
            max_message_chars=_env_int("AGENT_MAX_MESSAGE_CHARS", Limits.max_message_chars),
            camera_only_proofs=_env_bool("AGENT_CAMERA_ONLY_PROOFS", Limits.camera_only_proofs),
            max_run_seconds=float(os.environ.get("AGENT_MAX_RUN_SECONDS") or Limits.max_run_seconds),
        )
        both = _provider("AGENT_LLM_PROVIDER", "anthropic")
        maker_provider = _provider("AGENT_MAKER_PROVIDER", both)
        auditor_provider = _provider("AGENT_AUDITOR_PROVIDER", both)
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        if "openrouter" in (maker_provider, auditor_provider) and not openrouter_key:
            raise ValueError("OPENROUTER_API_KEY is required when a provider is openrouter")
        return cls(
            platform_base_url=os.environ["PLATFORM_API_URL"],
            maker_token=os.environ["PLATFORM_MAKER_TOKEN"],
            auditor_token=os.environ["PLATFORM_AUDITOR_TOKEN"],
            maker_model=os.environ.get("AGENT_MAKER_MODEL") or DEFAULT_MODELS[maker_provider],
            auditor_model=os.environ.get("AGENT_AUDITOR_MODEL") or DEFAULT_MODELS[auditor_provider],
            maker_provider=maker_provider,
            auditor_provider=auditor_provider,
            openrouter_api_key=openrouter_key,
            openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
            openrouter_vision=_env_bool("AGENT_OPENROUTER_VISION", False),
            openrouter_documents=_env_bool("AGENT_OPENROUTER_DOCUMENTS", False),
            maker_effort=os.environ.get("AGENT_MAKER_EFFORT", "high"),
            auditor_effort=os.environ.get("AGENT_AUDITOR_EFFORT", "high"),
            inbound_token=os.environ.get("AGENT_INBOUND_TOKEN", ""),
            limits=limits,
        )
