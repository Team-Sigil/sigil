# Agent Layer

The agentic AI layer of the Sigil platform: the **maker agent** that runs a dependent's
allowance, the independent **Auditor agent** that checks larger or riskier transfers, and
the **scheduled triggers** that wake them. See
[the system architecture](<Delegated Financial Guardianship — System Architecture.md>).

## The agents: responsibilities, inputs, outputs

### Maker ([`maker/agent.py`](src/agent_layer/maker/agent.py))

Decides what happens to each request and runs the allowance. It never holds keys,
chooses a recipient, changes policy or writes to a database.

| Responsibility | Entry point | Uses the model? |
|---|---|---|
| Judge a dependent's request: send full or partial, counter-offer, ask for proof, ask the guardian, or decline | `MakerAgent.handle_request` | Yes |
| Send the weekly allowance, adjusted within the guidance | `MakerAgent.run_allowance` | Yes |
| Draft a policy from the guardian's chat, with test scenarios and open questions | `flows/policy.py: draft_policy` | Yes |
| Write the weekly summary | `flows/scheduled.py: weekly_summary` | Wording only; pattern flags are code |
| Warn the dependent when spending runs fast | `flows/scheduled.py: pace_check` | No |
| Ask the guardian for a top-up before the reservoir runs dry | `flows/scheduled.py: reservoir_check` | No |
| Propose "stop asking" policy updates from the guardian's answers | `flows/scheduled.py: learning_proposals` | No |

**Inputs**, all read through `MakerAPI`:

| Input | Platform call |
|---|---|
| The request: the dependent's words, amount asked, amount already sent, emergency flag, conversation so far, verified guardian answers | `GET /requests/{id}` |
| Proof: images go to the model through vision, text goes as delimited data | `GET /proofs/{id}` |
| Signed policy view (outer limits and private guidance), week's budget, reservoir, freeze flag, recent history | `GET /dependents/{id}/context` |
| Guardian's past answers (for learning) and the week's activity (for the summary) | `GET /guardians/{id}/answers`, `GET /dependents/{id}/weekly-activity` |
| The guardian's chat (for policy drafting) | Inbound `POST /v1/policy-drafts` |
| Wake-ups | Scheduler, or inbound `POST /v1/jobs/{name}` |

**Outputs.** The model can call only these tools; code validates each call, then turns it into a platform call:

| Tool | Becomes | Notes |
|---|---|---|
| `request_transfer(amount, purpose, category, category_confidence, reasoning)` | `POST /transfers` | No recipient field; code adds the idempotency key and, when review is required, the Auditor's verdict id |
| `ask_for_proof(kind, reason)` | `POST /proof-requests` | Request mode only |
| `ask_guardian(question, recommendation, recommended_amount, urgency)` | `POST /guardian-questions` | At most one per run |
| `propose_policy_update(change, evidence)` | `POST /policy-proposals` | Takes effect only if the guardian signs it |
| `notify_guardian(message)` | `POST /notifications` | Informational note, filed under the dependent (`dependent_id`) |
| `finish(outcome, reply_to_dependent, summary_for_guardian)` | `POST /decisions` | Ends every run. Code corrects the outcome if it doesn't match what happened, and checks the reply before recording it |

Code-only outputs: pace warnings and the weekly summary (`POST /notifications`),
top-up questions (`POST /guardian-questions`), and guardrail events (`POST /events`).

### Auditor ([`auditor/auditor.py`](src/agent_layer/auditor/auditor.py))

The guardian's advocate. It reviews a transfer the maker wants to make when a trigger in
[`auditor/triggers.py`](src/agent_layer/auditor/triggers.py) fires, before the engine
sees it. It can only make an outcome stricter: it never approves, raises an amount,
talks to the dependent or calls the engine.

| | |
|---|---|
| **Triggers** | Engine-verifiable: amount ≥ review threshold, emergency claimed, amount over 2× the usual, new category, allowance above usual. Agent-side (can only add reviews): category confidence < 0.7, manipulation signals in the input |
| **Inputs, phase 1** | Read by the Auditor itself with its own token: the policy, guidance, budget and recent history (`GET /dependents/{id}/context`). From the maker's run: the dependent's words, proof (text and images) and why it is reviewing. Not the maker's reasoning |
| **Inputs, phase 2** | The maker's amount, category, confidence, purpose and reasoning |
| **Output** | `agree`, or `flag` with a reason and concerns, posted with the Auditor's own token to `POST /auditor/verdicts` and bound to the transfer's idempotency key, amount and category |
| **On a flag** | No money moves; the maker's code asks the guardian with both views (`POST /guardian-questions`) |
| **Context check** | Before calling the model, it compares its own read with the maker's snapshot: a different policy (version, hash or content), another dependent or guardian, or a freeze during the run is flagged at once (`auditor_context_mismatch`) |
| **On failure** | Any error, refusal, exhausted budget, unreadable context or unrecordable verdict counts as a flag |

### Where the data lives

The agent layer stores nothing. Each run reads a snapshot from the platform API; the
Auditor reads its own copy of the context. In this repo the platform is the in-memory
mock, so all state is lost on restart.

```mermaid
flowchart LR
    GW["Gateway / your app"]

    subgraph AZ["Agent zone: stateless, no storage"]
        direction TB
        SVC["Agent service<br/>POST /v1/requests/{id}/process"]
        MK["Maker agent"]
        SNAP["AgentContext snapshot<br/>(in memory, one per run)"]
        AU["Auditor agent"]
        SVC --> MK
        MK --> SNAP
        SNAP -. "checked against the Auditor's own read" .-> AU
    end

    subgraph PL["Mock platform: in-memory, lost on restart"]
        direction TB
        POL["dependents[d].policy<br/>limits, guidance, version"]
        BUD["dependents[d].spent_this_week<br/>delegated[g]"]
        HIS["dependents[d].history[]<br/>transfer / blocked only"]
        REQ["requests{} + proofs{}<br/>message, thread, already_sent"]
        TX["transfers{}<br/>keyed by idempotency key"]
        VER["verdicts{}"]
        DEC["decisions[]"]
        DEC -. "out_of_scope only (mock)" .-> HIS
    end

    GW -->|"process request"| SVC
    MK -->|"GET /context"| POL
    MK -->|"GET /context"| BUD
    MK -->|"GET /context"| HIS
    MK -->|"GET /requests, /proofs"| REQ
    MK -->|"POST /transfers"| TX
    AU -->|"GET /context<br/>(auditor token)"| POL
    AU -->|"POST /auditor/verdicts<br/>(auditor token)"| VER
    MK -->|"POST /decisions"| DEC
    TX -->|"engine updates on send"| BUD
    TX -->|"engine appends"| HIS
    TX -->|"engine bumps already_sent"| REQ
```

### One request, from start to finish

The snapshot is read once at the start. The Auditor reads the context again for itself,
and the engine re-checks every transfer against its *current* state, which is what
catches a freeze or policy change mid-run.

```mermaid
sequenceDiagram
    autonumber
    participant GW as Gateway
    participant MK as Maker (agent service)
    participant LLM as Claude
    participant AU as Auditor
    participant PL as Platform API (mock engine)

    GW->>MK: POST /v1/requests/r1/process
    MK->>PL: GET /requests/r1
    PL-->>MK: message, amount_requested, already_sent, thread, proof refs
    MK->>PL: GET /dependents/d1/context
    PL-->>MK: policy, budget, reservoir, frozen, history
    MK->>PL: GET /proofs/{id} (max 3)
    PL-->>MK: receipt text / image

    Note over MK: Snapshot is read once.<br/>Input sanitised and delimited.

    MK->>LLM: context + request + proofs
    LLM-->>MK: request_transfer(amount, category, reasoning)
    Note over MK: ToolGuard pre-checks against the snapshot<br/>(limits, budget, reservoir, proof, over-requested)

    alt review triggers fire
        MK->>AU: ReviewCase (request, proofs, maker's decision)
        AU->>PL: GET /dependents/d1/context (auditor token)
        PL-->>AU: policy, guidance, budget, history
        Note over AU: Policy differs from the maker's,<br/>or frozen: flag, no model call
        AU->>LLM: phase 1: raw case, no maker reasoning
        LLM-->>AU: independent assessment
        AU->>LLM: phase 2: maker's decision
        LLM-->>AU: agree / flag
        AU->>PL: POST /auditor/verdicts (auditor token)
        PL-->>AU: verdict_id
        AU-->>MK: verdict
    end

    alt Auditor flagged
        MK->>PL: POST /guardian-questions (both views)
        Note over MK,PL: No money moves
    else cleared, or no review needed
        MK->>PL: POST /transfers (idempotency key, verdict_id)
        Note over PL: Re-checks the CURRENT state:<br/>version, freeze, limits, budget,<br/>reservoir, verdict binding
        PL->>PL: update spent, delegation, history,<br/>already_sent, store by key
        PL-->>MK: sent / duplicate / blocked
    end

    MK->>LLM: tool_result
    LLM-->>MK: finish(outcome, reply)
    Note over MK: Outcome reconciled with facts.<br/>Reply checked for leaks.
    MK->>PL: POST /decisions (the gateway links it to the transfer)
    MK-->>GW: DecisionRecord
```

### Known gaps

1. **The Auditor still gets the request and proofs from the maker.** It reads the policy,
   guidance, budget and history itself and flags any mismatch with the maker's snapshot,
   but the dependent's words and the proofs come from the maker's run, so a compromised
   maker could still reshape the story the Auditor judges. The engine's limits and proof
   rules hold either way. Fix: the platform lets the Auditor's token read
   `GET /requests/{id}` and `GET /proofs/{id}` (asked in
   [gateway-handoff.md](gateway-handoff.md)).
2. **The mock platform is simpler than the gateway.** The gateway builds history from the
   decision log, puts signed guardian answers in the thread, links decisions to
   transfers, normalises proof images and sets their integrity flags, enforces a daily
   message quota and supports several dependents per guardian. The mock does none of
   this (except history for `out_of_scope`), so those paths are tested in the gateway,
   not here.
3. **Proof images are re-sent and re-billed on every model turn.** Only the system
   prompt is cached. A cache breakpoint on the first user turn would make re-sends
   cache reads. See [proof-images.md](proof-images.md).
4. **Proof forgery is limited, not solved.** The agent and engine catch mis-paired,
   typed, tampered and reused proofs, and the gateway flags near-duplicates and
   suspicious metadata, but a convincing fake from a new shop still gets through, up to
   `max_per_request`. See [proof-images.md](proof-images.md#proof-integrity).
5. **The inbound API has no body size limit of its own.** Run the agent service only
   behind the gateway, which limits bodies and rates; don't expose it directly.

### Differences from the architecture doc

- The model ends every run with `finish` (explicit outcome plus reply), so decisions are structured and loggable.
- `notify` is narrowed to `notify_guardian`; the dependent's reply goes through `finish`, and pace warnings are code.
- Pace, reservoir, pattern flags and learning proposals are deterministic code, not model calls.
- The Auditor also sees the proof, and manipulation signals in the input force a review.
- Two more outcomes: `asked_dependent` (a clarifying question) and `out_of_scope` (not
  about the allowance). A request with no stated amount can't move money.

## Isolation: everything goes through the platform API

The agent zone has no database, no file access and no keys. Every read (policy, budget,
history, requests, proofs) and every action (transfer, guardian question, notification,
decision log) is an HTTP call to the platform API in
[`platform_api.py`](src/agent_layer/platform_api.py). The engine decides; the agent asks.

| Layer | How it is enforced | Tested in |
|---|---|---|
| Static | No DB drivers, key libraries, subprocess, file or socket APIs in the package; only `platform_api.py` (and `llm.py` for Claude) do network I/O | `tests/test_isolation.py` |
| Runtime | [`sandbox.py`](src/agent_layer/sandbox.py) installs a CPython audit hook: no file writes, reads only under code roots, no subprocess/ctypes/SQLite, no DB-driver imports, DNS and connections only to allowlisted hosts | `tests/test_isolation.py` (full request run inside the sandbox) |
| Credentials | The maker and the Auditor use separate platform tokens. The Auditor's token can only read a dependent's context and post a verdict; only it can post one | `tests/test_auditor.py` |

Production should add container-level isolation too (read-only filesystem, egress
firewall, no DB credentials in the agent's environment).

## Guardrails

| Stage | Guardrail | Where |
|---|---|---|
| Input | Unicode normalisation, hidden/zero-width characters stripped, length caps, thread and proof caps | `guardrails/input.py`, `maker/context.py` |
| Input | Dependent text and proofs wrapped in `<untrusted_…>` tags that cannot be closed from inside (HTML-escaped) | `guardrails/input.py` |
| Input | Injection signals in English and Chinese (instruction override, claimed guardian approval, role play, authority claims, urgency, wallet addresses) are shown to the model and **force an Auditor review**; they never block on their own | `guardrails/input.py`, `auditor/triggers.py` |
| Platform | Responses over 8 MB, invalid JSON or outside the contract (hard length ceilings on every text field and list) raise `PlatformAPIError`: the run fails closed | `platform_api.py`, `contracts.py` |
| Platform | Zero trust: platform text in a prompt (guidance, names, history purposes, verified guardian answers, events, engine reasons, and the maker's reasoning shown to the Auditor) is capped, stripped and escaped like user input; inline fields are collapsed to one line | `guardrails/input.py` (`PlatformText`), `maker/context.py` |
| Platform | A trusted channel that needed fixing (hidden characters, oversized, markup), or a proof whose bytes don't match its declared size, adds `platform_text_anomaly` and **forces an Auditor review** | `guardrails/input.py`, `maker/context.py` |
| Tools | Allowlisted tools per mode; strict Pydantic arguments (`extra="forbid"`, so no recipient can be added); display amounts with max 2 decimals converted to integer base units | `guardrails/tools.py`, `contracts.py` |
| Tools | Pre-checks before any API call: frozen, blocked category, per-request max, weekly budget, reservoir, **an amount stated in `amount_requested`** (never guessed from the chat), not more than requested (counting pending money), proof threshold, allowance ceiling (the signed amount: lowering is fine, raising needs the guardian). A signed guardian approval (`guardian_approved_amount`) lets exactly that amount past the per-request max and weekly cap without review, like the engine; nothing else is lifted | `guardrails/tools.py` |
| Proofs | A proof counts toward the threshold only if it belongs to this request, its bytes match the platform's hash, it is an image or platform-extracted (`ocr`) text, and it isn't a duplicate or AI-generated (with `AGENT_CAMERA_ONLY_PROOFS`, it must also be a verified in-app capture). Above the threshold the model must report `proof_findings` (merchant, date, total, receipt number, matches claim); a mismatch or a total below the amount is refused. Platform flags (near-duplicate, screenshot, old, edited, AI-suspected, re-photographed screen) force an Auditor review | `maker/context.py`, `guardrails/tools.py` |
| Tools | Per-run limits: 1 transfer, 1 guardian question, 10 tool calls, 8 model turns | `config.py` |
| Tools | Idempotency key derived by code from request state (`req:{id}:{thread length}`, `allowance:{dep}:{iso week}`), never chosen by the model | `maker/agent.py` |
| Auditor | Required when trusted triggers fire (amount ≥ review threshold, emergency, off-pattern amount, new category, allowance above usual) or agent triggers fire (low category confidence, injection signals) | `auditor/triggers.py` |
| Auditor | Two phases: an independent view of the raw case **before** reading the maker's reasoning, then a verdict. A "concerned" view cannot flip to "agree". Fails closed: any error means a flag. A flag means no money moves, and the guardian is asked with both views | `auditor/auditor.py` |
| Output | Reply to the dependent is replaced, in the dependent's language, if it leaks the guardian's guidance (7 English words or 10 Chinese characters in a row), a wallet address or anything that looks like a secret; capped in length at a word or clause break | `guardrails/output.py` |
| Output | The model's claimed outcome is reconciled with what actually happened (e.g. "sent" with no transfer becomes "declined", "out_of_scope" after asking the guardian becomes "asked_guardian"), and the reply is replaced when corrected | `guardrails/output.py` |
| Scope | Off-topic messages ("what is 1+1?", homework, chat) end as `out_of_scope` with a short redirect; a reply over 200 (Han characters count 3) means the model answered the question, so it is replaced. Unclear requests end as `asked_dependent`. Three or more off-topic messages in a week are flagged in the guardian's summary | `maker/prompts.py`, `maker/agent.py`, `flows/scheduled.py` |
| Run | Model outage, refusal, `max_tokens`, missing `finish`, loop limit, exhausted family token budget or the **run deadline** (150 s; each model call gets the time left) all end in a **safe fallback**: no further money moves and the guardian is asked. A transfer the platform accepted but hasn't confirmed ends as `transfer_pending`, never "declined" | `maker/agent.py`, `guardrails/budget.py`, `llm.py` |

The engine, signer and chain remain the authoritative limits. These guardrails make the
agent fail early, safely and explainably.

## Languages

The agent supports English, Simplified Chinese and Traditional Chinese
([`i18n.py`](src/agent_layer/i18n.py)). Each run tells the model which language to
write in for each reader, and every fixed text code sends to a person exists in all
three.

| Reader | Language |
|---|---|
| Dependent | The language and script of their latest message; if too short to tell (`"10?"`), their profile locale; otherwise English |
| Guardian (summaries, questions, notes, Auditor reasons) | Their profile locale; otherwise the language their guidance is written in; otherwise English |

Profile locales come from the platform as optional BCP 47 tags on the context
(`dependent_locale`, `guardian_locale`: `en`, `zh-CN`, `zh-TW`, `zh-HK`, …).
Unsupported tags are ignored. Purposes, reasoning and guardrail events stay in whatever
language the model or code wrote them in; they are not shown to people as-is.

The Chinese fixed texts and injection patterns should be reviewed by a native speaker
of each script before launch. Adding a language means a column in `i18n.py`, patterns in
`guardrails/input.py`, and a script rule in `detect_locale`.

## Layout

```
src/agent_layer/
  contracts.py      platform API payloads, tool arguments, Auditor outputs
  platform_api.py   MakerAPI / AuditorAPI: the only way out of the agent zone
  llm.py            LLM port + AnthropicLLM (Claude Opus 5.5, adaptive thinking, refusal fallbacks)
  i18n.py           languages: who gets which, and every fixed text in en / zh-Hans / zh-Hant
  guardrails/       input, tools, output, budget
  maker/            system prompt, tool schemas, context builder, the agent loop
  auditor/          prompts, review triggers, the two-phase Auditor
  flows/            scheduled actions (pace, reservoir, summary, learning), policy drafting and the policy interview
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
| GET | `/dependents/{id}/context` | maker, **Auditor** (read-only) |
| GET | `/dependents`, `/dependents/{id}/weekly-activity` | maker |
| GET | `/requests/{id}`, `/proofs/{id}`, `/guardians/{id}/answers`, `/usage/{family}` | maker |
| POST | `/transfers` (with `Idempotency-Key` header; no recipient field) | maker |
| POST | `/guardian-questions`, `/proof-requests`, `/policy-proposals`, `/notifications` (deduped by `dedupe_key`; a guardian note may name the `dependent_id` it's about, which must be one of that guardian's dependents) | maker |
| POST | `/decisions`, `/events`, `/usage`, `/policy-drafts/validate` | maker |
| POST | `/auditor/verdicts` | **auditor only** |

The engine must refuse a transfer that needs review (`auditor.triggers.trusted_triggers`)
unless it carries an unused `agree` verdict bound to the same idempotency key, amount and
category. `mock_platform/app.py` implements this as a reference.

Money the engine has accepted but the chain hasn't confirmed is `pending`: the transfer
result says so (`status: "pending"`), the request carries it as `already_pending`, and it
counts against `amount_requested` exactly like `already_sent`, because it may still go
out. Only the engine finishes a pending transfer, by its own idempotency key. A request
the guardian approved by signature carries `guardian_approved_amount`. What the gateway
needs to change for these: [gateway-handoff.md](gateway-handoff.md).

Responses must stay within the contract, or the agent fails closed (`PlatformAPIError`,
so `502` to the gateway, and no model call):

- A response body of at most ~8 MB (one proof at the 5 MB limit, base64-encoded).
- Valid JSON that matches `contracts.py`, including its hard length ceilings
  (`MAX_TEXT`, `MAX_SHORT_TEXT`, `MAX_GUIDANCE`, `MAX_ITEMS`, `MAX_PROOF_B64`).
- For an image proof, `size_bytes` equal to the decoded length of `data_b64`. A mismatch
  skips the proof and forces an Auditor review.

Proofs carry integrity fields (`request_id`, `dependent_id`, `uploaded_at`, `sha256`,
`source`, `flags`), and transfers carry `proof_ids` and `proof_findings`. Above the
proof threshold, the engine must require an unused proof for that request and mark it
used in the same transaction as the transfer, so one receipt backs one transfer.
`mock_platform/app.py` implements this as a reference. Requirements, upload rules and
open questions: [proof-images.md](proof-images.md).

## Inbound API (called by the gateway)

| Method | Path | |
|---|---|---|
| POST | `/v1/requests/{request_id}/process` | Run the maker on a dependent's request; returns the decision |
| POST | `/v1/policy-interviews/turn` | One turn of the policy interview: the agent's next question, the checklist and, when ready, the draft to sign. See [policy-protocol.md](policy-protocol.md) |
| GET | `/v1/policy-interviews/protocol` | The interview's checklist, phases and labels (`?locale=`), for rendering progress |
| POST | `/v1/policy-drafts` | One-shot draft from the guardian's chat, no questions (kept for compatibility; the guardian signs; the agent never activates) |
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

Or both in containers (mock platform on :8200, agent service on :8100):

```bash
ANTHROPIC_API_KEY=sk-... docker compose up --build
```

[walkthrough.md](walkthrough.md) drives both services step by step with HTTPie. Every
command in it is also a test (`pytest tests/test_walkthrough.py`).

Optional settings: `AGENT_MAKER_MODEL`, `AGENT_AUDITOR_MODEL` (both default to
`claude-opus-5-5`), `AGENT_MAKER_EFFORT`, `AGENT_AUDITOR_EFFORT`, `AGENT_SCHEDULER=0`,
`AGENT_CAMERA_ONLY_PROOFS=1` (only verified in-app captures count as proof; turn on with
the engine's matching rule once the app's camera flow ships), `AGENT_MAX_RUN_SECONDS`
(default 150; keep it below the gateway's 180 s timeout).
