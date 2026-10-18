# SPEC — Delegated Financial Guardianship

> **Single source of truth.** Every human and AI coding agent on this project implements against this file.
> If code and SPEC disagree, SPEC wins until SPEC is changed. If SPEC is silent, do not invent: use the default in §17 or add an entry to §17 and ask a human.

- Project: FinTechathon 2026 entry (theme: AI as a Financial Participant)
- Status: v1.0 — contracts frozen for implementation
- Security weighs 30% of judging. Requirements tagged `SEC-xx` are non-negotiable.

---

## 0. Rules for AI coding agents (read first)

1. **Read this file before every task.** Re-read the sections for the module you touch.
2. **Stay in your module** (§4). Touching another module's code needs a human's OK.
3. **Contracts are frozen.** Never change an API shape, DB column, schema, enum, env var, or reason code without updating SPEC in the same PR, and flagging it in the PR title with `[SPEC]`.
4. **Never weaken a `SEC-xx` requirement**, even to make a test or demo pass. If one blocks you, stop and ask.
5. **Money is integers.** All amounts are integer base units (§6.1). No floats anywhere in money paths.
6. **The LLM is untrusted.** Never let model output choose a recipient, move money, change policy, or skip a check (§11).
7. **No secrets in code, logs, prompts or commits.** Use env vars (§13).
8. **Every change ships with tests** (§14). Security checks need a test that proves the attack fails.
9. **When unsure, prefer the stricter behavior** (deny, escalate, or refuse) and note it in the PR.
10. **Don't add dependencies** beyond §3 without noting them in the PR description.

---

## 1. Product in one paragraph

A guardian (parent, family member) funds a reservoir in their own Phantom wallet and describes, in plain language, how a dependent (student, elderly parent, anyone they support) should be helped with money. An AI **Agent** then runs the dependent's allowance day to day: it judges requests, negotiates, asks for proof, paces the budget, sends scheduled allowances, asks the guardian when needed, and learns the guardian's preferences. An independent **Auditor** agent reviews larger or riskier decisions. Deterministic code (the **Policy engine** and the **Signer**) guarantees nothing moves beyond the guardian's signed outer limits, and only to the dependent's registered address. Money moves as an SPL token on **Solana devnet** via SPL token delegation, so the app never custodies funds.

**Core security claim (the pitch):** even a fully manipulated agent cannot move more than the guardian allowed, to anyone other than the dependent.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| Guardian | The person who funds and sets intent. Authenticated by wallet. |
| Dependent | The person who receives money. Authenticated by wallet. |
| Family | One guardian + one dependent (MVP). |
| Reservoir | The guardian's SPL token account, with a delegation to the agent wallet. |
| Agent | Main LLM agent ("maker"). Decides what to do with requests. |
| Auditor | Second LLM agent ("checker"). Agrees or flags. Never approves. |
| Policy | Signed, versioned JSON: outer limits + guidance (§7). |
| Outer limits | Hard rules enforced by code: weekly cap, max per request, blocked categories, thresholds. |
| Guidance | Plain-language intent the Agent interprets. Never enforced by code. |
| Policy engine | Deterministic Python module that clears or blocks transfers (§9). |
| Signer | Separate process holding the delegate key; signs `transfer_checked` (§10). |
| Agent wallet | The delegate keypair = the agent's on-chain identity. Held only by the Signer. |
| Base units | Integer token amount; 1 aUSD = 1,000,000 base units. |
| Guardian signature | ed25519 signature from the guardian's Phantom wallet over a canonical message (§8.4). |

---

## 3. Tech stack (fixed)

| Layer | Choice |
|---|---|
| Mobile app | Native mobile app for iOS and Android (framework chosen by the app owner); receipts taken with the in-app camera and signed with an attested device key (Apple App Attest; Android key attestation + Play Integrity) |
| Wallet | Phantom mobile, **devnet only** (Phantom testnet mode) |
| Sign-in | Sign-In With Solana (Phantom `signIn`, fallback `signMessage`), JWT session |
| Backend API | Python 3.12, FastAPI, Pydantic v2, Uvicorn |
| LLM | Anthropic API (Claude), tool use; vision for receipts. Agent and Auditor are separate calls with separate prompts |
| Policy engine | Pure Python module inside the backend, no I/O except through injected repositories |
| Scheduler | APScheduler inside the backend |
| Database | PostgreSQL 16, SQLAlchemy 2 + Alembic migrations |
| Signer | Separate Python 3.12 FastAPI service; `solana` (solana-py incl. `spl.token`), `solders`, PyNaCl |
| Chain | Solana devnet; own SPL test mint **aUSD**, 6 decimals |
| Crypto utils | PyNaCl (ed25519 verify), `hmac`/`hashlib` stdlib |
| Tests | pytest, pytest-asyncio for Python; the app's own test tooling for mobile |
| Lint/format | ruff + mypy (strict) for Python; the app's own linters for mobile |
| Local dev | docker-compose: `postgres`, `migrate`, `mock-crypto`, `gateway` (gateway repo); the app runs on a device or simulator |

Pin versions to latest stable at project start in `pyproject.toml` / `package.json`; do not upgrade majors mid-hackathon.

---

## 4. Repository layout and ownership

```
/
├─ SPEC.md                 ← this file
├─ CLAUDE.md               ← imports SPEC.md for Claude Code
├─ docker-compose.yml
├─ .env.example
├─ apps/mobile/            ← Dev A
│  └─ src/{app,components,lib/api,lib/wallet}
├─ services/api/           ← FastAPI backend
│  ├─ app/main.py
│  ├─ app/gateway/         ← Dev C  auth, sessions, rate limits, routes
│  ├─ app/engine/          ← Dev C  policy engine (pure)
│  ├─ app/db/              ← Dev C  models, migrations, repositories, audit log
│  ├─ app/agents/          ← Dev B  agent, auditor, prompts, tool handlers
│  ├─ app/scheduler/       ← Dev D  jobs
│  ├─ app/chain/           ← Dev D  read-only Solana client + signer client
│  ├─ app/contracts/       ← SHARED Pydantic schemas (§6–§8). Changes = [SPEC] PR
│  └─ tests/
├─ services/signer/        ← Dev D
│  ├─ signer/main.py
│  └─ tests/
└─ scripts/                ← Dev D  mint setup, demo seed, attack demos
```

| Dev | Owns | Delivers first (so others are unblocked) |
|---|---|---|
| A | Mobile app (guardian + dependent views, Phantom wallet, signing UX, in-app receipt camera) | Screens against mocked API from `app/contracts` OpenAPI |
| B | Agent, Auditor, prompts, tool handlers | Tool handlers calling a fake engine that returns fixed decisions |
| C | Gateway, auth, engine, DB, audit log | Engine with full unit tests + DB migrations |
| D | Signer, chain client, scheduler, scripts | Signer in `MOCK_CHAIN=true` mode returning fake tx signatures |

`app/contracts/` is shared: any change needs a `[SPEC]` PR reviewed by at least one other dev.

---

## 5. Architecture and trust zones

```
Clients (untrusted) ── Guardian app, Dependent app
        │ HTTPS, JWT
API gateway ── SIWS auth, roles, rate limits, schema validation
        │
Agent zone (LLM output untrusted) ── Agent, Auditor, Scheduler
        │ tool calls only
Trusted core (deterministic, no LLM) ── Policy engine, PostgreSQL, Signer
        │ transfer_checked as delegate
Solana devnet ── guardian token account (reservoir), dependent token account
```

| Zone | Rule |
|---|---|
| Clients | Every input authenticated, rate-limited, schema-validated at the gateway. |
| Agent zone | May only call the tools in §11.2. Outputs validated like user input. No DB or network access except via tools. |
| Trusted core | No LLM calls. Enforces the guardian's **signed** policy. Default deny. |
| Chain | Token program caps total spend at the delegated amount. |

**Request path for money (always):** Agent decides → tool handler → (Auditor if required) → Policy engine → Signer (independent re-check) → Solana.

---

## 6. Core conventions

### 6.1 Money
- Token: **aUSD**, SPL, **6 decimals**. Store and pass **integer base units** (`int`, Postgres `BIGINT`).
- UI converts for display only. Field names end with `_units` (e.g. `amount_units`).
- Never use floats for money. Never round in the backend.

### 6.2 Time
- Store UTC (`timestamptz`). Each family has `timezone` (default `Asia/Bangkok`).
- **Budget week** = Monday 00:00 to next Monday 00:00 in the family timezone.

### 6.3 IDs and idempotency
- All primary keys are UUIDv4 except `audit_log.id` (bigserial).
- Every transfer request has an `idempotency_key` (string ≤ 64). Dependent requests: generated server-side per chat turn. Scheduled allowance: `allowance:{family_id}:{week_start_iso}`.

### 6.4 Canonical JSON
- Used for hashing and signing: UTF-8, keys sorted, no insignificant whitespace (`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).

### 6.5 Categories (fixed enum)
`food, transport, education, health, housing, clothing, entertainment, gaming, gambling, subscriptions, gifts, emergency, other`

---

## 7. Policy schema

`app/contracts/policy.py` — Pydantic model `PolicyBody`. Stored as `policies.body` (jsonb).

```json
{
  "schema_version": 1,
  "currency": "aUSD",
  "limits": {
    "weekly_cap_units": 100000000,
    "max_per_request_units": 40000000,
    "blocked_categories": ["gambling", "gaming"],
    "proof_threshold_units": 30000000,
    "audit_threshold_units": 20000000,
    "low_confidence_threshold": 0.6,
    "max_requests_per_day": 5
  },
  "allowance": {
    "amount_units": 50000000,
    "cadence": "weekly",
    "weekday": "mon"
  },
  "guidance": [
    "Education and study materials are always a priority.",
    "Clothes are fine occasionally; question frequent requests."
  ]
}
```

Validation rules (engine and API both enforce):
- All `_units` ≥ 0; `max_per_request_units` ≤ `weekly_cap_units`; `allowance.amount_units` ≤ `weekly_cap_units`.
- `audit_threshold_units` ≤ `max_per_request_units`; `proof_threshold_units` ≤ `max_per_request_units`.
- `low_confidence_threshold` in [0, 1].
- `cadence` ∈ `weekly | none`; `weekday` ∈ `mon..sun`.
- `guidance`: ≤ 20 items, each ≤ 300 chars, plain text.
- `blocked_categories` ⊆ §6.5.
- **Policy hash** = SHA-256 hex of canonical JSON of `body`.
- A policy is **active** only if `guardian_signature` verifies over the activation message (§8.4). Exactly one active policy per family.

---

## 8. Interfaces

### 8.1 Public REST API (gateway)

Base path `/api/v1`. Auth: `Authorization: Bearer <JWT>` except `/auth/*`. JWT claims: `sub` (user id), `wallet`, `role` (`guardian|dependent`), `family_id`, `exp` (≤ 12 h).

| Method | Path | Role | Body → Response |
|---|---|---|---|
| POST | `/auth/nonce` | — | `{wallet}` → `{nonce, message, expires_at}` |
| POST | `/auth/verify` | — | `{wallet, message, signature_b58}` → `{token, role, family_id}` |
| POST | `/families` | guardian | `{dependent_wallet, dependent_token_account, timezone, registration_signature_b58}` → `Family` |
| POST | `/guardian/chat` | guardian | `{message}` → `{reply, draft_policy_id?}` (policy setup conversation) |
| GET | `/policy/drafts/{id}` | guardian | → `{body, body_hash, summary_text, test_cases[]}` |
| POST | `/policy/drafts/{id}/activate` | guardian | `{signature_b58}` → `{policy_id, version}` |
| GET | `/policy/active` | both | → guardian: full; dependent: `limits` only (no guidance) |
| GET | `/questions` | guardian | → open `GuardianQuestion[]` |
| POST | `/questions/{id}/answer` | guardian | `{decision: approve|deny, note?, signature_b58}` → `GuardianQuestion` |
| POST | `/freeze` | guardian | `{frozen: bool, signature_b58}` → `{frozen}` |
| POST | `/dependent/chat` | dependent | `{message}` → `{reply, request_id?}` |
| POST | `/proofs` | dependent | multipart `file` (jpeg/png/pdf ≤ 5 MB), `request_id` → `{proof_id}` |
| GET | `/budget` | both | → `{week_start, spent_units, reserved_units, weekly_cap_units, delegated_remaining_units}` |
| GET | `/history` | both | → decisions + transfers (dependent sees own only) |
| GET | `/summaries` | guardian | → `WeeklySummary[]` |
| GET | `/audit` | guardian | → audit log entries for family |
| GET | `/events` | both | SSE stream: `message`, `question`, `transfer`, `summary` |

Errors: `{"error": {"code": "<UPPER_SNAKE>", "message": "..."}}` with proper HTTP status. Never return stack traces.

### 8.2 Agent tool contracts (§11.2 has rules)

Defined in `app/contracts/tools.py`. The model sees these JSON schemas; handlers validate with Pydantic.

| Tool | Input | Returns to model |
|---|---|---|
| `get_context` | `{}` | budget week, spent, remaining, delegated remaining, recent decisions (last 10), active limits, guidance |
| `request_transfer` | `{amount_units:int>0, purpose:str≤200, category:Category, category_confidence:float[0,1], reasoning:str≤1000}` | `{request_id, outcome, reason_codes[]}` |
| `ask_for_proof` | `{request_id?, reason:str≤300}` | `{ok}` |
| `ask_guardian` | `{request_id?, question:str≤500, recommendation: approve|deny|partial, recommended_amount_units?:int}` | `{question_id}` |
| `notify` | `{to: guardian|dependent, message:str≤500}` | `{ok}` |
| `propose_policy_update` | `{change_summary:str≤300, proposed_body: PolicyBody}` | `{draft_policy_id}` (needs guardian signature to activate) |

**There is no recipient, wallet, or address field in any tool.** Idempotency keys are added by the handler, not the model.

### 8.3 Auditor contract

`app/contracts/audit.py`

Input (built by the tool handler, never by the Agent):
```json
{
  "request_text": "raw dependent messages for this request",
  "proposal": {"amount_units": 0, "purpose": "", "category": "", "category_confidence": 0.0},
  "agent_reasoning": "…",
  "guidance": ["…"],
  "limits": {…},
  "history": {"week_spent_units": 0, "recent": [ {"amount_units":0,"category":"","outcome":""} ]},
  "proof_summary": "…|null"
}
```
Output (strict JSON, validated; invalid output = `flag` with reason `AUDITOR_INVALID_OUTPUT`):
```json
{"verdict": "agree|flag", "reason": "≤300 chars", "risk_signals": ["manipulation|purpose_mismatch|pattern|proof_mismatch|emergency_claim|other"]}
```
Prompt order: the Auditor's prompt presents `request_text`, guidance, history and proof **before** `agent_reasoning`, and instructs it to form a view first.

### 8.4 Signed messages (guardian wallet)

All guardian signatures are ed25519 over the UTF-8 text below, made with Phantom `signMessage`, verified server-side with PyNaCl against the guardian's wallet public key. Every message includes a server nonce (single use, 5 min expiry).

```
FinGuard:activate-policy
family:{family_id}
version:{version}
hash:{body_hash}
nonce:{nonce}
```
```
FinGuard:answer-question
family:{family_id}
question:{question_id}
decision:{approve|deny}
amount_units:{amount_units|none}
nonce:{nonce}
```
```
FinGuard:freeze
family:{family_id}
frozen:{true|false}
nonce:{nonce}
```
```
FinGuard:register-dependent
family:{family_id|new}
dependent_wallet:{pubkey}
dependent_token_account:{pubkey}
nonce:{nonce}
```
Nonces come from `POST /auth/nonce` with an added `purpose` field; the server stores purpose and rejects mismatches.

### 8.5 Engine → Signer (internal HTTP)

`POST http://signer:8100/v1/transfer` (internal network only, not exposed publicly)

Headers: `X-Timestamp` (unix seconds), `X-Nonce` (uuid), `X-Signature` = hex HMAC-SHA256(`SIGNER_HMAC_SECRET`, `{timestamp}.{nonce}.{raw_body}`).

Body:
```json
{"transfer_id":"uuid","family_id":"uuid","amount_units":0,
 "source_token_account":"pubkey","destination_token_account":"pubkey",
 "mint":"pubkey","idempotency_key":"…"}
```
Response: `{"status":"confirmed|failed","tx_signature":"…|null","error_code":"…|null"}`

---

## 9. Policy engine (deterministic)

Module: `app/engine/`. Pure functions; repositories injected. **No LLM, no randomness, no clock except injected `now`.**

### 9.1 `audit_required(family, policy, proposal, history) -> bool`
True if ANY:
- `amount_units ≥ limits.audit_threshold_units`
- `category_confidence < limits.low_confidence_threshold`
- `category == "emergency"`
- origin is scheduler AND amount > `allowance.amount_units`
- pattern rule: ≥ 3 requests in same category this budget week

### 9.2 `evaluate_transfer(ctx) -> Decision`
Runs checks **in this exact order**, stops at the first non-pass. Returns `Decision{outcome, reason_codes[], policy_version}`.

| # | Check | Fail outcome / code |
|---|---|---|
| 1 | Family not frozen | BLOCKED `FROZEN` |
| 2 | Active policy exists and signature valid | BLOCKED `POLICY_INACTIVE` / `POLICY_SIGNATURE_INVALID` |
| 3 | Dependent token account registered (signed) | BLOCKED `RECIPIENT_NOT_REGISTERED` |
| 4 | Idempotency key unused | return the original decision (no new transfer) |
| 5 | `amount_units > 0` | BLOCKED `INVALID_AMOUNT` |
| 6 | Category not blocked | BLOCKED `BLOCKED_CATEGORY` |
| 7 | Requests today ≤ `max_requests_per_day` | NEEDS_GUARDIAN `RATE_LIMIT_DAY` |
| 8 | `amount ≤ max_per_request` (unless guardian override) | NEEDS_GUARDIAN `OVER_MAX_PER_REQUEST` |
| 9 | `spent + reserved + amount ≤ weekly_cap` (unless guardian override) | NEEDS_GUARDIAN `OVER_WEEKLY_CAP` |
| 10 | `amount ≤ delegated_remaining − reserved` (read from chain) | BLOCKED `INSUFFICIENT_DELEGATION` |
| 11 | If `audit_required`: verdict exists | BLOCKED `AUDIT_MISSING` |
| 12 | If verdict = flag (and no guardian override) | NEEDS_GUARDIAN `AUDIT_FLAGGED` |
| 13 | Proof required (`amount ≥ proof_threshold`) and none attached | NEEDS_PROOF `PROOF_REQUIRED` |
| — | All pass | CLEARED |

**Guardian override** = a verified `answer-question` signature with `decision=approve` for this request. It skips checks 7, 8, 9, 12, 13 only. It never skips 1–6, 10, 11. Override amount must equal the signed `amount_units`.

### 9.3 On CLEARED (single DB transaction)
1. `SELECT … FOR UPDATE` the family's `budget_counters` row.
2. Re-run checks 9–10 under the lock.
3. Increase `reserved_units`; insert `transfers(status=pending)`; append audit log.
4. Commit, then call Signer.
5. On `confirmed`: move reserved → spent, store `tx_signature`, append audit log. On `failed`: release reservation, mark failed, append audit log, outcome becomes BLOCKED `SIGNER_FAILED`.

### 9.4 Reason codes (enum, `app/contracts/codes.py`)
`FROZEN, POLICY_INACTIVE, POLICY_SIGNATURE_INVALID, RECIPIENT_NOT_REGISTERED, DUPLICATE_REQUEST, INVALID_AMOUNT, BLOCKED_CATEGORY, RATE_LIMIT_DAY, OVER_MAX_PER_REQUEST, OVER_WEEKLY_CAP, INSUFFICIENT_DELEGATION, AUDIT_MISSING, AUDIT_FLAGGED, AUDITOR_INVALID_OUTPUT, PROOF_REQUIRED, SIGNER_FAILED, SIGNER_REJECTED`

Outcomes: `CLEARED | BLOCKED | NEEDS_GUARDIAN | NEEDS_PROOF`.

---

## 10. Signer

Service `services/signer/`. Separate process and container; the **only** holder of the delegate private key.

On every `/v1/transfer` it MUST, in order:
1. Verify HMAC, timestamp within ±30 s, nonce unused (store last 10 min) → else 401 `SIGNER_REJECTED`.
2. Verify `destination_token_account` is the registered destination for `family_id`, read via `DATABASE_URL_READONLY` from the view `registered_destinations` → else `SIGNER_REJECTED`.
3. Verify `amount_units ≤ SIGNER_MAX_PER_TRANSFER_UNITS` and today's signed total + amount ≤ `SIGNER_MAX_PER_DAY_UNITS` → else `SIGNER_REJECTED`.
4. Read on-chain delegation of `source_token_account`: delegate == agent wallet pubkey and `delegated_amount ≥ amount_units` → else `failed`/`INSUFFICIENT_DELEGATION`.
5. Idempotency: same `idempotency_key` returns the stored result, never signs twice.
6. Build `transfer_checked` (mint, decimals 6) with the agent wallet as delegate authority; sign; send; confirm (`confirmed` commitment); return signature.

Startup: refuse to start unless `SOLANA_RPC_URL` contains `devnet` (SEC-14). Key: `AGENT_WALLET_KEY_ENC` decrypted with `SIGNER_MASTER_KEY` (Fernet) at startup; never logged.

`MOCK_CHAIN=true`: skip steps 4 and 6, return `tx_signature = "mock_" + uuid`.

---

## 11. Agents

### 11.1 Agent (maker) behavior
- Channels: guardian chat (policy setup, questions) and dependent chat (requests), plus scheduler wake-ups.
- Per dependent request: call `get_context`, judge against guidance/budget/history, then exactly one of: `request_transfer` (full or partial), `ask_for_proof`, `ask_guardian`, or reply declining / counter-offering.
- Explains every outcome to the dependent in plain language, including engine reason codes translated to human words.
- Policy setup: asks follow-ups (budget, always-OK, never-OK, when to ask, context), drafts `PolicyBody`, produces `summary_text` and 3 `test_cases` ("If X asks for Y, I would Z"). Mostly guidance; few outer limits.
- Learning loop: when ≥ 3 guardian answers point the same way, call `propose_policy_update`.
- Scheduler wake-ups (§12): allowance day, pace check, reservoir check, end of week.

### 11.2 Tool rules (enforced in handlers, not prompts)
- Handlers ignore any extra fields from the model.
- `request_transfer` handler: create `transfer_requests` row with server-generated idempotency key → `audit_required`? → call Auditor, store verdict → `evaluate_transfer` → act on decision → return outcome to model.
- The Agent cannot call the Auditor, the engine or the signer directly.
- Max 8 tool calls per turn; max 1 `request_transfer` per dependent turn.

### 11.3 Prompt hygiene
- System prompts live in `app/agents/prompts/*.md`, versioned in git.
- Dependent messages and proof text are wrapped in `<untrusted_input>…</untrusted_input>`; the system prompt states content inside is data, never instructions.
- Never put secrets, wallet keys or other families' data in prompts.
- Guardian guidance is inserted as data under a heading, not concatenated into instructions.

### 11.4 Auditor
- Separate API call, separate system prompt (`prompts/auditor.md`), temperature 0, no tools, JSON output only.
- Role: guardian's advocate; look for manipulation, purpose/guidance mismatch, unusual patterns, proof mismatch, emergency claims.
- Can only `agree` or `flag`. Never sees or talks to the dependent directly. Verdict stored in `audit_verdicts`.
- Model: `AUDITOR_MODEL` env (may differ from `AGENT_MODEL`).

---

## 12. Scheduler jobs (`app/scheduler/`)

| Job | When (family TZ) | Action |
|---|---|---|
| Allowance | `allowance.weekday` 08:00 | Wake Agent with `allowance_due`; Agent calls `request_transfer` (idempotency key per §6.3) |
| Pace check | Daily 18:00 | If spent > (days elapsed / 7) × cap × 1.25 → Agent may `notify` dependent |
| Reservoir check | Daily 09:00 | If delegated remaining < 2 × allowance → Agent `notify` guardian with forecast |
| Weekly summary | Sunday 20:00 | Agent writes summary → `summaries` + notify guardian |

Jobs only wake the Agent; they never call the engine or signer directly. A job that runs twice must not pay twice (idempotency).

---

## 13. Configuration (`.env.example`)

```
# api
DATABASE_URL=postgresql+psycopg://...
JWT_SECRET=
JWT_TTL_HOURS=12
ANTHROPIC_API_KEY=
AGENT_MODEL=
AUDITOR_MODEL=
DATA_ENCRYPTION_KEY=          # Fernet, for chats/proofs at rest
SIGNER_URL=http://signer:8100
SIGNER_HMAC_SECRET=
SOLANA_RPC_URL=https://api.devnet.solana.com
AUSD_MINT=
AGENT_WALLET_PUBKEY=
RATE_LIMIT_PER_MIN=30
LLM_TOKEN_BUDGET_PER_FAMILY_PER_DAY=200000
MOCK_CHAIN=false

# signer
SIGNER_HMAC_SECRET=
SIGNER_MASTER_KEY=
AGENT_WALLET_KEY_ENC=
SIGNER_MAX_PER_TRANSFER_UNITS=200000000
SIGNER_MAX_PER_DAY_UNITS=300000000
SOLANA_RPC_URL=https://api.devnet.solana.com
DATABASE_URL_READONLY=
MOCK_CHAIN=false
```

---

## 14. Data model (PostgreSQL)

| Table | Key columns |
|---|---|
| `users` | id, wallet_address (unique), role, display_name, created_at |
| `families` | id, guardian_user_id, dependent_user_id, dependent_token_account, registration_signature, timezone, frozen (bool), created_at |
| `nonces` | nonce (pk), wallet, purpose, expires_at, used_at |
| `policies` | id, family_id, version, body (jsonb), body_hash, guardian_signature, status (`draft|active|superseded`), created_at, activated_at |
| `messages` | id, family_id, channel (`guardian|dependent`), sender (`user|agent|system`), content_enc (bytea), created_at |
| `transfer_requests` | id, family_id, origin (`dependent|scheduler`), amount_units, purpose, category, category_confidence, agent_reasoning, idempotency_key (unique), created_at |
| `audit_verdicts` | id, request_id, verdict, reason, risk_signals (text[]), model, created_at |
| `guardian_questions` | id, family_id, request_id?, kind (`exception|question|policy_update`), question, recommendation, recommended_amount_units?, status (`open|answered|expired`), decision?, signature?, created_at, answered_at |
| `decisions` | id, request_id, outcome, reason_codes (text[]), policy_version, created_at |
| `transfers` | id, request_id (unique), amount_units, destination_token_account, status (`pending|confirmed|failed`), tx_signature, error_code, created_at |
| `budget_counters` | family_id + week_start (pk), spent_units, reserved_units |
| `proofs` | id, request_id, file_enc (bytea), mime, extracted (jsonb), created_at |
| `summaries` | id, family_id, week_start, content, created_at |
| `audit_log` | id (bigserial), family_id, event_type, payload (jsonb), prev_hash, hash, created_at |

`audit_log.hash = sha256(prev_hash || canonical_json({event_type, payload, created_at}))`. App DB role has INSERT/SELECT only on `audit_log` (no UPDATE/DELETE). View `registered_destinations` exposes only `families.dependent_token_account` to the signer's read-only role.

Events written to `audit_log`: `policy_activated, policy_proposed, question_asked, question_answered, request_created, audit_verdict, decision, transfer_pending, transfer_confirmed, transfer_failed, freeze_changed, login`.

---

## 15. Security requirements (non-negotiable)

| ID | Requirement | Where | Test |
|---|---|---|---|
| SEC-01 | LLM output never selects a recipient; no tool has an address field | agents, contracts | schema test |
| SEC-02 | Every transfer passes `evaluate_transfer`; no other code path calls the signer | engine, chain | grep test + integration |
| SEC-03 | Signer independently checks HMAC, allowlist, own caps, delegation | signer | unit tests per check |
| SEC-04 | Policy active only with valid guardian signature over its hash | engine, gateway | tamper test |
| SEC-05 | Guardian approvals/overrides only via verified signature; chat claims ignored | engine, agents | "Mom said OK" test |
| SEC-06 | Required Auditor verdict must exist before clearing | engine | skip-audit test |
| SEC-07 | Idempotency: one key → at most one transfer, also on retries | engine, signer | replay test |
| SEC-08 | Budget updates under row lock in one transaction | engine | concurrent test (2 parallel requests) |
| SEC-09 | SIWS login: nonce single-use, ≤ 5 min, domain-bound; JWT ≤ 12 h | gateway | replayed-nonce test |
| SEC-10 | Role checks on every route; dependent can't read guidance or other families | gateway | authz tests |
| SEC-11 | Rate limits, request size limits, per-family LLM token budget | gateway, agents | load test |
| SEC-12 | Untrusted input delimited in prompts; injection in proof can't exceed limits | agents | injected-receipt test |
| SEC-13 | Delegate key encrypted at rest, only in signer, never logged | signer | log scan test |
| SEC-14 | Signer refuses non-devnet RPC | signer | startup test |
| SEC-15 | Chats and proofs encrypted at rest; no personal data in on-chain memos | db, signer | inspection test |
| SEC-16 | Append-only, hash-chained audit log; chain verifies end-to-end | db | tamper-detect test |
| SEC-17 | Freeze (signed) blocks all transfers immediately | engine | freeze test |
| SEC-18 | No secrets in repo; `.env` git-ignored; CI secret scan | repo | CI |
| SEC-19 | Errors never leak stack traces, keys, or other families' data | gateway | error test |
| SEC-20 | Default deny: missing/malformed data → BLOCKED | engine | fuzz test |

**Attack demos** (`scripts/attacks/`, also pytest scenarios): (1) talk-the-agent-into-it, (2) injected receipt, (3) replayed request, (4) guardian revokes delegation mid-demo, (5) agent skips audit (direct handler call without verdict).

---

## 16. Flows (implementation steps)

### 16.1 Guardian onboarding and policy
1. Connect Phantom (devnet) → `/auth/nonce` → sign → `/auth/verify`.
2. Register dependent wallet + token account with `register-dependent` signature → `POST /families`.
3. `/guardian/chat` loop: Agent asks follow-ups → drafts `PolicyBody` → engine validates schema (§7) → returns draft with summary + test cases.
4. Guardian signs `activate-policy` → `POST /policy/drafts/{id}/activate` → engine verifies signature + hash → status active, previous superseded, audit log.
5. Guardian signs SPL `approve` in Phantom (delegate = `AGENT_WALLET_PUBKEY`, amount = reservoir) — client-side transaction. `/budget` reads delegated remaining from chain.

### 16.2 Dependent request
1. `/dependent/chat` → Agent `get_context` → judges.
2. Optional `ask_for_proof` → dependent uploads via `/proofs` → Agent reads (vision) → continues.
3. Optional `ask_guardian` → question appears in guardian app → guardian signs answer → engine records override/denial → Agent informed via event.
4. `request_transfer` → handler → Auditor if required → `evaluate_transfer` → §9.3 → Signer → result.
5. Agent explains outcome; SSE `transfer` event to both apps.

### 16.3 Agent-initiated
Per §12. Same handler path as 16.2 step 4.

---

## 17. Decisions and defaults (resolve open questions here)

| Topic | Default for MVP | Status |
|---|---|---|
| Emergency with no guardian answer | Wait; no automatic emergency amount | default |
| Several dependents per guardian | Out of scope: 1 guardian, 1 dependent | default |
| Multiple guardians | Out of scope | default |
| Dependent policy visibility | Outer limits only | default |
| Notifications | In-app + SSE only | default |
| Auditor threshold | `audit_threshold_units` set by guardian; default 20% of weekly cap | default |
| Auditor model | Same family as Agent; separate prompt; switchable via env | default |
| Custodial fallback | Not implemented | default |

To change a default: `[SPEC]` PR updating this table.

---

## 18. Out of scope

Mainnet, real fiat, KYC, multiple dependents/guardians, push notifications, LINE/Telegram bots, on-chain spending-limit programs, KMS/HSM (mention as production path only).

---

## 19. Definition of done (per PR)

- Matches SPEC sections it touches; SPEC updated if contracts changed.
- ruff, mypy (strict) and the app's linters pass; tests pass; new logic has tests; SEC tests untouched or strengthened.
- No secrets, no floats in money paths, no new tool fields that accept addresses.
- PR description lists SPEC sections implemented (e.g. "Implements §9.2 checks 1–6").

---

## 20. Milestones

| # | Goal | Done when |
|---|---|---|
| M0 | Contracts frozen | `app/contracts/` merged; OpenAPI generated; mocks run in docker-compose |
| M1 | Vertical slice (mock chain) | Dependent asks → Agent → engine → mock signer → UI shows result |
| M2 | Real devnet | Mint, delegation, real `transfer_checked`, signatures visible in Solana Explorer |
| M3 | Auditor + security | Auditor live; all SEC tests green; 5 attack demos scripted |
| M4 | Demo polish | Seed script, demo walkthrough, weekly summary, guardian learning loop |

---

## 21. Changelog

| Date | Version | Change |
|---|---|---|
| 2026-10-17 | 1.0 | Initial spec from architecture doc |
| 2026-10-18 | 1.0.1 | The client is a native mobile app (iOS, Android), not a web app: §3 stack, §4 layout and ownership, §18, §19; receipts use verified in-app capture |
