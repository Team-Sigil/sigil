# Crypto layer (signer)

The crypto layer of Sigil: the **signer**. It holds the agent wallet's key, answers
delegation reads, and sends cleared aUSD transfers on Solana **devnet** as the delegate of
the guardian's reservoir. It re-checks every transfer on its own, so even a compromised
gateway can't send money to the wrong place, from the wrong reservoir, or beyond its caps.

The source of truth is `../gateway`: `SPEC.md` §9 and §13, README "Crypto layer
contract" and `docs/crypto-handoff.md`. `src/signer/contracts.py` is a verbatim copy of the
gateway's `contracts/crypto.py`; `tests/test_contracts.py` fails if they drift.

## API

Every request carries `X-Timestamp`, `X-Nonce` and `X-Signature` = hex
HMAC-SHA256(`SIGNER_HMAC_SECRET`, `{timestamp}.{nonce}.{raw_body}`). A bad signature, a
timestamp more than 30 s off, or a nonce seen in the last 10 minutes → `401 SIGNER_REJECTED`.

| Method | Path | Answer |
|---|---|---|
| GET | `/v1/delegation/{token_account}` | `200 {token_account, owner, delegate, delegated_amount_units}` read fresh at `confirmed`; no delegate → `delegate: null, 0`; not a token account → `404 UNKNOWN_ACCOUNT` |
| POST | `/v1/transfer` | `200 confirmed` / `200 failed` / `4xx` / `503`, see below |
| GET | `/healthz` | `{"status": "ok"}`, unauthenticated |

### `/v1/transfer`, in order

1. HMAC, timestamp, nonce → `401 SIGNER_REJECTED`.
2. **Idempotency.** A key with a final result gets it back unchanged, before any other
   check (so a retry still gets `confirmed` when today's cap is full or the delegation was
   revoked since). A key another request is working on → `503 IN_PROGRESS`.
3. Registered accounts from `registered_destinations`: destination = the family's
   `dependent_token_account` **and** source = its `guardian_token_account` →
   else `403 SIGNER_REJECTED`.
4. Mint = `AUSD_MINT` → else `403 SIGNER_REJECTED`.
5. Caps: per transfer, per UTC day overall, optionally per UTC day per reservoir →
   `403 OVER_SIGNER_CAP`. In-flight transfers count; failed ones don't.
6. On-chain delegation: delegate = agent wallet and delegated amount ≥ amount →
   else `200 failed INSUFFICIENT_DELEGATION`.
7. Sign one `transfer_checked` (agent wallet as fee payer and delegate, 6 decimals, **no
   memo**), store its signature, send, wait for `confirmed` → `200 confirmed`.

| Answer | Meaning for the gateway | When |
|---|---|---|
| `200 confirmed` | moved | confirmed on chain (or `MOCK_CHAIN`) |
| `200 failed` | did not move, never will | `INSUFFICIENT_DELEGATION`; preflight refused (`TX_REJECTED`, `INSUFFICIENT_FUNDS`, …); landed with an error (`TX_FAILED`, …) |
| `4xx` | refused before signing | `401`, `403`, `413`, `422 INVALID_REQUEST` |
| `503` | unknown, retry the same key | `IN_PROGRESS`, `IN_FLIGHT`, `NOT_CONFIRMED_YET`, `IDEMPOTENCY_MISMATCH`, `CHAIN_UNAVAILABLE`, `REGISTRY_UNAVAILABLE`, `SIGNER_ERROR` |

### Never signing twice (SEC-07)

`data/signer.sqlite3` holds one row per idempotency key: `claimed → sent → confirmed |
failed`. The transaction's signature, last valid block height and raw bytes are written
**before** it is sent. A retry of a `sent` key never signs blind; it asks the chain:

- confirmed → `confirmed`; landed with an error → `failed`;
- not found, blockhash still valid → rebroadcast the same bytes (same signature, can't pay
  twice) and answer `503`;
- not found and the *finalized* block height is past the last valid height → it can never
  land, so a new transaction is signed (after re-checking the delegation) and stored first.

Rows are worked on under a lease (120 s), taken in a `BEGIN IMMEDIATE` transaction, so two
requests for one key can't both sign, and a crashed worker's key frees itself. A final
result is never overwritten. Keep the database on a volume.

## Run

```sh
uv sync --extra dev
cp .env.example .env          # fill in, see below
uv run python -m signer       # 127.0.0.1:8300
```

It refuses to start when `SOLANA_RPC_URL`'s host isn't devnet, when the cluster's genesis
hash isn't devnet's (skipped with `MOCK_CHAIN=true`), or when `AGENT_WALLET_KEY_ENC` doesn't
decrypt with `SIGNER_MASTER_KEY` or doesn't derive to `AGENT_WALLET_PUBKEY`.

Docker: `docker compose up -d --build` (port on `127.0.0.1` only, ledger on a volume; set
`DATABASE_URL_READONLY` to `host.docker.internal:5432` inside the container).

**Against the gateway** (handoff §8): run the signer on `127.0.0.1:8300`,
`docker compose stop mock-crypto` in `../gateway`, set
`GATEWAY_CRYPTO_URL=http://host.docker.internal:8300` in the gateway's `.env`, and restart
its gateway container. `SIGNER_HMAC_SECRET` must equal the gateway's `CRYPTO_HMAC_SECRET`.

| Variable | Purpose |
|---|---|
| `SIGNER_HMAC_SECRET` | = gateway `CRYPTO_HMAC_SECRET` |
| `AUSD_MINT`, `AGENT_WALLET_PUBKEY` | same values as the gateway |
| `AGENT_WALLET_KEY_ENC`, `SIGNER_MASTER_KEY` | the Fernet-encrypted 64-byte secret key and its key; this service only |
| `SOLANA_RPC_URL` | a devnet RPC |
| `DATABASE_URL_READONLY` | `postgresql://sigil_signer_ro:<SIGIL_SIGNER_RO_PASSWORD>@localhost:5432/sigil` |
| `SIGNER_MAX_PER_TRANSFER_UNITS`, `SIGNER_MAX_PER_DAY_UNITS` | caps in base units (default 200 and 300 aUSD) |
| `SIGNER_MAX_PER_SOURCE_PER_DAY_UNITS` | optional cap per reservoir per UTC day |
| `MOCK_CHAIN` | `true`: transfers skip the delegation check and the chain (`mock_<uuid>`); delegation reads still read devnet |
| `SIGNER_DB_PATH`, `SIGNER_HOST`, `SIGNER_PORT` | ledger file, listen address |

## Devnet scripts

```sh
uv run python -m scripts.new_agent_wallet       # prints AGENT_WALLET_PUBKEY / _KEY_ENC / SIGNER_MASTER_KEY
AGENT_WALLET_PUBKEY=... uv run python -m scripts.setup_devnet  # mint, wallets, accounts, funding, approve
uv run python -m scripts.setup_devnet --guardian <Phantom wallet>  # guardian approves in the app
uv run python -m scripts.delegation show
uv run python -m scripts.delegation revoke      # attack demo 4: next transfer → INSUFFICIENT_DELEGATION
uv run python -m scripts.delegation approve 500
uv run python -m scripts.token_metadata set     # name the mint, so wallets show "aUSD"
```

`token_metadata` writes a Metaplex Token Metadata account (name "Sigil aUSD", symbol `aUSD`)
for the mint, signed by the setup payer, which is the mint authority. Without it, wallets
such as Phantom list aUSD as "Unknown Token". It is display only: the signer and the gateway
always identify aUSD by its mint address. From the repo root: `./sigil crypto token_metadata set`.

`setup_devnet` is idempotent. It keeps throwaway demo keypairs in `.devnet/` (git-ignored,
solana-keygen format) and writes the addresses the gateway needs to `.devnet/seed.json`
(mint, guardian wallet and token account, dependent wallets and token accounts). Devnet
airdrops are rate limited: if one is refused, fund the printed address at
https://faucet.solana.com and re-run. The agent wallet's plain key is never written to disk.

## Tests

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests scripts
```

`tests/fake_chain.py` is an in-memory JSON-RPC that decodes, verifies and executes the
signer's real transactions like the token program does, so the tests check the bytes that
would go to devnet. `tests/test_registry.py` runs against the gateway's Postgres when
`SIGNER_TEST_DATABASE_URL_READONLY` is set.

| SPEC §13 test | Where |
|---|---|
| bad HMAC, old timestamp, reused nonce → 401 | `test_auth.py` |
| destination / source not the family's → 403, nothing signed | `test_transfer.py` |
| over per-transfer / per-day cap → 403 | `test_transfer.py` |
| delegation revoked or too small → `failed INSUFFICIENT_DELEGATION` | `test_transfer.py` |
| same key twice, also after the day cap is full → same `confirmed`, one transaction | `test_transfer.py` |
| crash after sending, then retry → no second transaction | `test_transfer.py` (`test_lost_response_*`, `test_crash_*`, `test_expired_*`) |
| non-devnet `SOLANA_RPC_URL` → refuses to start | `test_startup.py` |
| logs contain no key material | `test_startup.py` |
| transaction has no memo | `test_transfer.py` |

## Layout

```
src/signer/
  main.py        create_app: startup checks, routes, error mapping
  transfers.py   the /v1/transfer workflow (checks, idempotency, sign once)
  store.py       SQLite ledger and nonces
  chain.py       devnet JSON-RPC, transfer_checked builder
  registry.py    registered_destinations (read-only Postgres)
  keys.py        the encrypted agent wallet key
  auth.py        HMAC, timestamp, nonce
  config.py      settings, devnet check
  contracts.py   verbatim copy of the gateway's contracts/crypto.py
scripts/         devnet setup, delegation approve/revoke, agent wallet
tests/
```
