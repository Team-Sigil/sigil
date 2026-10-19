"""Tool definitions sent to the model, generated from the Pydantic argument models so
the schema the model sees and the validator that checks it can never drift apart."""

from __future__ import annotations

from agent_layer.guardrails.tools import TOOL_ARGS, TOOLS_BY_MODE, Mode

DESCRIPTIONS = {
    "request_transfer": (
        "Ask the platform to send tokens to the dependent's registered wallet. There is no "
        "recipient field. Limits are checked by code; larger or unusual transfers are "
        "reviewed by the Auditor first. At most one per request. At or above the proof "
        "threshold, fill proof_findings from the proof itself; code refuses the transfer if "
        "it doesn't match the claim or its total is lower than the amount."
    ),
    "ask_for_proof": "Ask the dependent for a receipt, quote or photo before deciding.",
    "ask_guardian": (
        "Ask the guardian a question with your recommendation. The guardian answers by "
        "wallet signature later. At most one per request."
    ),
    "propose_policy_update": (
        "Propose a change to the guardian's policy, with evidence. It only takes effect if "
        "the guardian signs it."
    ),
    "notify_guardian": "Send the guardian a short informational note (no decision needed).",
    "finish": "End this request. Call exactly once, last.",
}


def tool_definitions(mode: Mode) -> list[dict]:
    tools = []
    for name in sorted(TOOLS_BY_MODE[mode]):  # deterministic order keeps the prompt cache stable
        schema = TOOL_ARGS[name].model_json_schema()
        schema.pop("title", None)
        tools.append({"name": name, "description": DESCRIPTIONS[name], "input_schema": schema})
    return tools
