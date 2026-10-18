from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GuardrailLog:
    """Collects every guardrail that fired during one run, for the decision record."""

    events: list[str] = field(default_factory=list)

    def add(self, code: str, detail: str = "") -> None:
        self.events.append(f"{code}: {detail}" if detail else code)

    def codes(self) -> list[str]:
        return [e.split(":", 1)[0] for e in self.events]
