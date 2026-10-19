# CLAUDE.md

This folder is the **gateway layer** of Sigil, owned by Julian, and its docs are the **single
source of truth for the whole project**. Sigil is four services that talk over HTTP:

| Layer | Owner | Folder |
|---|---|---|
| App (native iOS) | Shine | — (separate repo) |
| Agent (maker, Auditor, scheduler) | Alwin | `agent/` |
| Gateway (this folder: app API, platform API, policy engine, DB) | Julian | `gateway/` |
| Crypto (signer, Solana devnet) | Han | `crypto/` |

## Sources of truth

SPEC.md §0 says where each fact lives. In short:

- **SPEC.md:** product, architecture, conventions, policy, security requirements (`SEC-xx`),
  decisions, working rules.
- **README.md:** this layer's interfaces in full (App API, Platform API, crypto contract),
  the policy engine's checks, data, configuration, running.
- **`src/gateway/contracts/`:** the exact shapes. `contracts/agent.py` is a verbatim copy of
  the agent layer's `contracts.py`, kept identical by a parity test.
- **docs/:** the design rationale (`architecture.md`) and one guide per layer
  (`app-handoff.md`, `auth-guide.md`, `agent-handoff.md`, `crypto-handoff.md`). Guides
  follow SPEC and README; if they disagree, fix the guide.

@SPEC.md

## Working rules

- Stay inside `gateway/`. A change to another layer's contract needs that owner's OK, and
  lands here together with its docs in a `[SPEC]` commit.
- Never weaken a `SEC-xx` requirement (SPEC §13). If one blocks you, stop and ask a human.
- Money is integer base units (6 decimals), never floats.
- LLM output is untrusted: nothing from the agent layer may choose a recipient or skip a check.
- If the docs are silent, ask; do not invent behaviour.
- Every change ships with tests; security checks need a test proving the attack fails.
- Keep code simple; engineer carefully only the security-critical parts (engine, budget
  transaction, signatures and nonces, idempotency, audit log, DB permissions).
- When behaviour changes, update the doc that owns it (SPEC §0) in the same change.
- Follow GUIDELINES.md for code style.
