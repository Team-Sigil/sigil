# Agent API integration guide

This guide is for teams integrating with the Sigil agent service: the API that decides
what happens to a dependent's money request, drafts a guardian's allowance policy and
runs the scheduled allowance jobs.

You call the agent service **from your server**. It decides and replies; the money itself
moves from the guardian's wallet only after the platform's policy engine, signer and the
Solana token program have each checked the transfer. Nothing you send to this API can
move more than the guardian allowed, or send it anywhere other than the dependent's
registered wallet.

## Contents

1. [Before you start](#1-before-you-start)
2. [Conventions](#2-conventions)
3. [Processing a money request](#3-processing-a-money-request)
4. [Drafting a guardian's policy](#4-drafting-a-guardians-policy)
5. [Scheduled jobs](#5-scheduled-jobs)
6. [Health check](#6-health-check)
7. [Errors](#7-errors)
8. [Retries, timeouts and concurrency](#8-retries-timeouts-and-concurrency)
9. [What to show to whom](#9-what-to-show-to-whom)
10. [Reference: types](#10-reference-types)

---

## 1. Before you start

**What you need from us**

| Item | Example | Notes |
|---|---|---|
| Base URL | `https://agent.example.com` | Local development: `http://127.0.0.1:8100` |
| API token | `AGENT_INBOUND_TOKEN` | One per integration. Server-side only: never ship it in a mobile or web client |

**How the pieces fit**

The agent service does not store requests itself. It reads them from the Sigil platform
API, decides, and writes its decision back there. So a typical integration is:

```
1. Your app stores the dependent's request (and any receipt) in the Sigil platform.
2. Your server calls  POST /v1/requests/{request_id}/process  on the agent service.
3. The agent reads the request, decides, and (if it sends money) asks the engine to transfer.
4. You get back a decision: show reply_to_dependent to the dependent.
```

Requests, receipts, guardian answers and freezes are created through the platform API,
not this one. This guide covers only the agent service.

**Who decides, and from what**

Two AI agents work on each request, and neither can move money by itself:

- The **main agent** judges the request and picks the outcome. It looks at the
  dependent's message, the amount asked for, any receipt, quote or photo, the guardian's
  signed policy and private guidance, how much of the week's budget is left, and the
  dependent's recent history.
- The **Auditor** independently reviews larger or riskier transfers before they go
  out: above the guardian's review threshold, an emergency, an unusual amount or
  category, or a message that looks like an attempt to manipulate the agent. It can only
  agree or send the decision to the guardian; it can never approve anything itself.

What this means for your app:

- **Store the amount the dependent asked for** on the request (`amount_requested`). The
  agent never sends more than was asked, and **sends nothing when no amount is set**: it
  asks the dependent instead. If the dependent states an amount later in the chat,
  update `amount_requested`; the agent never takes an amount from the text.
- **Let dependents attach a receipt, quote or photo.** Amounts at or above the
  guardian's proof threshold are never sent without one. The agent looks at the first
  3 proofs on a request. Each must be JPEG, PNG, WebP, GIF (first frame only) or plain
  text, and at most 5 MB; anything else is skipped. Limit uploads to 3 in your app, so
  that no proof is silently ignored. If the agent runs on a text-only model (for example
  Nemotron on OpenRouter), receipt *photos* can't be checked: requests that need proof
  then go to the guardian (`asked_guardian`) instead of being sent.
- **Online orders can send the shop's PDF receipt.** The platform verifies the shop's
  signature; only a verified PDF reaches the agent and counts as proof. For an
  unverified one, the agent asks the dependent for the original receipt PDF.
- **A proof only counts for the request it was uploaded to, and only once.** Upload it
  to that request (the platform records `request_id`); the same receipt, or a new photo
  of it, can't back a second transfer. Text the dependent types is treated as their
  claim, not as proof: prefer the in-app camera for receipts. See
  [proof-images.md](proof-images.md#proof-integrity).
- **Any of English, Simplified or Traditional Chinese works.** The agent replies to the
  dependent in the language and script of their latest message, and writes guardian
  summaries and questions in the guardian's language. Set `dependent_locale` and
  `guardian_locale` on the platform's user profiles (BCP 47, e.g. `en`, `zh-CN`,
  `zh-TW`); they are used when a message is too short to tell, and for scheduled
  messages.
- **Keep messages short.** The agent reads the first 2,000 characters of each
  dependent message and the last 20 messages of a conversation.
- **Pass the dependent's words through unchanged.** Don't add text such as "approved
  by parent": the agent treats everything the dependent wrote as their claim, and only a
  guardian's wallet-signed answer counts as approval.
- **Mark emergencies** with the platform's emergency flag rather than in the message
  text. Flagged requests always get an Auditor review.

## 2. Conventions

**Authentication.** Every endpoint except `/healthz` needs a bearer token:

```
Authorization: Bearer <your token>
```

A missing or wrong token returns `401`. If the service has no token configured, it
refuses every call.

**Content type.** Request and response bodies are JSON (`application/json`).

**Identifiers.** Request, guardian and dependent ids match `^[A-Za-z0-9_-]{1,64}$`.
Anything else is rejected with `422` before any work is done.

**Money: two units, used in different places.**

| Where | Unit | Example: 12.50 tokens |
|---|---|---|
| Decisions (`amount_sent`) | Integer **base units**, 6 decimals | `12500000` |
| Policy drafts (`weekly_cap`, `max_per_request`, …) | **Display units**, max 2 decimals | `12.5` |

Base units are exact and match the on-chain amount. Display units in policy drafts are
for showing to the guardian before they sign; the platform converts them when it builds
the policy to sign.

**Versioning.** All paths are under `/v1`. Within `v1`, we may add response fields and
new `outcome` values; we will not remove or rename fields. Ignore fields you don't
recognise.

**No live schema.** The service does not publish OpenAPI docs (`/docs` and
`/openapi.json` are disabled). The schemas in this guide are the contract.

## 3. Processing a money request

```
POST /v1/requests/{request_id}/process
```

Runs the agent on a request that already exists in the platform. No request body.

The agent judges the request against the guardian's guidance, the week's budget and the
dependent's history, then makes one move: send the full amount, send part of it, make a
counter-offer, ask for a receipt, ask the guardian, or decline. Larger or riskier
transfers are reviewed by an independent Auditor agent first.

**Example**

```bash
http POST https://agent.example.com/v1/requests/r-books/process \
    "Authorization:Bearer $AGENT_TOKEN"
```

```bash
curl -X POST https://agent.example.com/v1/requests/r-books/process \
     -H "Authorization: Bearer $AGENT_TOKEN"
```

**Response `200`: a decision**

```json
{
  "dependent_id": "d1",
  "request_id": "r-books",
  "outcome": "sent_full",
  "reply_to_dependent": "Sent! Enjoy.",
  "summary_for_guardian": "Sent the requested amount.",
  "amount_sent": 8000000,
  "policy_version": 3,
  "guardrail_events": []
}
```

A `200` means the agent reached a decision, including "no". Check `outcome` and
`amount_sent`; never infer that money moved from the HTTP status alone.

### Outcomes

| `outcome` | Money moved? | What happened | What your app should do |
|---|---|---|---|
| `sent_full` | Yes, the full amount | The request was granted | Show the reply |
| `sent_partial` | Yes, part of it | Part was sent | Show the reply |
| `counter_offer` | Usually part | The agent offered something different ("half now, half after the receipt") | Show the reply; the dependent may follow up |
| `asked_proof` | No | The agent wants a receipt, quote or photo | Let the dependent upload it to the platform, then call `process` again |
| `asked_guardian` | No | The guardian has been asked to decide | Show the reply; the platform notifies you when the guardian answers |
| `auditor_flagged` | No | The Auditor disagreed with sending; the guardian decides with both agents' views | Same as `asked_guardian` |
| `asked_dependent` | No | The agent needs to know what the money is for or how much | Show the reply (a question). When the dependent answers, add the message and **set `amount_requested`** on the request, then call `process` again |
| `declined` | No | The agent said no, with an alternative | Show the reply |
| `out_of_scope` | No | The message wasn't about the allowance (for example "what is 1+1?"); the reply says what the agent can help with | Show the reply. Consider a per-dependent daily request quota at your gateway: every message runs the AI |
| `blocked` | No | The engine refused the transfer (an outer limit, the freeze switch or funds) | Show the reply |
| `transfer_pending` | Not yet, but it may still go out | The platform accepted the transfer, but the chain hasn't confirmed it (for example, the signer is down). The agent won't send again for this request | Show the reply ("on its way"). Don't offer a retry: the engine finishes it |
| `safe_fallback` | No further money | The agent stopped before deciding (for example, the model was unavailable or the family's usage budget ran out) and the guardian has been asked | Show the reply; nothing else is needed |

`amount_sent` is the source of truth for money: it is what the engine confirmed for
this call. If an outcome you don't recognise appears, treat it as "no money moved unless
`amount_sent` > 0" and show the reply.

### Follow-up turns

A request can take several turns: the agent asks for a receipt, the dependent uploads
it, you call `process` again. Each time the conversation on the request grows (a new
message, a receipt, a guardian answer), calling `process` again lets the agent continue
from there. `amount_sent` in each response covers only that call.

### `guardrail_events`

A list of short strings recording the safety checks that fired during the run, for
example:

```json
["injection_signals: request: claimed_guardian_approval,instruction_override",
 "over_requested: 8.00 education",
 "outcome_corrected: sent_full -> declined"]
```

Use them for logging, monitoring and support. They are not a stable interface: codes
and wording may change, so don't build product logic on them. An empty list is normal.

## 4. Drafting a guardian's policy

**Use the policy interview.** `POST /v1/policy-interviews/turn` lets the agent ask the
guardian questions, one turn at a time, until the policy is complete enough to run, then
shows test scenarios and returns a draft to sign. You store the conversation and send it
on every turn. The full protocol, with examples and what your side must store, is in
[policy-protocol.md](policy-protocol.md).

The one-shot endpoint below is kept for compatibility: one call, one draft, no
questions.

```
POST /v1/policy-drafts
```

Turns the guardian's plain-language description into a draft policy: a few outer limits
that code enforces, written guidance for the agent, and example scenarios the guardian
can correct.

The draft is **not active**. Show it to the guardian; it becomes a policy only when the
guardian signs it with their wallet through the platform. Nothing this endpoint returns
can change what the agent is allowed to do.

**Request**

| Field | Type | Rules |
|---|---|---|
| `guardian_id` | string | Identifier format |
| `dependent_id` | string | Identifier format |
| `conversation` | array of turns | 1 to 50 turns; the most recent 20 are used |
| `conversation[].role` | `"guardian"` or `"agent"` | |
| `conversation[].text` | string | Up to 4,000 characters |

Extra fields are rejected with `422`.

```bash
http POST https://agent.example.com/v1/policy-drafts "Authorization:Bearer $AGENT_TOKEN" \
    guardian_id=g1 dependent_id=d1 \
    conversation:='[{"role": "guardian", "text": "100 a week. Books and bus fare are always fine, no games. Receipts for anything over 25. Allowance of 30 on Mondays."}]'
```

```bash
curl -X POST https://agent.example.com/v1/policy-drafts \
     -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
     -d '{"guardian_id": "g1", "dependent_id": "d1",
          "conversation": [{"role": "guardian", "text": "100 a week. Books and bus fare are always fine, no games."}]}'
```

**Response `200`**

```json
{
  "draft": {
    "weekly_cap": 100.0,
    "max_per_request": 40.0,
    "blocked_categories": ["gaming"],
    "proof_threshold": 25.0,
    "review_threshold": null,
    "allowance_amount": 30.0,
    "allowance_weekday": 0,
    "guidance": "School supplies and bus fare are always fine. Ask first about trips.",
    "test_scenarios": [
      {"scenario": "12.00 for a novel for English class", "what_i_would_do": "Send it."}
    ],
    "open_questions": []
  },
  "validation": {"valid": true, "errors": []}
}
```

| Result | Meaning | What to do |
|---|---|---|
| `draft` present, `validation.valid: true` | Ready to show the guardian | Show the limits, guidance, scenarios and open questions; let the guardian edit and sign |
| `draft` present, `validation.valid: false` | The agent produced a draft but it failed validation (for example, a per-request maximum above the weekly cap) | Show `validation.errors`, ask the guardian to clarify, then call again with the longer conversation |
| `draft: null` | No draft could be produced (for example, the model was unavailable) | Try again later |

`open_questions` lists anything the agent had to guess; ask the guardian about these
before they sign. The `agent` role in `conversation` lets you send back your app's
earlier questions so the agent sees the whole exchange.

## 5. Scheduled jobs

```
POST /v1/jobs/{name}
```

The service runs these itself on a schedule. The endpoint lets an operator trigger one
on demand, for example from an external scheduler. Most integrations never need it.

| `name` | What it does | Default schedule |
|---|---|---|
| `allowance` | Sends each dependent's regular allowance on its weekday (adjusted within the guardian's guidance; above-usual amounts get an Auditor review) | Daily 07:00 |
| `pace` | Warns a dependent mid-week when spending is running ahead of the week | Daily 18:00 |
| `reservoir` | Asks a guardian to top up before the reservoir runs out | Daily 09:00 |
| `weekly_summary` | Sends each guardian a short summary of the week, with unusual patterns | Sunday 19:00 |
| `learning` | Proposes policy updates from how guardians answer (they take effect only if signed) | Sunday 19:30 |

Times are Asia/Bangkok. Every job is idempotent: running it twice in the same period
sends nothing twice.

```bash
http POST https://agent.example.com/v1/jobs/pace "Authorization:Bearer $AGENT_TOKEN"
```

Response `200`: `{"ran": "pace"}`. An unknown name returns `404`. The call returns when
the job has finished, which can take a while for `allowance` and `weekly_summary` on a
large platform.

## 6. Health check

```
GET /healthz
```

No authentication. Returns `200` `{"ok": true}` when the service is up. It does not check
the platform API or the model.

## 7. Errors

Errors return a JSON body with a `detail` field, except `500`, which returns plain text.

| Status | `detail` | Cause | Retry? |
|---|---|---|---|
| `401` | `"unauthorized"` | Missing or wrong token | No; fix the token |
| `404` | `"unknown job"` (or `"Not Found"`) | Unknown job name or path | No |
| `422` | `"invalid request id"` or a validation list | Bad identifier, missing or extra field, wrong type | No; fix the request |
| `502` | `"platform API unavailable"` | The agent could not read from or write to the platform API, or the platform sent data outside its contract (too large, malformed, over a length limit). **This currently includes a `request_id` the platform doesn't know.** | Yes, with backoff, if you're sure the request exists. A contract violation fails again on retry: report it to the platform team |
| `500` | Plain text `Internal Server Error` | Unexpected failure | Yes, with backoff |

A `422` validation body looks like this:

```json
{"detail": [{"type": "too_short", "loc": ["body", "conversation"],
             "msg": "List should have at least 1 item after validation, not 0"}]}
```

The model being unavailable is **not** an HTTP error on `process`: you get `200` with
`outcome: "safe_fallback"`, and the guardian is asked to decide.

## 8. Retries, timeouts and concurrency

**Timeouts.** `process` runs one or two AI agents and may take from a few seconds to a
couple of minutes. The agent caps every run at **150 seconds** (`AGENT_MAX_RUN_SECONDS`)
and ends in `safe_fallback` when time runs out, so a client timeout of **3 minutes** is
enough. A timeout does not mean nothing happened: the agent may have finished after you
gave up.

**Retrying `process` is safe for money.** Every transfer carries an idempotency key
derived from the request and the state of its conversation. Calling `process` again on
the same request state can never pay twice: the engine returns the original transfer,
and the agent also refuses to send more than was asked for. A retry may still produce a
second decision record or a repeated receipt request, so prefer retrying only after
errors or timeouts.

**Concurrency.** Process each request one call at a time. Parallel calls for the same
request won't double-pay, but can produce duplicate messages. Different requests can be
processed in parallel.

**Rate limits.** The agent service does not rate-limit callers itself; rate limits and
quotas are applied at the gateway in front of it. Each family also has a usage budget
for AI calls; when it is used up, `process` returns `safe_fallback` until it resets.

## 9. What to show to whom

| Field | Dependent | Guardian | Your logs |
|---|---|---|---|
| `reply_to_dependent` | ✅ Written for them | ✅ | ✅ |
| `summary_for_guardian` | ❌ | ✅ | ✅ |
| `outcome`, `amount_sent` | ✅ (as you see fit) | ✅ | ✅ |
| `guardrail_events` | ❌ May reveal limits | Optional, for transparency | ✅ |
| Policy draft `guidance` | ❌ The guardian's private guidance | ✅ | ✅ |

`reply_to_dependent` is already checked before it is returned: it never contains the
guardian's private guidance, wallet addresses or secrets, and it is capped at 600
characters. You can show it as-is. It is plain text: escape it like any other user-facing
string.

The dependent may be a minor. Don't put request text or replies in URLs, analytics
events or anything stored outside your normal retention rules.

## 10. Reference: types

### Decision

| Field | Type | Description |
|---|---|---|
| `dependent_id` | string | The dependent the request belongs to |
| `request_id` | string or `null` | The request processed (`null` for a scheduled allowance) |
| `outcome` | string | See [Outcomes](#outcomes) |
| `reply_to_dependent` | string | Message for the dependent, max 600 characters |
| `summary_for_guardian` | string | One line for the guardian's activity feed, max 300 characters |
| `amount_sent` | integer | Base units confirmed sent by this call (`0` if none) |
| `policy_version` | integer or `null` | The signed policy version the decision was made under |
| `guardrail_events` | array of strings | Safety checks that fired; informational only |

### Policy draft

| Field | Type | Description |
|---|---|---|
| `weekly_cap` | number | Most the dependent can receive per week (display units) |
| `max_per_request` | number | Most for a single request |
| `blocked_categories` | array of category | Categories never paid for |
| `proof_threshold` | number | From this amount, a receipt is required |
| `review_threshold` | number or `null` | From this amount, the Auditor reviews; `null` means 25% of the weekly cap |
| `allowance_amount` | number or `null` | Regular weekly allowance |
| `allowance_weekday` | integer or `null` | 0 = Monday … 6 = Sunday; set together with `allowance_amount` |
| `guidance` | string | Advice for the agent in the guardian's words, up to 4,000 characters |
| `test_scenarios` | array | Up to 8 `{scenario, what_i_would_do}` pairs |
| `open_questions` | array of strings | What the agent had to guess |

### Category

`education`, `food`, `transport`, `health`, `clothing`, `entertainment`, `gaming`,
`savings`, `allowance`, `other`. New categories may be added within `v1`.
