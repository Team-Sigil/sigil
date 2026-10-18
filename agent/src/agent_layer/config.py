"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


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
    # Fraction of the weekly cap used as the review threshold when the guardian set none.
    default_review_fraction: float = 0.25


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

    @classmethod
    def from_env(cls) -> "Settings":
        limits = Limits(
            max_loop_turns=_env_int("AGENT_MAX_LOOP_TURNS", Limits.max_loop_turns),
            max_message_chars=_env_int("AGENT_MAX_MESSAGE_CHARS", Limits.max_message_chars),
        )
        return cls(
            platform_base_url=os.environ["PLATFORM_API_URL"],
            maker_token=os.environ["PLATFORM_MAKER_TOKEN"],
            auditor_token=os.environ["PLATFORM_AUDITOR_TOKEN"],
            maker_model=os.environ.get("AGENT_MAKER_MODEL", "claude-opus-5-5"),
            auditor_model=os.environ.get("AGENT_AUDITOR_MODEL", "claude-opus-5-5"),
            maker_effort=os.environ.get("AGENT_MAKER_EFFORT", "high"),
            auditor_effort=os.environ.get("AGENT_AUDITOR_EFFORT", "high"),
            inbound_token=os.environ.get("AGENT_INBOUND_TOKEN", ""),
            limits=limits,
        )
