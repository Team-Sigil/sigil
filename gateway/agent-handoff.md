# Agent layer handoff: several dependents per guardian

For Alwin, from the gateway (Julian). Gateway branch `feat/multi-dependent`; checked against
agent-layer `main` at `ded5d75`.

**Short version: nothing has to change.** The Platform API contract (`contracts.py`) is
unchanged, and the agent layer already treats a guardian as having several dependents. This
note says what the gateway now guarantees, and proposes one optional contract addition.

## What changed in the gateway

- A guardian may have several dependents, one family each (own policy, weekly budget,
  freeze, questions, policy interview, history). A dependent still has exactly one guardian.
  Multiple guardians stay out of scope (SPEC §17, §18).
- **One reservoir per guardian.** All of a guardian's dependents must use the same guardian
  token account, so there is one delegation per guardian. Registering a dependent with a
  different token account is refused.
- **The engine counts the whole reservoir.** "Insufficient delegation" now subtracts money
  pending for *any* of the guardian's dependents, under a lock on the token account, so two
  siblings asking at once cannot overspend the delegation. Weekly caps stay per dependent.

## What the agent layer can rely on

| Agent layer code | Gateway behaviour | Status |
|---|---|---|
| `reservoir_check` reads `contexts[0].delegated_amount` as the one shared delegation | `AgentContext.delegated_amount` is the guardian's delegation minus money pending for all of their dependents, so it is the same for every sibling | matches |
| `TokenBudget(api, ctx.guardian_id)`; `UsageReport.family_id` = guardian id | Usage is counted per guardian per day, across all their dependents | matches; see note 1 |
| `learning_proposals` groups `get_guardian_answers` by `(dependent_id, category)` | `/guardians/{id}/answers` returns every dependent's answers, each with its own `dependent_id` | matches |
| `GuardianQuestion`, `PolicyProposal`, `PolicyDraftSubmission` carry `guardian_id` + `dependent_id` | Filed under that dependent's family; refused if the dependent isn't the guardian's | matches |
| `list_dependents()` | One row per dependent, oldest family first; siblings share `guardian_id` | matches |
| Guardrail `over_reservoir` uses `ctx.delegated_amount` | Same reservoir-wide figure as the engine uses | matches |
| `Notification(audience="guardian", recipient_id=guardian_id)` | Stored as a guardian-wide note (`family_id` null in the app), pushed to every family's event stream | works; see proposal A |

**Note 1, token budget.** `LLM_TOKEN_BUDGET_PER_FAMILY_PER_DAY` (default 200,000) is now
shared by all of a guardian's dependents, because the usage "family" is the guardian. If a
guardian with three dependents runs out too early in the demo, we raise the default on our
side; tell us if you'd rather have it per dependent.

**Note 2, reservoir top-up question.** `reservoir_check` files the top-up question under
`contexts[0].dependent_id`. Because `list_dependents()` is ordered oldest first, that is the
guardian's first dependent, and the app shows it there. Please keep it deterministic (don't
reorder or pick by dict order).

## Proposal A (optional contract addition): `dependent_id` on guardian notifications

Some guardian notes are about one dependent: the weekly summary (`weekly_summary` in
`flows/scheduled.py`) and the maker's `notify_guardian` tool (`maker/agent.py`). Today they
reach the guardian without saying which dependent they are about, so the app files them as
guardian-wide. The text usually names the dependent, but the app can't link the note to the
right dependent's screen.

Proposed change to `contracts.py` (backwards compatible, defaults to the current behaviour):

```python
class Notification(_Strict):
    audience: Literal["dependent", "guardian"]
    recipient_id: str
    message: str
    dedupe_key: str | None = None
    # For a guardian note about one dependent; None = about all of them (e.g. the reservoir).
    dependent_id: str | None = None
```

Where you would set it:

- `weekly_summary`: `dependent_id=dependent_id`
- `notify_guardian` in `maker/agent.py`: `dependent_id=run.ctx.dependent_id`
- leave it `None` for anything about the shared reservoir

On our side: we mirror the field, check that the dependent belongs to that guardian, and
file the note under that dependent's family. This needs your OK, because it changes your
contract. Until then, nothing breaks.

## Suggested test in the agent layer

The mock platform already keys delegations and used proofs by guardian, so sibling behaviour
is modelled. A second seeded dependent for `g1` (in `mock_platform/seed.py`) would let your
tests cover `reservoir_check` and `learning_proposals` with two siblings.

## Known difference (gateway side, being fixed)

Your mock counts a used proof (id, SHA-256, receipt fingerprint) once per guardian. The
gateway counts proof ids and SHA-256 per family, and only the keyed receipt fingerprint
across all families. So siblings uploading the same image file is caught only when the agent
reports `proof_findings`. We will widen the check to the guardian's families to match your
mock. No change is needed from you.
