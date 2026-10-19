# SPEC: Sigil, Delegated Financial Guardianship

> **The project's single source of truth.** Every person and AI coding agent on the four
> layers implements against this file and the documents it points to (§0). If code and this
> repo's docs disagree, the docs win until they are changed. If they are silent, don't
> invent: use the decision in §15, or add an entry there and ask a human.

- Project: FinTechathon 2026 entry (theme: AI as a Financial Participant)
- Version: 2.5 (see §19)
- Security weighs 30% of judging. Requirements tagged `SEC-xx` (§13) are non-negotiable.

---

## 0. Where each fact lives

The docs in `gateway/` (this folder of the Sigil monorepo) are the source of truth for the
whole project. Each fact is written down in exactly one place:

| Topic | Source of truth |
|---|---|
| Product, architecture, conventions, policy, security requirements, decisions, rules | this file |
| Design rationale, threats and controls, user flows | [docs/architecture.md](docs/architecture.md) |
| App API (app ↔ gateway): every route, field and error | [README.md](README.md) "App API" |
| Platform API (agent → gateway) | `src/gateway/contracts/agent.py` and README "Platform API" |
| Inbound API (gateway → agent) | README "Platform API" (inbound table) |
| Crypto API (gateway → crypto) | `src/gateway/contracts/crypto.py` and README "Crypto layer contract" |
| Policy engine: checks in order, the money transaction | README "The policy engine" |
| Gateway data model, DB roles, configuration, running | README "Data", "Configuration", "Running" |
| Code style | [GUIDELINES.md](GUIDELINES.md) |

The handoffs are guides for one layer each, written from the sources above. If a handoff
disagrees with them, the sources win; fix the handoff.

| Layer | Guide |
|---|---|
| App (Shine) | [docs/app-handoff.md](docs/app-handoff.md), [docs/auth-guide.md](docs/auth-guide.md) |
| Agent (Alwin) | [docs/agent-handoff.md](docs/agent-handoff.md) |
| Crypto (Han) | [docs/crypto-handoff.md](docs/crypto-handoff.md) |

**The agent contract is shared.** `src/gateway/contracts/agent.py` is a verbatim copy of the
agent layer's `contracts.py`; a parity test fails if they differ. A change is agreed with
Alwin, then lands in both folders and in these docs together, in one commit.

---

## 1. Product

A guardian (parent, family member) funds a reservoir in their own Phantom wallet and
describes, in plain language, how each of their dependents (a student, an elderly parent,
anyone they support) should be helped with money. An AI **agent** then runs each dependent's
allowance day to day: it judges requests, negotiates, asks for proof, paces the budget, sends
the weekly allowance, asks the guardian when needed, and learns the guardian's preferences.
An independent **Auditor** agent reviews larger or riskier decisions. Deterministic code (the
**policy engine** and the **signer**) guarantees that nothing moves beyond the guardian's
signed outer limits, and only to the dependent's registered account. Money moves as an SPL
token on **Solana devnet** through SPL token delegation, so the app never holds funds.

**Core security claim:** even a fully manipulated agent cannot move more than the guardian
allowed, to anyone other than the dependent.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| Guardian | Funds the reservoir and sets intent. Signs in with their wallet. May support several dependents |
| Dependent | Receives money. Signs in with their wallet. Has exactly one guardian |
| Family | One guardian and one of their dependents. Each family has its own policy, weekly budget, freeze, questions, history and audit trail |
| Reservoir | The guardian's aUSD token account, delegated to the agent wallet. **One per guardian**, shared by all their dependents |
| Agent wallet | The delegate keypair: the agent's on-chain identity. Its private key exists only in the crypto layer |
| Agent (maker) | The main LLM agent. Decides what to do with each request |
| Auditor | The second LLM agent (checker). Agrees or flags; never approves |
| Policy | A signed, versioned body: outer limits plus guidance (§6). One active policy per family |
| Outer limits | Rules the engine enforces: weekly cap, max per request, blocked categories, proof and review thresholds, allowance |
| Guidance | Plain-language intent the agent interprets. Never enforced by code; never shown to the dependent |
| Policy engine | The gateway's deterministic code that clears or blocks every transfer |
| Signer | The crypto layer's service that holds the agent wallet key and signs `transfer_checked` |
| Base units | Integer token amount; 1 aUSD = 1,000,000 base units |
| Guardian signature | An ed25519 signature by the guardian's Phantom wallet over a fixed text (§7.5) |

---

## 3. Layers and trust zones

Sigil is four services that talk over HTTP.

| Layer | Owner | Folder | Responsibility |
|---|---|---|---|
| App | Shine | — | Native iOS app for guardians and dependents: Phantom sign-in and signing, chat, in-app receipt camera |
| Agent | Alwin | `agent/` | Maker, Auditor, scheduler. Reads and acts only through the gateway's platform API |
| **Gateway** | Julian | `gateway/` (these docs) | App API, platform API, policy engine, database, audit log, crypto client |
| Crypto | Han | `crypto/` | Signer holding the agent wallet key; delegation reads; `transfer_checked` on Solana devnet; devnet scripts |

```
iOS app (untrusted) ──HTTPS, JWT──▶ Gateway: App API
                                       │  runs the agent on each request
                                       ▼
            Agent layer (output untrusted) ──maker / Auditor tokens──▶ Gateway: platform API
                                                                          │
                              Trusted core: policy engine + PostgreSQL ◀──┘
                                       │  HMAC-signed, already cleared
                                       ▼
            Crypto layer: signer (re-checks independently) ──transfer_checked──▶ Solana devnet
```

| Zone | Components | Rule |
|---|---|---|
| Clients | iOS app | Every input authenticated, rate-limited and schema-validated by the gateway |
| Agent zone | Maker, Auditor, scheduler | Output treated like user input. No database or chain access; only the platform API |
| Trusted core | Policy engine, PostgreSQL, signer | No LLM. Enforces the guardian's **signed** policy. Default deny |
| Chain | Solana devnet | The token program caps total spending at the delegated amount |

**The money path is always:** agent asks → (Auditor, when review is required) → policy
engine → signer, re-checking → Solana. No other code path reaches the signer.

---

## 4. Tech stack

| Part | Choice |
|---|---|
| App | Native iOS; receipts from the in-app camera, signed with an Apple App Attest key |
| Wallet | Phantom mobile in testnet mode (devnet only), through Phantom's deeplinks |
| Sign-in | Sign-In With Solana: the gateway's text signed with Phantom `signMessage`; JWT sessions ≤ 12 h |
| Gateway | Python ≥ 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async, psycopg 3), Alembic, PostgreSQL 16 |
| Agent layer | Python, Anthropic API (Claude) with tool use and vision; the Auditor as a separate call; APScheduler |
| Crypto layer | Python 3.12, FastAPI, `solana` (solana-py incl. `spl.token`), `solders`, PyNaCl |
| Chain | Solana devnet; own SPL test mint **aUSD**, 6 decimals |
| Tests and lint | pytest; ruff and mypy (strict) for Python; the app's own tooling for iOS |
| Local dev | docker-compose in this repo: `postgres`, `migrate`, `mock-crypto`, `gateway` |

Pin versions in each repo's lock file; don't upgrade majors mid-hackathon. A dependency not
listed here is noted in the commit or PR that adds it (the gateway's are listed in README
"Configuration").

---

## 5. Conventions

### 5.1 Money
- Token **aUSD**, SPL, **6 decimals**. Stored and passed as **integer base units**
  (Postgres `BIGINT`). Never floats; the backend never rounds.
- The only place display units appear is the agent's policy draft (`PolicyDraft`), which the
  gateway converts exactly (at most 2 decimals) to base units before anything is signed.
- Field names follow the agent contract (`amount`, `amount_requested`, `already_sent`,
  `weekly_cap`); the crypto API and the signed texts use `_units`.

### 5.2 Time
- Stored in UTC (`timestamptz`). Each family has a `timezone` (default `Asia/Bangkok`).
- **Budget week:** Monday 00:00 to the next Monday 00:00 in the family's timezone.
- Code that decides about money takes the time as an input (an injected clock), never from
  the database.

### 5.3 Ids and idempotency
- Primary keys are UUIDv4, except `audit_log.id` (bigserial).
- Every transfer has an idempotency key (≤ 64 chars), derived by the agent layer's code,
  never chosen by the model: `req:{request_id}:{thread length}` for a request,
  `allowance:{dependent_id}:{ISO year}-W{week}` for the allowance. One key → at most one
  transfer, ever (SEC-07).

### 5.4 Canonical JSON
For hashing and signing: UTF-8, sorted keys, no insignificant whitespace
(`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).

### 5.5 Categories and outcomes
Fixed by the agent contract (`contracts/agent.py`):

- **Categories:** `education, food, transport, health, clothing, entertainment, gaming,
  savings, allowance, other`. `allowance` cannot be blocked.
- **Outcomes** of an agent run: `sent_full, sent_partial, counter_offer, asked_proof,
  asked_guardian, asked_dependent, declined, out_of_scope`, and, set by code only,
  `auditor_flagged, blocked, safe_fallback, transfer_pending, receipt_accepted,
  receipt_rejected` (the last two end a receipt check). Clients must accept new outcomes.

---

## 6. Policy

`src/gateway/contracts/policy.py`, `PolicyBody`; stored as `policies.body` (jsonb).

```json
{
  "schema_version": 1,
  "currency": "aUSD",
  "weekly_cap": 100000000,
  "max_per_request": 40000000,
  "blocked_categories": ["gaming"],
  "proof_threshold": 25000000,
  "review_threshold": null,
  "allowance": {"amount": 30000000, "weekday": 0},
  "guidance": "School supplies and bus fare are always fine. Ask me about trips."
}
```

- Amounts are integer base units ≥ 0 (strict: floats, strings and booleans are refused).
- `max_per_request ≤ weekly_cap`; `proof_threshold ≤ max_per_request`;
  `review_threshold ≤ max_per_request` when set (unset: 25% of `weekly_cap`);
  `allowance.amount ≤ weekly_cap`; `weekday` 0–6, 0 = Monday.
- `blocked_categories`: from §5.5, no repeats, never `allowance`. `guidance`: ≤ 4,000 chars.
  No other fields.
- **Policy hash** = SHA-256 hex of the canonical JSON (§5.4) of the body.
- A policy is **active** only after the guardian signs `activate-policy` (§7.5) over its
  version and hash. The engine re-verifies that signature on every transfer. One active
  policy per family; activating supersedes the previous one.
- Drafts come from the policy interview: the guardian chats with the agent, the gateway
  converts the agreed draft exactly, and only the guardian's signature activates it.

---

## 7. Interfaces

### 7.1 Overview

| Boundary | Direction | Auth | Exact contract |
|---|---|---|---|
| App API `/api/v1/*` | app → gateway | SIWS, then `Authorization: Bearer <JWT>` | README "App API" |
| Platform API `/agent/v1/*` | agent → gateway | maker token; Auditor token (verdicts, read-only context) | `contracts/agent.py`, README "Platform API" |
| Inbound API `/v1/*` | gateway → agent | `AGENT_INBOUND_TOKEN` | README "Platform API" |
| Crypto API `/v1/delegation`, `/v1/transfer` | gateway → crypto | HMAC-SHA256 per request | `contracts/crypto.py`, README "Crypto layer contract" |

### 7.2 App API essentials
- Sign-in: `POST /auth/nonce {wallet, purpose: "login"}` returns the exact text; the app
  signs it with Phantom `signMessage`; `POST /auth/verify` returns the token. Nonces are
  single use, 5 minutes, bound to wallet, purpose and `SIWS_DOMAIN` (SEC-09). Guide:
  [docs/auth-guide.md](docs/auth-guide.md).
- The JWT proves identity only (`sub` = user id). Role and families are read from the
  database on every request. A wallet registered as a dependent is a dependent; any other is
  a guardian.
- A guardian with several dependents names the family with the `X-Family-Id` header
  (`400 FAMILY_REQUIRED` without it; another's family is `404`).
- Errors: `{"error": {"code": "UPPER_SNAKE", "message": "..."}}`; never stack traces or
  upstream details (SEC-19).

### 7.3 Platform and inbound API essentials
- The agent identifies a dependent and a guardian by user id. The agent's usage "family" is
  the guardian (token budget per guardian).
- Nothing the agent sends can name a recipient, wallet or address (SEC-01); unknown fields
  are refused.
- Only the Auditor's token can post a verdict (SEC-06).
- `POST /agent/v1/transfers` returns `sent`, `blocked` (with a reason), `duplicate` or
  `pending` (outcome unknown; never reported as blocked).

### 7.4 Crypto API essentials
- `GET /v1/delegation/{token_account}` → `{token_account, owner, delegate,
  delegated_amount_units}`, read fresh from chain.
- `POST /v1/transfer {transfer_id, family_id, amount_units, source_token_account,
  destination_token_account, mint, idempotency_key}` →
  `{status: confirmed|failed, tx_signature, error_code}`.
- Every request: `X-Timestamp`, `X-Nonce`, `X-Signature` = hex HMAC-SHA256(secret,
  `{timestamp}.{nonce}.{raw_body}`).
- **Status semantics:** `confirmed` = moved; `failed` or 4xx = definitely not moved (the
  gateway releases the reservation); 5xx or no answer = unknown (the gateway keeps the
  reservation and retries with the same key).

### 7.5 Guardian-signed messages
ed25519 over the UTF-8 text below (`\n` line breaks, no trailing newline), made with Phantom
`signMessage`, verified by the gateway against the guardian's wallet. Each needs a nonce
from `POST /auth/nonce` with the matching `purpose`; the gateway rebuilds the text from its
own data and never accepts a client-built message.

```
FinGuard:register-dependent          purpose: register-dependent
family:new
dependent_wallet:{pubkey}
dependent_token_account:{pubkey}
nonce:{nonce}
```
```
FinGuard:activate-policy             purpose: activate-policy
family:{family_id}
version:{version}
hash:{body_hash}
nonce:{nonce}
```
```
FinGuard:answer-question             purpose: answer-question
family:{family_id}
question:{question_id}
decision:{approve|deny}
amount_units:{amount_units|none}
nonce:{nonce}
```
```
FinGuard:freeze                      purpose: freeze
family:{family_id}
frozen:{true|false}
nonce:{nonce}
```

---

## 8. Policy engine

`src/gateway/engine/`. The ordered checks and the transaction are in README "The policy
engine". These invariants hold:

1. **Pure decision.** `check_transfer` takes facts and returns "cleared" or a reason. No
   database, network, clock or randomness; the first failing check blocks; missing or
   unverifiable data blocks (SEC-20).
2. **Facts are verified, not trusted.** The engine loads the policy, signatures, verdicts,
   proofs and the delegation itself. Nothing the agent claims stands in for them.
3. **Recipient and source.** Money goes only to the family's registered
   `dependent_token_account`, from the guardian's reservoir, which must be owned by the
   guardian and delegated to the agent wallet.
4. **Shared reservoir.** The delegation check subtracts money pending for *any* of the
   guardian's dependents, under a lock on the reservoir; weekly caps stay per dependent.
5. **Review.** The engine applies the review triggers it can verify itself (a copy of the
   agent layer's `trusted_triggers`, kept identical by a parity test). When they fire, an
   unused `agree` verdict bound to the key, amount, category and dependent is required.
6. **Guardian override.** A wallet-signed "approve N" for a request lets exactly N past the
   amount cap, the weekly cap and the review. It never skips freeze, the signed policy, the
   destination, the delegation, the requested amount or proofs.
7. **Allowance.** Capped by the signed `allowance.amount` (not `max_per_request`), one per ISO
   week (by key). Raising it needs the guardian.
8. **Requested amount.** Never more than `amount_requested − already_sent − pending`.
9. **Proofs come after payment.** A dependent cannot buy anything before being paid, so a
   request at or above `proof_threshold` with no counting proof is sent first, after an
   Auditor review (trigger `pay_first`), and leaves a receipt owed. A dependent may owe up to
   **3** receipts (`MAX_OPEN_OBLIGATIONS`, pending transfers that will owe one included); at
   3 they get no more money for requests (`409 PROOF_OUTSTANDING`, engine `proof
   outstanding`) until one is settled. A guardian override does not lift that, and the
   scheduled allowance is unaffected. The agent sees what is owed (`proofs_owed` in its
   context, `proof_owed` on the request) and, when a receipt arrives for a request that
   owes one, runs a receipt check: the model reports what the receipt says, code offers it
   to `POST /agent/v1/proof-obligations/{id}/settle`, and the gateway decides. An owed
   receipt is settled only by a proof that counts, belongs to the request and whose
   findings cover the amount — never the agent's say-so. A receipt backs one transfer: across the guardian's dependents (id, file
   hash, fingerprint) and across all families (keyed fingerprint). Receipts for online
   orders arrive as PDFs: the gateway verifies the shop's signature over the file and only
   a PDF that verifies against a trusted root and an accepted shop counts as proof or
   reaches the model. An unsigned one is stored and ignored.
10. **Transaction.** Reserve, record and spend verdicts and proofs in one commit under the
    locks; call the crypto layer outside any lock; settle on a definite answer; keep
    `pending` and retry on an unknown one (SEC-07, SEC-08).

---

## 9. Crypto layer requirements

Full guide: [docs/crypto-handoff.md](docs/crypto-handoff.md). On every `/v1/transfer` the
signer MUST, in order:

1. Verify the HMAC, the timestamp (±30 s) and that the nonce is unused (10 min) → else
   `401 SIGNER_REJECTED`.
2. If the idempotency key already has a final result, return it unchanged; if it is in
   progress, return 503. Never sign a key twice, including after a crash (SEC-07).
3. Check that `destination_token_account` and `source_token_account` are the family's
   registered accounts, from the `registered_destinations` view (§12) → else 4xx.
4. Check that `mint` is aUSD, `amount_units ≤ SIGNER_MAX_PER_TRANSFER_UNITS`, and today's
   signed total + amount ≤ `SIGNER_MAX_PER_DAY_UNITS` → else 4xx.
5. Check the on-chain delegation: delegate = agent wallet and amount ≥ `amount_units` → else
   `failed INSUFFICIENT_DELEGATION`.
6. Sign `transfer_checked` (decimals 6, agent wallet as delegate authority, **no memo**);
   send; confirm at `confirmed`; return the signature.

Startup: refuse unless `SOLANA_RPC_URL` contains `devnet` (SEC-14). The key comes from
`AGENT_WALLET_KEY_ENC`, decrypted with `SIGNER_MASTER_KEY` (Fernet), and is never logged
(SEC-13). `MOCK_CHAIN=true` skips steps 5 and 6 and returns `mock_<uuid>`.

---

## 10. Agent layer requirements

Implemented in `agent/`; guide: [docs/agent-handoff.md](docs/agent-handoff.md).

- **Maker:** judges each request (send full or partial, counter-offer, ask for proof, ask
  the guardian, decline), sends the allowance, runs the policy interview, writes the weekly
  summary. Its tools are `request_transfer`, `ask_for_proof`, `ask_guardian`,
  `propose_policy_update`, `notify_guardian`, `report_receipt` (receipt checks only) and
  `finish`; none has a recipient field.
  Per run: 1 transfer, 1 guardian question, 10 tool calls, a 150 s deadline. Every run ends
  with an explicit outcome; failures end as `safe_fallback`.
- **Auditor:** a separate call with its own prompt and token. Forms its view from the raw
  request, proof, guidance and history before reading the maker's reasoning; returns
  `agree` or `flag`; any failure counts as a flag. On a flag no money moves and the guardian
  is asked with both views.
- **Untrusted input:** dependent text, proofs and platform text are delimited as data;
  injection signals force an Auditor review (SEC-12).
- **Scheduler** (Asia/Bangkok by default): allowance at 07:00 on the policy's weekday, pace
  check daily 18:00, reservoir check daily 09:00 (one top-up question per guardian, filed
  under their first dependent), weekly summary Sunday 19:00, learning proposals Sunday
  19:30. Jobs act only through the platform API; a job that runs twice never pays twice.

---

## 11. App requirements

Implemented by the iOS app; guides: [docs/app-handoff.md](docs/app-handoff.md),
[docs/auth-guide.md](docs/auth-guide.md).

- Sign in and sign every guardian message (§7.5) with Phantom `signMessage`, byte for byte.
- Guardian: register dependents (all on the same reservoir), run the policy interview, sign
  activation, answer questions, freeze, and sign the SPL `approve` (delegate =
  `AGENT_WALLET_PUBKEY`) in Phantom. Send `X-Family-Id` when there are several dependents.
- Dependent: requests and follow-ups; proofs from the in-app camera with App Attest-signed
  capture claims (no gallery picker), or the PDF receipt of an online order. After a
  payment at or above `proof_threshold`, show what is owed (`GET /proof-obligations`) and
  that at 3 owed receipts no further request is possible until one is sent.
- Live updates by SSE with the token in the `Authorization` header, never the URL;
  agent-backed calls need a timeout of at least 3 minutes.

---

## 12. Data and the audit log

The gateway owns the database (tables: README "Data").

- Chats, proofs, notifications and agent replies are encrypted at rest (Fernet,
  `DATA_ENCRYPTION_KEY`) (SEC-15).
- **Audit log:** one hash chain per family plus a system chain;
  `hash = sha256(prev_hash || canonical_json({event_type, payload, created_at}))`. Payloads
  hold ids and amounts, never chat content. A trigger rejects UPDATE, DELETE and TRUNCATE;
  every read re-verifies the chain (SEC-16).
- **Roles:** `sigil_owner` runs migrations; `sigil_app` (the gateway) has INSERT/SELECT
  only on append-only tables; `sigil_signer_ro` (the crypto layer) can read only the view
  `registered_destinations (family_id, dependent_token_account, guardian_token_account)`.

---

## 13. Security requirements (non-negotiable)

| ID | Requirement | Layer | Status |
|---|---|---|---|
| SEC-01 | LLM output never selects a recipient; no tool or platform payload has an address field | agent, gateway | gateway tested |
| SEC-02 | Every transfer passes the policy engine; no other code path calls the signer | gateway | tested |
| SEC-03 | The signer independently checks HMAC, registered destination **and source**, its own caps and the delegation | crypto | tested (`crypto/`) |
| SEC-04 | A policy is enforced only with the guardian's signature over its hash and version | gateway | tested |
| SEC-05 | Guardian approvals and overrides only via verified signature; chat claims ignored | gateway, agent | gateway tested |
| SEC-06 | A required Auditor verdict must exist before clearing; only the Auditor can post one | gateway, agent | gateway tested |
| SEC-07 | One idempotency key → at most one transfer, also on retries and crashes | gateway, crypto | gateway tested |
| SEC-08 | Budget and reservoir updates under locks in one transaction | gateway | tested (parallel requests, parallel siblings) |
| SEC-09 | SIWS: nonce single use, ≤ 5 min, domain-bound; JWT ≤ 12 h | gateway | tested |
| SEC-10 | Role and family checks on every route; dependents can't read guidance or other families | gateway | tested |
| SEC-11 | Rate limits, request size limits, daily dependent message quota, per-guardian LLM token budget | gateway, agent | gateway tested |
| SEC-12 | Untrusted input delimited in prompts; injection in a proof can't exceed limits | agent | agent layer |
| SEC-13 | Delegate key encrypted at rest, only in the signer, never logged | crypto | tested (`crypto/`) |
| SEC-14 | The signer refuses a non-devnet RPC | crypto | tested (`crypto/`) |
| SEC-15 | Chats and proofs encrypted at rest; no personal data in on-chain memos | gateway, crypto | gateway tested |
| SEC-16 | Append-only, hash-chained audit log that verifies end to end | gateway | tested |
| SEC-17 | A signed freeze blocks all of the family's transfers immediately | gateway | tested |
| SEC-18 | No secrets in repos; `.env` git-ignored; CI secret scan | all | tested (CI: gitleaks over the whole history) |
| SEC-19 | Errors never leak stack traces, keys or other families' data | gateway | tested |
| SEC-20 | Default deny: missing or malformed data blocks | gateway | tested |

**Attack demos** (also test scenarios): (1) talking the agent into it, (2) an injected
receipt, (3) a replayed request, (4) the guardian revokes the delegation mid-demo, (5) the
agent skips the audit, (6) a sibling reuses a paid receipt.

---

## 14. Flows

### 14.1 Guardian onboarding
1. Sign in (§7.2).
2. Register a dependent: `register-dependent` signature → `POST /families`. More dependents
   later the same way, on the same reservoir.
3. Policy interview with the agent → a draft with test scenarios → sign `activate-policy`
   → active.
4. Sign the SPL `approve` in Phantom (delegate = agent wallet, amount = reservoir). A top-up
   approves the new total; `revoke` stops everything without the app.

### 14.2 Dependent request
1. The dependent asks; the gateway stores the request and runs the agent.
2. The agent may ask for proof (the dependent uploads from the in-app camera) or ask the
   guardian (the guardian answers with a signature).
3. The agent requests a transfer → Auditor if required → engine → signer → chain.
4. The agent's reply and the result go to the app (response and SSE).

### 14.3 Agent-initiated
Scheduler jobs (§10) use the same platform API and engine path as 14.2 step 3.

### 14.4 Guardian decisions
Answers to questions and the freeze switch are wallet-signed (§7.5) and verified by the
gateway; the engine re-verifies approvals on every transfer.

---

## 15. Decisions

| Topic | Decision |
|---|---|
| Client | Native iOS app only; Android out of scope |
| Chain | Solana devnet with SPL delegation (a card-processor alternative was considered and rejected) |
| Several dependents per guardian | Supported: one family per dependent; all share the guardian's one reservoir and delegation |
| Multiple guardians | Out of scope: a dependent has exactly one guardian |
| Emergency with no guardian answer | Wait; no automatic emergency amount |
| Dependent's view of the policy | Outer limits only, never the guidance |
| Notifications | In-app and SSE only |
| Review threshold | The guardian's `review_threshold`, else 25% of the weekly cap |
| Auditor model | Same model family as the maker, separate prompt and token; configurable per agent |
| Allowance cap | The signed allowance amount; raising it needs the guardian |
| Guardian override scope | Exactly the signed amount, past the amount cap, weekly cap and review only |
| Sign-in method | Phantom `signMessage` over the gateway's text (not Phantom `signIn`) |
| Receipts | In-app camera with App Attest, or a signed PDF for an online order (verified against `PDF_TRUSTED_ROOTS_PATH` and `PDF_ALLOWED_SIGNERS`); with `CAMERA_ONLY_PROOFS`, only verified captures count among images |
| When proof is needed | After payment: money goes out first (after an Auditor review) and the receipt is owed; at 3 owed receipts no further request is served until one arrives. Proof first stays possible when something already exists to show (a quote, an online order's PDF) |
| Custodial fallback | Not implemented |

To change a decision: a `[SPEC]` commit updating this table.

---

## 16. Out of scope

Mainnet, real money and fiat, KYC, multiple guardians, an Android app, push notifications,
LINE/Telegram bots, on-chain spending-limit programs, KMS/HSM (production path only).

---

## 17. Working rules (people and AI agents)

1. Read this file and the source for the part you touch (§0) before every task.
2. Stay in your layer. A change to another layer's contract needs that owner's OK.
3. Contracts are frozen: an API shape, DB column, enum, env var or signed text changes only
   together with its source document, in a commit titled `[SPEC] …`.
4. Never weaken a `SEC-xx` requirement, even to make a test or demo pass. If one blocks
   you, stop and ask.
5. Money is integer base units; no floats in money paths.
6. The LLM is untrusted: its output never chooses a recipient, moves money, changes policy
   or skips a check.
7. No secrets in code, logs, prompts or commits; use environment variables.
8. Every change ships with tests; a security check needs a test that proves the attack
   fails.
9. When unsure, choose the stricter behaviour (deny, escalate, refuse) and say so.
10. Keep code simple; engineer carefully only the security-critical parts (engine, budget
    transaction, signatures and nonces, idempotency, audit log, DB permissions).

**Definition of done:** the docs it touches are updated; ruff, mypy (strict) and tests pass;
new logic has tests; SEC tests untouched or strengthened; no secrets, floats in money paths
or address fields; the commit or PR says which sections it implements.

---

## 18. Milestones

| # | Goal | Done when |
|---|---|---|
| M0 | Contracts frozen | Agent and crypto contracts agreed; mocks run in docker-compose |
| M1 | Vertical slice (mock chain) | Dependent asks → agent → engine → mock crypto → app shows the result |
| M2 | Real devnet | Mint, delegation, real `transfer_checked`, signatures visible in Solana Explorer |
| M3 | Auditor and security | Auditor live; all SEC tests green; attack demos scripted |
| M4 | Demo polish | Seed script, demo walkthrough, weekly summary, learning loop |

---

## 19. Changelog

| Date | Version | Change |
|---|---|---|
| 2026-10-17 | 1.0 | Initial spec from the architecture doc |
| 2026-10-18 | 1.0.1 | The client is a native mobile app, not a web app |
| 2026-10-18 | 1.0.2 | The mobile app is iOS only; Android out of scope |
| 2026-10-19 | 1.0.3 | A guardian may have several dependents; multiple guardians stay out of scope |
| 2026-10-19 | 1.0.4 | The signer also checks the source token account; `registered_destinations` exposes it |
| 2026-10-19 | 2.0 | Rewritten for the four-service split. This repo's docs are the project's single source of truth (§0). The policy schema, categories, outcomes, interfaces, engine, data model and agent behaviour now match what is built; signer idempotency comes before its caps; sign-in uses `signMessage`; the SEC table has owners and status |
| 2026-10-19 | 2.1 | Proof comes after payment: a request at or above `proof_threshold` is sent first and owes a receipt, which blocks further requests until settled; PDF receipts accepted for online orders (not yet checked) |
| 2026-10-19 | 2.2 | PDF receipts from online orders are accepted only when the shop's signature over the file verifies; an unsigned PDF is stored but never reaches the model |
| 2026-10-19 | 2.3 | Agent contract: `Notification.dependent_id` (a guardian note about one dependent, filed under that family) and the PDF flags `issuer_signed` / `unchecked_document`. A PDF's signer must match an allowed shop name exactly (default `LAZADA EXPRESS COMPANY LIMITED`, checked against a real receipt) and its certificate must be meant for signing; BER-encoded signatures are no longer broken by padding removal |
| 2026-10-19 | 2.3.1 | SEC-03, SEC-13 and SEC-14 marked tested: implemented and covered by tests in `sigil-crypto` |
| 2026-10-19 | 2.4 | One monorepo (`Team-Sigil/sigil`): `gateway/`, `agent/` and `crypto/` with their full history, and the stack scripts at the root. Each layer keeps its own package, lockfile, Dockerfile and tests |
| 2026-10-19 | 2.4.1 | SEC-18 met: CI runs every layer's tests and a gitleaks scan of the whole history on each push and pull request |
| 2026-10-20 | 2.5 | Proof after payment end to end: the agent pays first (after an Auditor review, new trusted trigger `pay_first`), sees owed receipts (`proofs_owed`, `proof_owed`), checks an arriving receipt with `report_receipt` and the gateway's settle route, and ends with `receipt_accepted` / `receipt_rejected`. A dependent may owe up to 3 receipts before requests stop being paid |
