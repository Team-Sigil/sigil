# Crypto layer handoff

> Written from [SPEC.md](../SPEC.md) and the [README](../README.md), which are the source of
> truth. If this guide disagrees with them, they win.

For Han, from the gateway (Julian). It describes everything the gateway expects from the
crypto layer, so you can build it on your own. Gateway branch `feat/multi-dependent`.

The source of truth for the shapes is `src/gateway/contracts/crypto.py`. `mock_crypto/app.py`
is a working in-memory reference of the same API; the gateway runs against it today.

## 1. What the crypto layer is

Sigil is four HTTP services. The crypto layer is the only one that touches the private key
or sends anything to Solana.

```
iOS app ──▶ Gateway ──▶ Agent layer (decides what to do; untrusted)
              │
              │  the engine has already decided: CLEARED, amount, from, to
              ▼
         Crypto layer ── holds the agent wallet key; re-checks; signs ──▶ Solana devnet
```

**The money model.**

- The guardian owns an SPL token account of our test token **aUSD** (6 decimals): the
  *reservoir*.
- In Phantom, the guardian signs an SPL `approve` that makes the **agent wallet** a delegate
  for up to N aUSD. That transaction is the app's job, not yours.
- The crypto layer holds the agent wallet's private key and sends `transfer_checked` *as the
  delegate*, from the reservoir to the dependent's token account.
- Nobody custodies funds. The token program itself caps the total at the delegated amount.

**Several dependents.** A guardian may support several dependents (one *family* each), and
all of them draw on the guardian's one reservoir. So several `family_id`s can share one
`source_token_account`, and transfers from the same source can reach you at the same time.

**Your responsibilities (SPEC §9; SEC-03, SEC-07, SEC-13, SEC-14, SEC-15)**

1. Hold the agent wallet key: encrypted at rest, only in this service, never logged.
2. Answer delegation reads (`GET /v1/delegation/{token_account}`).
3. Sign and send cleared transfers (`POST /v1/transfer`), **re-checking independently**, so
   that even a compromised gateway can't send money to the wrong place, from the wrong
   reservoir, or beyond your own caps.
4. Never sign the same transfer twice.
5. Run on devnet only.
6. Own the devnet scripts: aUSD mint setup, demo seed, attack demos (SPEC §3).

## 2. Authentication on every request

The gateway signs every request with a shared secret (`CRYPTO_HMAC_SECRET` on our side; SPEC
calls it `SIGNER_HMAC_SECRET`; same value).

| Header | Value |
|---|---|
| `X-Timestamp` | unix seconds, e.g. `1792390532` |
| `X-Nonce` | a UUID4 |
| `X-Signature` | hex HMAC-SHA256(secret, `"{timestamp}.{nonce}."` + raw body bytes) |

For `GET` the body is empty: sign `"{timestamp}.{nonce}."`. Verify exactly like
`mock_crypto/app.py`:

```python
import hashlib, hmac, time

def expected(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.{nonce}.".encode() + body,
                    hashlib.sha256).hexdigest()

# reject unless ALL hold:
#   timestamp is digits and abs(time.time() - int(timestamp)) <= 30
#   hmac.compare_digest(signature, expected(...))      (constant-time)
#   nonce not seen in the last 10 minutes (then remember it)
```

On failure, return `401 {"error_code": "SIGNER_REJECTED"}`. Use the **raw** request bytes;
don't re-serialise the JSON before checking.

## 3. `GET /v1/delegation/{token_account}`

The engine calls this before every transfer decision, and for the budget screens. It must
read the chain **fresh** (no caching beyond a few seconds), because one attack demo is "the
guardian revokes the delegation mid-demo".

```json
200 {"token_account": "<pubkey>", "owner": "<guardian wallet>",
     "delegate": "<pubkey or null>", "delegated_amount_units": 500000000}
```

- Read the token account (`getAccountInfo`, `jsonParsed`, `confirmed` commitment):
  `parsed.info.owner`, `parsed.info.delegate`,
  `parsed.info.delegatedAmount.amount`.
- The amount is a string on chain: return it as an **integer** in base units, never a float.
- `token_account` in the reply must equal the one asked for.
- No delegate (never approved, or revoked): `delegate: null`, `delegated_amount_units: 0`,
  status 200.
- Not a token account, or doesn't exist: `404 {"error_code": "UNKNOWN_ACCOUNT"}`.
- Any other status, a slow answer or a malformed body makes the gateway treat the delegation
  as unknown and **block** the transfer (the safe default).

The gateway checks `owner` == the guardian's wallet and `delegate` == the agent wallet
itself, so report them exactly as on chain.

## 4. `POST /v1/transfer`

Body (strict, no other fields):

```json
{"transfer_id": "uuid", "family_id": "uuid", "amount_units": 8000000,
 "source_token_account": "<guardian reservoir>", "destination_token_account": "<dependent account>",
 "mint": "<aUSD mint>", "idempotency_key": "req:<request id>:1"}
```

Response:

```json
200 {"status": "confirmed", "tx_signature": "<base58 signature>", "error_code": null}
200 {"status": "failed", "tx_signature": null, "error_code": "INSUFFICIENT_DELEGATION"}
```

### What your status code tells the gateway

This part matters most. The gateway reserves the money before calling you, and what it does
next depends only on your answer:

| You return | Gateway concludes | Gateway does |
|---|---|---|
| `200 confirmed` | money moved | marks it sent, stores the signature |
| `200 failed` + `error_code` | definitely did **not** move | releases the reservation |
| `4xx` + `{"error_code": …}` | refused before signing, did **not** move | releases the reservation |
| `5xx`, timeout, no answer | **unknown** | keeps the reservation, retries the same request (same `idempotency_key`, same `transfer_id`) every 60 s until it gets a definite answer |

So **never return 4xx or `failed` unless you are sure nothing was or will be sent.** When in
doubt, return `503`. The gateway's timeout is 60 s: if confirmation takes longer, return
`503` and let the retry pick it up.

### Checks, in this order

1. **HMAC, timestamp, nonce** (section 2) → `401 SIGNER_REJECTED`.
2. **Idempotency.** If this `idempotency_key` already has a final result, return it unchanged
   and do nothing else. This must come before the caps: a retry of a transfer you already
   sent must still return `confirmed`, even if today's cap is now full. Otherwise the
   gateway would release money that actually moved.
   - If the key is **in progress** (another request is signing it right now), return
     `503 {"error_code": "IN_PROGRESS"}`, not 4xx.
   - If the key was sent before but its outcome isn't known, see "Never sign twice" below.
3. **Registered source and destination.** Look up `family_id` in the
   `registered_destinations` view (section 5). Require `destination_token_account` ==
   `dependent_token_account` **and** `source_token_account` == `guardian_token_account` →
   else `403 {"error_code": "SIGNER_REJECTED"}`. The source check matters because every
   guardian's reservoir delegates to the *same* agent wallet. Without it, a compromised
   gateway could pay one family from another guardian's reservoir.
4. **Mint** == your `AUSD_MINT` → else `403 SIGNER_REJECTED`.
5. **Your own caps.** `amount_units ≤ SIGNER_MAX_PER_TRANSFER_UNITS`, and today's signed
   total + amount ≤ `SIGNER_MAX_PER_DAY_UNITS` → else `403 {"error_code": "OVER_SIGNER_CAP"}`.
   Suggestion: count the daily total per `source_token_account` (one reservoir) as well as
   overall, so one guardian can't use up everyone's day.
6. **Delegation on chain.** delegate == agent wallet and `delegatedAmount ≥ amount_units` →
   else `200 failed INSUFFICIENT_DELEGATION`.
7. **Sign and send** `transfer_checked`:
   - source = `source_token_account`, mint = `mint`, destination =
     `destination_token_account`;
   - authority = the agent wallet (as delegate), amount = `amount_units`, decimals = 6;
   - **no memo**: no personal data on chain (SEC-15).

   Wait for `confirmed` commitment, then return `200 confirmed` with the signature. A
   definite on-chain error (for example the transaction landed and failed) → `200 failed`
   with an `error_code` such as `TX_FAILED`.

Error codes are free text up to 64 characters; the gateway stores them as
`signer failed: <code>`.

### Never sign twice (SEC-07)

The gateway retries anything whose outcome it didn't learn, so a crash between "sent" and
"answered" must not lead to a second payment:

1. Before sending, store `idempotency_key → {transaction signature, last valid block height}`
   in **your own** durable storage. A Solana transaction's signature is known as soon as you
   sign it. A small SQLite file on a volume is enough; you have no write access to the
   gateway's database.
2. On a retry of a key with a stored signature, don't re-sign. Ask the chain
   (`getSignatureStatuses`):
   - confirmed → return `confirmed`;
   - landed with an error → `failed`;
   - not found and the blockhash still valid → `503` (still in flight);
   - not found and the blockhash **expired** → it can never land, so it is safe to build a
     new transaction (store its signature first).
3. Store the final result against the key, and return it for every later retry.

## 5. Reading the registered accounts (read-only database)

You get a Postgres login that can read exactly one view:

```
DATABASE_URL_READONLY=postgresql://sigil_signer_ro:<password>@<host>:5432/sigil
```

```sql
SELECT dependent_token_account, guardian_token_account
FROM registered_destinations WHERE family_id = :family_id;
```

| Column | Meaning |
|---|---|
| `family_id` | one guardian + one dependent |
| `dependent_token_account` | where money for this family may go |
| `guardian_token_account` | where it may come from (shared by the guardian's dependents) |

The role can't read any table (tested), only this view. Rows are written when the guardian
registers a dependent with a wallet signature. The password is `SIGIL_SIGNER_RO_PASSWORD` in
the gateway's `.env`; ask Julian.

## 6. Startup and secrets

- **Devnet only (SEC-14).** Refuse to start unless `SOLANA_RPC_URL` contains `devnet`.
- **The key (SEC-13).** `AGENT_WALLET_KEY_ENC` is the agent wallet's secret key, encrypted
  with Fernet using `SIGNER_MASTER_KEY`. Decrypt it once at startup and keep it in memory.
  Never log it, never return it, never put it in an error message. Also check that it derives
  to `AGENT_WALLET_PUBKEY`, and refuse to start if not.
- Listen only on the internal network: the gateway is your only client. In docker-compose,
  don't publish the port beyond `127.0.0.1`.
- `MOCK_CHAIN=true` (SPEC §9): skip steps 6 and 7 and return `tx_signature = "mock_" + uuid`.
  Useful before devnet works.

| Variable | Purpose | Shared with the gateway? |
|---|---|---|
| `SIGNER_HMAC_SECRET` | request signatures | yes: = gateway `CRYPTO_HMAC_SECRET` |
| `AUSD_MINT` | the aUSD mint | yes: same value |
| `AGENT_WALLET_PUBKEY` | the delegate's public key | yes: same value |
| `AGENT_WALLET_KEY_ENC`, `SIGNER_MASTER_KEY` | the encrypted key and its Fernet key | no, crypto layer only |
| `SOLANA_RPC_URL` | `https://api.devnet.solana.com` (or another devnet RPC) | no |
| `DATABASE_URL_READONLY` | section 5 | no |
| `SIGNER_MAX_PER_TRANSFER_UNITS`, `SIGNER_MAX_PER_DAY_UNITS` | your caps (suggested: 200 and 300 aUSD) | no |
| `MOCK_CHAIN` | skip the chain | no |

## 7. Devnet setup (your `scripts/`)

What the demo needs:

1. **An aUSD mint** with 6 decimals (`spl-token create-token --decimals 6`). Its address is
   `AUSD_MINT`.
2. **The agent wallet**: a new keypair. Its public key is `AGENT_WALLET_PUBKEY`; encrypt the
   secret key into `AGENT_WALLET_KEY_ENC`. It needs a little devnet SOL for fees
   (`solana airdrop`).
3. **Demo wallets**: a guardian wallet (the one in Phantom) with an aUSD token account
   holding some aUSD, and one or two dependent wallets, each with an aUSD token account.
4. **The delegation**: in the real app the guardian approves in Phantom. For the seed and
   the tests, a script can do the same `approve` with the guardian's demo key.
5. **Attack demo 4**: a script that revokes the delegation (`spl-token revoke`). The next
   transfer must fail with `insufficient delegation`.

## 8. Running against the gateway

Start the gateway stack as usual (`docker compose up -d`). It uses `mock-crypto` until you
switch it over:

1. Run your service on `127.0.0.1:8300`, and stop the mock: `docker compose stop
   mock-crypto`.
2. In the gateway's `.env`, set `GATEWAY_CRYPTO_URL=http://host.docker.internal:8300` and
   restart the gateway container.
3. From your machine, Postgres is at `localhost:5432` (database `sigil`, user
   `sigil_signer_ro`).

## 9. Tests to write (SPEC §13)

| Test | Proves |
|---|---|
| bad HMAC, old timestamp, reused nonce → 401 | SEC-03 request authentication |
| destination not the family's → 403, nothing signed | SEC-03 allowlist |
| source not the family's (another guardian's reservoir) → 403 | SEC-03 source check |
| over per-transfer / per-day cap → 403 | SEC-03 own caps |
| delegation revoked or too small → `failed INSUFFICIENT_DELEGATION` | SEC-03 delegation |
| same key twice, also after the day cap is full → same `confirmed`, one transaction | SEC-07 |
| crash after sending, then retry → no second transaction | SEC-07 |
| non-devnet `SOLANA_RPC_URL` → refuses to start | SEC-14 |
| logs contain no key material | SEC-13 |
| transaction has no memo | SEC-15 |

The gateway's side of the contract is tested in `tests/test_crypto_client.py`, against
`mock_crypto`.

## Questions

Anything unclear or that doesn't fit how Solana works: tell Julian before building around
it. Changing this contract changes `contracts/crypto.py`, and both sides need to agree.
