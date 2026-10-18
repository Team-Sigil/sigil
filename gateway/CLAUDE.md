# CLAUDE.md

This repo is the **gateway layer** of Sigil, owned by Julian. Sigil is four services that
talk over HTTP:

| Layer | Owner | Repo |
|---|---|---|
| App (web) | Shine | — |
| Agent (maker, Auditor, scheduler) | Alwin | `Team-Sigil/agent-layer` |
| Gateway (this repo: public API, platform API, policy engine, DB) | Julian | `sigil-gateway` |
| Crypto (signer, chain) | Han | — |

Read README.md for this layer's responsibilities and interfaces.

## Sources of truth

1. **Agent ↔ gateway:** the agent layer's `contracts.py` and README ("Platform API contract",
   "Inbound API"). Our `/agent/v1/*` routes match them exactly.
2. **App ↔ gateway and gateway ↔ crypto:** this repo's README.
3. **Everything else** (security requirements, money rules, flows): SPEC.md, which predates the
   split into services. Where SPEC disagrees with 1 or 2, those win.

@SPEC.md

## Working rules

- Stay inside this repo; changes to another layer's contract need that owner's OK.
- Never weaken a `SEC-xx` requirement (SPEC §15). If one blocks you, stop and ask a human.
- Money is integer base units (6 decimals), never floats.
- LLM output is untrusted: nothing from the agent layer may choose a recipient or skip a check.
- If the contracts and SPEC are silent, ask; do not invent behavior.
- Every change ships with tests; security checks need a test proving the attack fails.
- Keep code simple; engineer carefully only the security-critical parts (engine, budget
  transaction, signatures and nonces, idempotency, audit log, DB permissions).
- Follow GUIDELINES.md for code style.
