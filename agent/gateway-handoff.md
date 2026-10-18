# Gateway handoff: open items for the gateway layer

For Julian (gateway layer). This is everything the agent layer needs from the gateway,
from reviewing `sigil-gateway` at `6810508` against the agent layer's current contract.
Items are ranked; P0 is a money-safety bug.

The agent layer's `mock_platform/app.py` implements every engine change below as a
reference, with tests in `tests/test_engine_alignment.py` and `tests/test_proofs.py`.
Proof and upload details are in [proof-images.md](proof-images.md).

## Summary

| # | Item | Priority | Contract change |
|---|---|---|---|
| 1 | [Pending transfers can pay twice](#1-pending-transfers-can-pay-twice) | **P0** | Yes |
| 2 | [Re-copy the agent contract](#2-re-copy-the-agent-contract) | **P0** (blocks 1, 3) | Yes |
| 3 | [Expose signed guardian overrides](#3-expose-signed-guardian-overrides) | P1 | Yes |
| 4 | [Normalise proof images at upload](#4-normalise-proof-images-at-upload) | P1 (privacy) | No |
| 5 | [Parity tests skip silently](#5-parity-tests-skip-silently) | P1 | No |
| 6 | [More proof integrity flags](#6-more-proof-integrity-flags) | P2 | No |
| 7 | [Verified in-app capture and camera-only](#7-verified-in-app-capture-and-camera-only) | P2 (with the mobile team) | No |
| 8 | [Daily request quota per dependent](#8-daily-request-quota-per-dependent) | P2 | No |
| 9 | [Auditor read access to context](#9-auditor-read-access-to-context) | P3 | No |
| 10 | [Link decisions to transfers](#10-link-decisions-to-transfers) | P3 | Maybe |

Already done on your side, no action needed: history built from the decision log,
signed guardian answers in the thread, `amount_requested` updated from the dependent's
messages, the requested-amount check, the allowance cap, locales, the 3-proof cap,
exact-duplicate flags, typed text treated as a claim, the engine's proof rules, and body,
rate and token limits.

---

## 1. Pending transfers can pay twice

**What happens today.**

1. The crypto layer is down. The engine clears a transfer, reserves the money and keeps
   it `pending` (`engine/transfers.py:466`). The agent is told `blocked` with "signer
   unavailable; transfer pending".
2. The agent records its decision, and the gateway appends the agent's reply to the
   thread. The thread is one message longer.
3. The dependent writes again. The agent's idempotency key is
   `req:{request_id}:{thread length}`, so the new run has a **new key**.
4. Check 10 (`engine/checks.py:105`) compares against `already_sent`, which only rises
   on confirmation. The pending amount isn't counted, so a second transfer for the same
   request clears.
5. Nothing ever replays the first key: it stays pending with its reservation held, or
   both go out when the crypto layer recovers. The weekly cap limits the damage, but
   "never more than was asked" breaks.

**What to change.**

| Change | Where |
|---|---|
| Check 10: `amount ≤ amount_requested − already_sent − (pending transfers for this request)` | `engine/checks.py`, `RequestFacts` |
| Return `TransferResult(status="pending", amount=…)` instead of `blocked` while the outcome is unknown | `engine/transfers.py` (`_send`) |
| Expose the pending amount on `GET /requests/{id}` as `already_pending` | `agent_api/service.py` |
| A job that retries every pending transfer by its own idempotency key (the crypto layer is idempotent on it) | new; the scheduler or a startup task |

**Done when** a test shows: signer down → first transfer pending → a second run with a
new key is blocked `over requested amount` → the retry job sends the first exactly once.

**What the agent does now.** It treats `pending` as "may still go out". It ends the run
with a new code-only outcome `transfer_pending`, tells the dependent the money is on its
way, doesn't ask the guardian, and refuses to send again while `already_sent +
already_pending` covers the request. That's defense in depth; the engine rule is the one
that has to hold.

## 2. Re-copy the agent contract

`agent_layer/contracts.py` has four additive changes. Copy it to
`gateway/contracts/agent.py`; with the agent layer checked out, the parity test currently
fails on them, as it should.

| Change | Type | Default |
|---|---|---|
| `TransferResult.status` gains `"pending"` | new enum value | — |
| `DependentRequest.already_pending: int ≥ 0` | new field | `0` |
| `DependentRequest.guardian_approved_amount: int > 0 \| None` | new field | `None` |
| `Outcome.TRANSFER_PENDING = "transfer_pending"` | new code-only outcome, in `DecisionRecord` | — |

The app needs a `transfer_pending` state: "on its way", with no retry button (see the
agent's `guide.md`, Outcomes).

## 3. Expose signed guardian overrides

The engine lets exactly the signed amount past checks 7, 8 and 11. The agent couldn't use
that: its tool guard refused anything over the per-request max or the weekly budget, and
its triggers sent the transfer to the Auditor, which could ask the guardian a second
time.

**Change:** fill `DependentRequest.guardian_approved_amount` from the same lookup as
`_guardian_approved` (`engine/transfers.py:373`): an answered, approved question for this
request whose signature verifies.

**What the agent does with it:** for exactly that amount, it skips its per-request and
weekly checks and the Auditor, and logs `guardian_override`. Freeze, blocked categories,
the reservoir, the requested amount and proof still apply, matching your engine. The
engine re-verifies the signature, so a wrong field can't move money the engine wouldn't.

## 4. Normalise proof images at upload

Your known gap 2. Uploads are typed and hashed, but stored as sent. Phone photos carry
EXIF metadata, often with GPS location, and the dependent may be a minor.

At `POST /api/v1/requests/{id}/proofs`:

1. Read what's needed **before** stripping anything: C2PA credentials, camera make and
   model, capture time (see item 6).
2. Decode the image; reject anything that isn't a valid JPEG, PNG or WebP by its bytes;
   reject animated images.
3. Strip all metadata by re-encoding. Resize so the long edge is at most 2576 px (or
   1568 px: open question 1 in `proof-images.md`). Keep the result under **3.75 MB** so it
   works on every Claude platform.
4. Store `size_bytes` and `sha256` of the **stored** bytes (the agent checks both), and
   keep the original encrypted if it's needed as evidence.

## 5. Parity tests skip silently

`tests/test_contracts.py`, `tests/test_engine_triggers.py` and
`tests/db/test_agent_layer_clients.py` skip unless the agent layer is at
`../agent-layer`. A different checkout layout, or a CI job without it, passes with no
check at all. Read the path from an env var (for example `AGENT_LAYER_SRC`), and fail
instead of skipping when `CI` is set. With the path pointed at the agent repo, all 131
unit tests passed before the contract changes in item 2.

## 6. More proof integrity flags

Today the gateway sets only `duplicate` (exact SHA-256 match in the family). The agent
also understands these flags; any flag it doesn't know is ignored.

| Flag | How to set it | Agent's effect |
|---|---|---|
| `near_duplicate` | Perceptual hash (pHash or dHash) close to an earlier proof in the family | Forces an Auditor review |
| `screenshot`, `no_camera_metadata`, `old_capture`, `edited` | Metadata read before stripping (item 4) | Forces a review |
| `ai_generated` | A C2PA credential saying "AI-generated", or a SynthID hit once Google offers an API | Doesn't count as proof; forces a review |
| `ai_suspected`, `rephotographed_screen` | Optional classifiers; never reject on these alone | Forces a review |
| `camera_signed` | A valid C2PA camera capture signature | A note only |

Also worth doing: a **receipt fingerprint shared across families** (merchant + date +
total + receipt number, stored as a keyed hash), so the same receipt can't be used by two
families. Today `used_proof_keys` is per family.

## 7. Verified in-app capture and camera-only

Your known gap 3, and the strongest defense against forged receipts. The design is in
`proof-images.md` ("Verified in-app capture"). The gateway's part:

- Register device keys with app attestation (Android key attestation plus Play Integrity,
  or iOS App Attest).
- Issue capture tickets: single-use nonces bound to a request, the dependent and the
  device key, valid about 5 minutes.
- Verify the signed claim (key, signature, nonce, SHA-256, capture time) before
  normalising, and only then set `source: "photo"`. Anything that fails is `"file"`.
- For OCR text, set `derived_from` to the photo it was read from.
- The engine's half of camera-only: only `photo` images, and `ocr` text derived from a
  `photo` on the same request, count above the proof threshold (the agent's reference:
  `counts_as_proof` in `mock_platform/app.py`). Turn it on together with the agent's
  `AGENT_CAMERA_ONLY_PROOFS`; until both are on, keep both off.

## 8. Daily request quota per dependent

Every dependent message runs Claude Opus at high effort, including off-topic ones ("what
is 1+1?"). The gateway limits 30 calls per minute per token and 200,000 LLM tokens per
family per day, but not the number of requests a day. SPEC §7 has
`max_requests_per_day` for this. Suggestion: a per-dependent daily cap on new requests
and messages, with a clear "try again tomorrow" error for the app.

## 9. Auditor read access to context

The agent layer's known gap 1: the Auditor reviews the maker's snapshot instead of
reading context itself, so a compromised maker could feed it doctored history. Fixing it
needs the gateway to accept the **Auditor's** token on `GET /dependents/{id}/context`,
read-only. No change needed yet; flagging it so the auth rules leave room.

## 10. Link decisions to transfers

The agent layer's known gap 3: `DecisionRecord` doesn't carry the engine's decision id
or `tx_signature`, so the audit trail can't join a decision to its transfer directly.
The gateway already returns `decision_id` from `POST /decisions`. Options: the gateway
links them by request and time, or the agent adds the transfer's `decision_id` to
`DecisionRecord` (a contract change). Your call on which is simpler.

---

## Answers to your README questions

- **Allowance cap.** Agreed: the agent now caps the allowance at the signed amount (1.0×,
  down from 1.5×). Lowering it is fine; for more, the model sends the usual amount and
  notifies the guardian. `trusted_triggers` is unchanged, so the parity test still holds;
  its `allowance_above_usual` branch is now unreachable from the agent.
- **`guardian_answer` wording.** Your current text works. The agent escapes and caps it
  like other platform text. Once item 3 ships, the wording no longer matters for money:
  the agent takes the amount from the field, never from the text.

## Timeouts

The agent now caps every run at **150 s** (`AGENT_MAX_RUN_SECONDS`), under your 180 s
`PROCESS_TIMEOUT_SECONDS`. Each Claude call gets the time left as its timeout, with one
retry; when time runs out the run ends in `safe_fallback` (or `transfer_pending` /
`sent_*` if money already moved). Keep your timeout above the agent's cap if either
changes.

## Not verified from the agent side

Your 121 database tests, including the end-to-end runs with the agent layer's
`MakerAPI`, need Postgres and weren't run in this review. Please run them after items 1–3.
