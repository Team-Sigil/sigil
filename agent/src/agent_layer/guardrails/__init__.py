"""Guardrails around the model, applied by code at every step of a run.

* ``input``   – sanitise and delimit untrusted text before the model sees it, and
  detect injection signals (which force an Auditor review; they never block).
* ``tools``   – validate every tool call and enforce per-run limits and soft policy
  pre-checks before anything reaches the platform API.
* ``output``  – check what the agent says to the dependent and reconcile the model's
  claimed outcome with what actually happened.
* ``budget``  – the per-family LLM token budget.

The engine, signer and chain remain the authoritative limits; these guardrails make
the agent fail early, safely and explainably.
"""

from agent_layer.guardrails.events import GuardrailLog

__all__ = ["GuardrailLog"]
