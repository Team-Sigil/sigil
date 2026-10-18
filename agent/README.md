# Agent Layer

The agentic AI layer of the Sigil platform: the **maker agent** that runs a dependent's
allowance, the independent **Auditor agent** that checks larger or riskier transfers, and
the **scheduled triggers** that wake them. See
[the system architecture](<Delegated Financial Guardianship — System Architecture.md>).

## Isolation: everything goes through the platform API

The agent zone has no database, no file access and no keys. Every read (policy, budget,
history, requests, proofs) and every action (transfer, guardian question, notification,
decision log) is an HTTP call to the platform API in
[`platform_api.py`](src/agent_layer/platform_api.py). The engine decides; the agent asks.

| Layer | How it is enforced | Tested in |
|---|---|---|
| Static | No DB drivers, key libraries, subprocess, file or socket APIs in the package; only `platform_api.py` (and `llm.py` for Claude) do network I/O | `tests/test_isolation.py` |
| Runtime | [`sandbox.py`](src/agent_layer/sandbox.py) installs a CPython audit hook: no file writes, reads only under code roots, no subprocess/ctypes/SQLite, no DB-driver imports, DNS and connections only to allowlisted hosts | `tests/test_isolation.py` (full request run inside the sandbox) |
| Credentials | The maker and the Auditor use separate platform tokens; only the Auditor's token can post a verdict | `tests/test_auditor.py` |

Production should add container-level isolation too (read-only filesystem, egress
firewall, no DB credentials in the agent's environment).

## Guardrails

| Stage | Guardrail | Where |
|---|---|---|
| Input | Unicode normalisation, hidden/zero-width characters stripped, length caps, thread and proof caps | `guardrails/input.py`, `maker/context.py` |
| Input | Dependent text and proofs wrapped in `<untrusted_…>` tags that cannot be closed from inside (HTML-escaped) | `guardrails/input.py` |
| Input | Injection signals (instruction override, claimed guardian approval, role play, authority claims, urgency, wallet addresses) are shown to the model and **force an Auditor review**; they never block on their own | `guardrails/input.py`, `auditor/triggers.py` |
| Tools | Allowlisted tools per mode; strict Pydantic arguments (`extra="forbid"`, so no recipient can be added); display amounts with max 2 decimals converted to integer base units | `guardrails/tools.py`, `contracts.py` |
| Tools | Pre-checks before any API call: frozen, blocked category, per-request max, weekly budget, reservoir, not more than requested, proof threshold, allowance ceiling (1.5× usual) | `guardrails/tools.py` |
| Tools | Per-run limits: 1 transfer, 1 guardian question, 10 tool calls, 8 model turns | `config.py` |
| Tools | Idempotency key derived by code from request state (`req:{id}:{thread length}`, `allowance:{dep}:{iso week}`), never chosen by the model | `maker/agent.py` |
| Auditor | Required when trusted triggers fire (amount ≥ review threshold, emergency, off-pattern amount, new category, allowance above usual) or agent triggers fire (low category confidence, injection signals) | `auditor/triggers.py` |
| Auditor | Two phases: an independent view of the raw case **before** reading the maker's reasoning, then a verdict. A "concerned" view cannot flip to "agree". Fails closed: any error means a flag. A flag means no money moves, and the guardian is asked with both views | `auditor/auditor.py` |
| Output | Reply to the dependent is replaced if it leaks the guardian's guidance (7-word overlap), a wallet address or anything that looks like a secret; capped in length | `guardrails/output.py` |
| Output | The model's claimed outcome is reconciled with what actually happened (e.g. "sent" with no transfer becomes "declined"), and the reply is replaced when corrected | `guardrails/output.py` |
| Run | Model outage, refusal, `max_tokens`, missing `finish`, loop limit or exhausted family token budget all end in a **safe fallback**: no further money moves and the guardian is asked | `maker/agent.py`, `guardrails/budget.py` |

The engine, signer and chain remain the authoritative limits. These guardrails make the
agent fail early, safely and explainably.

## Layout

```
src/agent_layer/
  contracts.py      platform API payloads, tool arguments, Auditor outputs
  platform_api.py   MakerAPI / AuditorAPI: the only way out of the agent zone
  llm.py            LLM port + AnthropicLLM (Claude Opus 5.5, adaptive thinking, refusal fallbacks)
  guardrails/       input, tools, output, budget
  maker/            system prompt, tool schemas, context builder, the agent loop
  auditor/          prompts, review triggers, the two-phase Auditor
  flows/            scheduled actions (pace, reservoir, summary, learning) and policy drafting
  scheduler.py      APScheduler jobs
  service.py        inbound HTTP API for the gateway
  sandbox.py        runtime isolation (audit hook)
mock_platform/      in-memory mock of the platform API (engine semantics), served over HTTP
tests/              unit, end-to-end, attack, isolation and adapter tests
```

## Platform API contract (consumed by the agent)

All under `/agent/v1`, bearer-authenticated. Money is integer base units (6 decimals).

| Method | Path | Token |
|---|---|---|
| GET | `/dependents`, `/dependents/{id}/context`, `/dependents/{id}/weekly-activity` | maker |
| GET | `/requests/{id}`, `/proofs/{id}`, `/guardians/{id}/answers`, `/usage/{family}` | maker |
| POST | `/transfers` (with `Idempotency-Key` header; no recipient field) | maker |
| POST | `/guardian-questions`, `/proof-requests`, `/policy-proposals`, `/notifications` (deduped by `dedupe_key`) | maker |
| POST | `/decisions`, `/events`, `/usage`, `/policy-drafts/validate` | maker |
| POST | `/auditor/verdicts` | **auditor only** |

The engine must refuse a transfer that needs review (`auditor.triggers.trusted_triggers`)
unless it carries an unused `agree` verdict bound to the same idempotency key, amount and
category. `mock_platform/app.py` implements this as a reference.

## Inbound API (called by the gateway)

| Method | Path | |
|---|---|---|
| POST | `/v1/requests/{request_id}/process` | Run the maker on a dependent's request; returns the decision |
| POST | `/v1/policy-drafts` | Draft a policy from the guardian's chat (the guardian signs; the agent never activates) |
| POST | `/v1/jobs/{allowance\|pace\|reservoir\|weekly_summary\|learning}` | Run a scheduled job now |

All require `Authorization: Bearer $AGENT_INBOUND_TOKEN`; with no token configured, every
call is refused.

## Running

```bash
pip install -e ".[dev]"
pytest                      # full suite, no network or API key needed
pytest -m live              # also hit the real Claude API (needs ANTHROPIC_API_KEY; costs money)

python -m mock_platform.server          # mock platform with a demo family on :8200
PLATFORM_API_URL=http://127.0.0.1:8200 \
PLATFORM_MAKER_TOKEN=maker-test-token PLATFORM_AUDITOR_TOKEN=auditor-test-token \
AGENT_INBOUND_TOKEN=change-me python -m agent_layer   # agent service on :8100
```

Optional settings: `AGENT_MAKER_MODEL`, `AGENT_AUDITOR_MODEL` (both default to
`claude-opus-5-5`), `AGENT_MAKER_EFFORT`, `AGENT_AUDITOR_EFFORT`, `AGENT_SCHEDULER=0`.
