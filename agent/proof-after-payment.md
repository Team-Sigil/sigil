# Proof after payment, end to end

For the team (gateway and agent layers). Status: **built (SPEC 2.5, 2026-10-20)**, with
these decisions:

| Decision | Taken |
|---|---|
| 1. Pay first or proof first | Pay first by default; proof first stays possible when something already exists to show |
| 2. S1 | Every pay-first payment is reviewed (trusted trigger `pay_first`, in the engine and the agent) |
| How many owed receipts | Up to **3** (`MAX_OPEN_OBLIGATIONS` in the gateway, `max_proofs_owed` in the contract); at 3, requests get no more money |
| 3. S2, 4. S3 | Not built yet: overdue receipts aren't reported and can't be waived |

The contract below landed as proposed (`ProofOwed`, `proofs_owed` and `max_proofs_owed` on
the context, `proof_owed` on the request and the transfer result, `SettleObligation`,
`SettledObligation`, outcomes `receipt_accepted` / `receipt_rejected`). The "settle run" is
the maker's receipt mode, with the `report_receipt` tool. The rest of this note is the
original proposal, kept as the design record.

## What the gateway does today

- A request at or above `proof_threshold` is **paid first** and leaves a receipt owed (a
  *proof obligation*).
- While one is owed, the dependent's further requests are refused: `409 PROOF_OUTSTANDING`
  in the app, `proof outstanding` in the engine. The scheduled allowance still goes out. A
  guardian override doesn't lift it.
- `POST /agent/v1/proof-obligations/{id}/settle` closes one: the agent names the proof and
  what it read; the gateway applies the same proof rules as the engine (belongs to the
  request, counts as proof, unused across the family and all families, findings cover the
  amount).
- A transfer that already carries a counting proof leaves nothing owed, so the old "proof
  first" flow still works. It's what the agent does today.

## The trade-off to confirm

Proof first protected the money: nothing above the threshold went out on a story alone.
Proof after payment protects the **next** payment instead: one unverified payment, up to
`max_per_request`, then nothing more until a receipt arrives. It fits how buying works (you
can't get a receipt before you've paid), but it changes what the guardian's "receipt
needed from 25" means. Two gaps follow from it:

1. **A pay-first payment can skip the Auditor.** The review threshold defaults to 25% of
   the weekly cap and the guardian can set it higher than the proof threshold. A payment
   between the two goes out with no proof and no review.
2. **A lost receipt blocks the dependent indefinitely.** Nothing ends an obligation except a
   valid receipt, and the guardian has no way to waive it. Nothing tells the guardian it's
   overdue either.

## Proposal

### 1. Safeguards

| # | Change | Layer |
|---|---|---|
| S1 | **Every pay-first payment is reviewed.** New trusted review trigger `proof_owed`: a request at or above `proof_threshold` without a counting proof attached. It goes in `trusted_triggers`, so the engine and the agent apply it identically (the parity test covers it) | agent `auditor/triggers.py`, gateway `engine/triggers.py` |
| S2 | **Overdue receipts reach the guardian.** After `PROOF_DUE_HOURS` (proposed 72), the gateway files a guardian question: "Mali hasn't sent the receipt for 30.00 (calculator) from Monday." | gateway job |
| S3 | **The guardian can waive an owed receipt** with a wallet signature (`FinGuard:waive-proof`, `obligation:{id}`, `nonce`). Waived obligations stay in the audit log and the weekly summary | gateway, app |

### 2. Contract additions (`contracts.py`, both repos)

```python
class ProofOwed(_Lenient):
    """A receipt the dependent owes for money already sent."""
    obligation_id: str
    request_id: str
    amount: int = Field(gt=0)          # what the receipt must cover
    purpose: str = Field(default="", max_length=MAX_SHORT_TEXT)
    opened_at: datetime


class DependentRequest(_Lenient):
    ...
    proof_owed: ProofOwed | None = None   # owed for *this* request


class AgentContext(_Lenient):
    ...
    proofs_owed: list[ProofOwed] = Field(default=[], max_length=MAX_ITEMS)  # any request


class TransferResult(_Lenient):
    ...
    proof_owed: ProofOwed | None = None   # set when this transfer left a receipt owed


class SettleObligation(_Strict):          # POST /proof-obligations/{id}/settle
    proof_id: str
    findings: ProofFindingsRecord


class SettledObligation(_Lenient):
    settled: bool
    reason: str | None = Field(default=None, max_length=MAX_SHORT_TEXT)
```

`SettleObligation` and `SettledObligation` exist today only in the gateway's
`agent_api/service.py`. They're platform API payloads, so they belong in the shared
contract. Outcomes gain two code-only values, `receipt_accepted` and `receipt_rejected`.

### 3. What the agent does

| Situation | Today | Proposed |
|---|---|---|
| Request at or above the threshold, no proof | Asks for proof first (`proof_required`) | May pay first: the Auditor reviews it (S1), then the reply says a receipt is owed for exactly that amount. Still asks for proof first when the dependent already has one (an online order's PDF, a quote) |
| The payment left a receipt owed | — | Outcome `sent_full` / `sent_partial` as now; the run notes `proof_owed` from `TransferResult`, and the fixed reply says what's owed |
| A new request while a receipt is owed elsewhere (`ctx.proofs_owed`) | Engine blocks it as `proof outstanding`; the agent says "blocked" | Fails early in the tool guard, and the reply asks for the owed receipt by name (purpose, amount) |
| The dependent uploads the owed receipt (`request.proof_owed` set, a counting proof attached) | — | A **settle run**: the model reads the proof and calls a new tool `report_receipt(proof_findings)`. Code picks the proof (as for transfers, never the model) and calls `settle`. The outcome is `receipt_accepted` or `receipt_rejected` from the gateway's answer, never the model's say-so, with the gateway's `reason` turned into the reply ("this receipt was already used", "it doesn't cover 30.00") |

The model never decides that a debt is paid: code calls `settle`, and the gateway decides.
`report_receipt` is only offered in a settle run, like `ask_for_proof` only in request runs.

## Decisions needed

1. **Pay first, or proof first?** Keep pay-first as the default (my recommendation, with
   S1–S3), make it a guardian choice in the signed policy (`proof_timing: "before" |
   "after"`, a policy schema change), or go back to proof first.
2. **S1:** review every pay-first payment, or only above the review threshold?
3. **S2:** how long before an owed receipt counts as overdue (72 hours?), and does it also
   go to the dependent as a reminder?
4. **S3:** can the guardian waive an owed receipt, or only answer the overdue question?
5. **Contract:** agree the additions above, then both repos change together (the parity
   test enforces it).

Once these are settled, the agent work is about a day: the contract fields, the settle run
and tool, the tool guard and review trigger, prompts and fixed replies in three languages,
the mock engine as reference, and tests.
