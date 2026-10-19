# Sigil: what it does

A feature-level tour of the whole system, across all four layers. It says what exists today
and what does not, and links to the document that owns each detail rather than repeating it,
so there is one place to change when something moves.

> **Source of truth.** [`sigil-gateway/SPEC.md`](sigil-gateway/SPEC.md) and the documents it
> points to (its §0) are authoritative for the whole project. The `SPEC.md`,
> `System Architecture.md` and `GUIDELINES.md` sitting next to this file are the **original
> v1.0 copies and are out of date** — they still describe a web app, thirteen categories,
> one dependent per guardian and an emergency fund that skips the limits. Read the gateway's
> copies instead.
>
> Status below is as of **19 October 2026**. Where something is on an unmerged branch, it
> says so.

---

## In one paragraph

A guardian funds one reservoir in their own Phantom wallet and describes, in plain language,
how each person they support should be helped with money. An AI **agent** then runs each
dependent's allowance day to day: it judges requests, negotiates, asks for receipts, paces
the budget, sends the weekly allowance, asks the guardian when it should, and learns from
the answers. A second, independent **Auditor** agent reviews the larger and riskier
decisions as the guardian's advocate. Deterministic code — the **policy engine** and the
**signer** — guarantees nothing moves beyond the limits the guardian signed, and only to
the dependent's registered account. Money is an SPL token on Solana devnet, moved by
delegation, so the app never holds funds.

**The claim the whole design exists to support:** even a fully manipulated agent cannot move
more than the guardian allowed, to anyone other than the dependent.

---

## The four layers

| Layer | Owner | Repo | State |
|---|---|---|---|
| App | Shine | `Team-Sigil/sigil-ios` | Native iOS, SwiftUI. **Screens only** — mock data, no API calls, no wallet yet |
| Agent | Alwin | `Team-Sigil/agent-layer` | Maker, Auditor, scheduler, policy interview, guardrails. Built and tested |
| Gateway | Julian | `Team-Sigil/sigil-gateway` | App API, platform API, policy engine, database, audit log. Built and tested |
| Crypto | Han | — | Signer, delegation reads, `transfer_checked` on devnet. **Not built yet**; a mock stands in |

Detail: [SPEC §3](sigil-gateway/SPEC.md), [architecture.md](sigil-gateway/docs/architecture.md).

---

## What a guardian can do

| Feature | Where it lives | State |
|---|---|---|
| Sign in with their wallet, no password | Gateway `/auth/*`, [auth-guide.md](sigil-gateway/docs/auth-guide.md) | Gateway done; app not wired |
| Register a dependent by signing a message naming their wallet and account | `POST /families` | Done |
| Support **several dependents**, each with their own policy, budget, freeze and history | One family per dependent | Done |
| Fund one reservoir shared by all of them, by signing an SPL `approve` in Phantom | Crypto layer | Not built |
| Set the policy **by chatting with the agent**, not by filling in a form | `/policy/interview`, [policy-protocol.md](agent-layer/policy-protocol.md) | Done |
| Review the draft with test scenarios ("if X asks for Y, I would Z") before agreeing | Interview `ready` state | Done |
| Activate a policy by signing its hash, so nothing takes effect unsigned | `activate-policy` signature | Done |
| Answer the agent's questions with a signature, seeing both the agent's and the Auditor's view | `/questions`, `answer-question` signature | Done |
| Freeze one dependent instantly, with a signature | `/freeze`, per family | Done |
| See every decision, its reason, the policy version and the on-chain signature | `/history`, `/audit` | Done |
| Read a weekly summary the agent writes | Scheduler | Done |
| Get the agent's "stop asking me about books" proposals | `/policy/proposals` | Listed, not one-click adoptable |

## What a dependent can do

| Feature | Where it lives | State |
|---|---|---|
| Sign in with their own wallet | Gateway `/auth/*` | Gateway done; app not wired |
| Ask for money in a few taps: an amount, what it's for, optionally a note | App `AskSheet` | App built (branch), not wired |
| Say it is an emergency, which jumps the queue without lifting any limit | `emergency_claimed` | Done |
| Get an answer in their own words from the agent, not a reason code | `decision.reply_to_dependent` | Done |
| Take a counter-offer or send it to their guardian, in one tap | App `LiveRequestCard` | App built (branch) |
| Send a receipt with the app's own camera, signed by the device | App Attest capture claims | Gateway done; app not wired |
| Send the PDF of an online order's receipt instead | Gateway `proof_pdf.py` | **On an unmerged branch** |
| See their own limits — never the guardian's private guidance | `/policy/active` | Done |
| See what they have left this week and what they asked for | `/budget`, `/requests` | Done |

## What the agents do

| Feature | Where it lives | State |
|---|---|---|
| Judge each request against the guidance, the budget and the history | Maker | Done |
| Send the full amount, part of it, counter-offer, ask for proof, ask the guardian, or decline | Maker outcomes | Done |
| Review the larger and riskier decisions independently, as the guardian's advocate | Auditor | Done |
| Form its own view **before** reading the maker's reasoning, and never approve or raise | Auditor | Done |
| Send the weekly allowance on its own | Scheduler | Done |
| Warn when spending is running fast, and ask the guardian to top up before the reservoir runs dry | Scheduler | Done |
| Write the weekly summary and propose policy updates from how the guardian answered | Scheduler | Done |
| Speak English, Simplified Chinese or Traditional Chinese, matched to each reader | [i18n](agent-layer/README.md) | Done |
| Treat everything a dependent or a receipt says as data, never as instructions | Guardrails | Done |

## What the deterministic core guarantees

These are the parts no model can talk around. Detail: [SPEC §8](sigil-gateway/SPEC.md),
[README "The policy engine"](sigil-gateway/README.md).

- **Outer limits:** weekly cap, maximum per request, blocked categories, proof threshold,
  review threshold, and the signed allowance amount.
- **One recipient:** money reaches only the dependent's registered account, from the
  guardian's reservoir. No tool, prompt or payload anywhere has an address field.
- **Signatures, not claims:** a policy, an approval, a freeze and a registration count only
  as a wallet signature over a fixed text. "Mom said yes" in chat is nothing.
- **A required Auditor verdict must exist** before a reviewed transfer clears, and only the
  Auditor's own token can post one.
- **One idempotency key, at most one transfer** — including after a crash or a retry.
- **Budgets and the shared reservoir move under locks**, in one transaction.
- **One receipt backs one payment**, across a guardian's dependents and across all families.
- **The signer re-checks everything itself** — destination, source, its own caps, the live
  delegation — before it will sign.
- **The chain is the last word:** the token program refuses anything beyond the delegation,
  and the guardian can revoke it in Phantom without the app.
- **Default deny:** missing or unverifiable facts block.
- **An append-only, hash-chained audit log**, re-verified end to end on every read.

## Proof: how receipts work

Currently on an unmerged branch (`feat/proof-after-payment` in the gateway,
`ui/proof-after-payment` in the app), because the first version required the receipt before
the money, which nobody without their own money can produce.

- A request at or above the proof threshold is **sent first**, and the dependent then owes a
  receipt.
- Until that receipt is accepted, they cannot ask for more: `409 PROOF_OUTSTANDING`.
- A paper receipt is photographed **in the app's own camera**, with no gallery picker, and
  signed by the device key so it cannot be reused or substituted.
- An online order's receipt arrives as the **PDF the shop sent**, and is accepted only if the
  shop's digital signature over the file verifies against a configured certificate. An
  unsigned PDF is stored, counts for nothing, and is never shown to the model.
- The receipt's findings must cover the amount, and it can only ever be spent once.

**Blocked on:** the agent layer still refuses to request a transfer without proof in hand
(`guardrails/tools.py`), so this flow does not work end to end until that changes too.

---

## Honest status

| | |
|---|---|
| **Works today** | The gateway and the agent layer, together, against a mock crypto layer: a dependent can ask, the agent can decide, the Auditor can flag, the engine can clear or block, and everything is logged |
| **Mocked** | Money. `mock-crypto` returns fake signatures; nothing has touched devnet |
| **Not started** | The crypto layer: the signer, the delegation reads, the real `transfer_checked` (SEC-03, SEC-13, SEC-14 are all "to build") |
| **Screens only** | The iOS app. Every screen renders from mock data; no sign-in, no API calls, no Phantom, no camera |
| **Known gaps** | Listed in [README "Known gaps"](sigil-gateway/README.md): one gateway instance, no C2PA or AI-image detection, synchronous agent calls, policy proposals not adoptable in one click |

## Where the detail lives

| Topic | Document |
|---|---|
| Product, conventions, policy, security requirements, decisions | [sigil-gateway/SPEC.md](sigil-gateway/SPEC.md) |
| Why it is built this way; threats and controls; user flows | [sigil-gateway/docs/architecture.md](sigil-gateway/docs/architecture.md) |
| Every app-facing route, field and error | [sigil-gateway/README.md](sigil-gateway/README.md) "App API" |
| The policy engine's checks, in order | [sigil-gateway/README.md](sigil-gateway/README.md) "The policy engine" |
| Building the app against the gateway | [app-handoff.md](sigil-gateway/docs/app-handoff.md), [auth-guide.md](sigil-gateway/docs/auth-guide.md) |
| What the agents do, and their guardrails | [agent-layer/README.md](agent-layer/README.md), [guide.md](agent-layer/guide.md) |
| A runnable end-to-end narrative of the agent layer | [agent-layer/walkthrough.md](agent-layer/walkthrough.md) |
| Receipts, integrity flags and verified capture | [agent-layer/proof-images.md](agent-layer/proof-images.md) |
| The policy interview | [agent-layer/policy-protocol.md](agent-layer/policy-protocol.md) |
