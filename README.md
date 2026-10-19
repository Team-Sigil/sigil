# Sigil: Delegated Financial Guardianship

A guardian keeps funds in their own Phantom wallet and describes, in plain language, how each
dependent should be supported. An AI agent runs each dependent's allowance day to day, an
independent Auditor agent reviews larger or riskier decisions, and deterministic code
guarantees that nothing moves beyond the guardian's signed limits. Money moves as an SPL
token (aUSD) on **Solana devnet** through SPL token delegation, so Sigil never holds funds.

**Core security claim:** even a fully manipulated agent cannot move more than the guardian
allowed, to anyone other than the dependent.

FinTechathon 2026 entry (theme: AI as a Financial Participant).

## Repository layout

| Folder | Layer | What it is | Docs |
|---|---|---|---|
| [`gateway/`](gateway) | Gateway | App API, platform API, the policy engine (the only path to money), PostgreSQL with a hash-chained audit log | [README](gateway/README.md), [SPEC](gateway/SPEC.md) |
| [`agent/`](agent) | Agent | Maker agent, Auditor agent and scheduler; acts only through the gateway's platform API | [README](agent/README.md) |
| [`crypto/`](crypto) | Crypto | The signer: holds the agent wallet key, re-checks and signs `transfer_checked` on devnet; devnet setup scripts | [README](crypto/README.md) |
| root | Stack | `docker-compose.yml`, the `./sigil` script, `.env.example`, `config/` | this file |

The native iOS app lives in its own repository,
[`Team-Sigil/sigil-ios`](https://github.com/Team-Sigil/sigil-ios).

**Where to start reading:**

- [`FEATURES.md`](FEATURES.md): what the whole system does, feature by feature, and what is
  built today.
- [`gateway/SPEC.md`](gateway/SPEC.md): the project's single source of truth (product,
  architecture, policy, security requirements `SEC-01`–`SEC-20`, decisions).
- [`gateway/docs/architecture.md`](gateway/docs/architecture.md): design rationale, threats and
  controls, user flows.
- [`gateway/README.md`](gateway/README.md): the policy engine's checks and every API.
- [`agent/README.md`](agent/README.md): the agents, their guardrails and isolation.
- [`crypto/README.md`](crypto/README.md): the signer's checks and how it never signs twice.

```
iOS app ──► gateway :8000 ──► agent :8100 ──► Claude API / OpenRouter
              │   ▲              │
              │   └──────────────┘  platform API (/agent/v1, maker/auditor tokens)
              ├──► signer :8300 ──► Solana devnet
              └──► postgres :5432 ◄── signer (read-only registered_destinations)
```

## Setup

Needs Docker and openssl. No Python or uv on the host: the devnet scripts run inside the
signer image.

### 1. Configure

```sh
git clone git@github.com:Team-Sigil/sigil.git && cd sigil
./sigil init      # creates .env with generated secrets and the agent wallet
```

Then set these in `.env`:

- **The model.** `OPENROUTER_API_KEY` (or `ANTHROPIC_API_KEY` with
  `AGENT_LLM_PROVIDER=anthropic`). Paid OpenRouter models need credits on that account,
  otherwise every call fails with `402`. What the demo server runs, fast and about $0.0003
  per call:

  ```sh
  AGENT_MAKER_MODEL=deepseek/deepseek-v4.1-flash-1017
  AGENT_AUDITOR_MODEL=deepseek/deepseek-v4.1-flash-1017
  AGENT_OPENROUTER_VISION=1     # it reads receipt photos
  AGENT_MAKER_EFFORT=medium     # a policy interview turn: ~23 s instead of ~60 s at high
  ```

- **`SIWS_DOMAIN`**: the host of the `app_url` the iOS app gives Phantom (for example
  `iden.casa` for `https://iden.casa`). Sign-in fails if they differ.
- **`RATE_LIMIT_PER_MIN`** (optional, default 30 calls per minute per session): an app that
  polls needs more; the demo server uses 1000.

### 2. Fund the agent wallet

`./sigil init` prints `AGENT_WALLET_PUBKEY`. It pays the fee for every transfer, so send it
devnet SOL at https://faucet.solana.com.

### 3. Create the devnet accounts

```sh
./sigil devnet --guardian <guardian's Phantom wallet>   # add --dependents 4 for more
./sigil crypto token_metadata set   # optional: wallets show "aUSD" instead of "Unknown Token"
```

This creates the aUSD mint, the guardian's reservoir with 1,000,000 aUSD (`--fund`; amounts are
sized for baht), and demo dependents (keys in `devnet/dependent-N.json`, addresses in
`devnet/seed.json`). It is safe to re-run:
it reuses everything and only tops the reservoir back up. Without `--guardian` it makes a
throwaway guardian key instead and approves the agent itself (step 5 is then done).

### 4. Start

```sh
./sigil up        # build and start; gateway on http://127.0.0.1:8000
```

To reach it from a phone, set `GATEWAY_BIND=0.0.0.0` (same network) or put a tunnel in front
of port 8000 (the demo server uses a Cloudflare tunnel to `https://api.iden.casa`).

### 5. Let the agent spend from the reservoir

The guardian must approve the agent wallet on their reservoir; until then every transfer is
blocked with `insufficient delegation`. The app will do this with Phantom (an SPL `approve`);
until it does, sign it once on the server with the guardian's key, exported from Phantom
(Settings → the account → Show Private Key). The key is used for this one transaction and
removed again:

```sh
read -rs KEY && docker exec -i sigil-stack-signer-1 python -c \
  "import sys, json; from solders.keypair import Keypair; print(json.dumps(list(bytes(Keypair.from_base58_string(sys.stdin.read().strip())))))" \
  <<< "$KEY" > devnet/guardian.json && chmod 600 devnet/guardian.json; unset KEY
./sigil crypto delegation approve 500000   # up to 500,000; a new approve replaces the old amount
rm devnet/guardian.json
```

The key must belong to the guardian in `devnet/seed.json` (the last `--guardian` given to
`./sigil devnet`); for another guardian, re-run step 3 with their wallet first.
`./sigil crypto delegation show` prints the reservoir, its balance and the delegation.

### 6. First run in the app

The order matters:

1. The guardian signs in with Phantom (a new wallet becomes a guardian).
2. The guardian registers each dependent: their wallet and aUSD account from
   `devnet/seed.json`, with the reservoir as `guardian_token_account`.
3. The guardian runs the policy interview and signs the activation. Until a dependent's family
   has an active policy, the agent can't act on their requests.
4. Each dependent signs in. To use a demo dependent, import its key into Phantom as a second
   account; this prints the key Phantom asks for:

   ```sh
   docker exec -i sigil-stack-signer-1 python -c \
     "import sys; from solders.keypair import Keypair; print(Keypair.from_json(sys.stdin.read()))" \
     < devnet/dependent-1.json
   ```

The iOS app's side, step by step: [`gateway/docs/auth-guide.md`](gateway/docs/auth-guide.md)
and [`gateway/docs/app-handoff.md`](gateway/docs/app-handoff.md).

### Optional: signed PDF receipts

Online orders' PDF receipts count as proof only when the shop's signature verifies. Put the
certificate that issues Lazada's signing certificate (Thai Digital ID CA G3) in
`config/pdf-trusted-roots.pem`, checked by fingerprint against an independent source such as
Adobe Acrobat's trust list, never copied out of a PDF. While the file holds no certificate, no
PDF receipt counts.

### A clean demo

`./sigil reset-db` empties the database (after a backup). Money already sent on devnet stays
sent: re-run `./sigil devnet --guardian …` to top the reservoir up, approve the agent again
(step 5), and go through step 6.

`docker-compose.yml` builds each layer from its own folder with that layer's Dockerfile, and
gives each service only the variables it needs: the agent gets no DB password, the signer no
session keys. Only the gateway (and Postgres, on 127.0.0.1) is published. Start order:
postgres → migrate → signer → gateway → agent (the agent's sandbox resolves the gateway at
startup).

## Commands

| Command | Does |
|---|---|
| `./sigil init` | create `.env`, fill missing secrets and the agent wallet (never rotates existing ones) |
| `./sigil devnet [args]` | `crypto/scripts/setup_devnet.py` (args pass through, e.g. `--guardian <Phantom wallet>`), then writes `AUSD_MINT` |
| `./sigil up` | checks the agents' model keys, then `docker compose up -d --build --wait` |
| `./sigil reset-db [--yes]` | for a clean demo: backs the database up to `backups/`, deletes it, and starts the stack on an empty one (asks you to type `reset` unless `--yes`). Keeps `.env`, devnet and the signer's ledger; on-chain balances and delegations are as they were |
| `./sigil crypto <script> [args]` | any script in `crypto/scripts`, e.g. `crypto delegation revoke` |
| `./sigil <other>` | passed to `docker compose`: `down`, `ps`, `logs -f gateway`, `down -v` (wipes data) |

`./sigil up` always rebuilds from the working tree, so local changes in any folder are picked
up.

## Logs

Every service writes one line per event, in one format:

```
2026-10-19T18:04:08.981Z INFO uvicorn.access 172.18.0.1:54052 - "POST /api/v1/requests HTTP/1.1" 200
2026-10-19T18:04:09.412Z WARNING agent_layer.flows.policy_interview interview turn unavailable: LLMUnavailable: OpenRouter answered 402: …
```

UTC time, level, logger, message. Docker's health checks (`GET /healthz`, every 30 s once a
service is up) are not logged, and the services replace their tokens and keys before writing.
When the agent can't do its job (the model provider refuses, the gateway's answer is outside
the contract) it logs a `WARNING` with the reason, while the app only sees "unavailable".

```sh
./sigil logs -f                        # everything, live
./sigil logs --since 30m agent         # one service, recent
./sigil logs agent gateway | grep -E "WARNING|ERROR"
```

## Tests

Each layer is its own Python package (Python ≥ 3.12) with its own tests. Run them from the
layer's folder:

| Layer | Commands |
|---|---|
| Gateway | `uv sync --extra dev && uv run pytest` (tests under `tests/db/` need the gateway's Postgres: `docker compose up -d postgres` in `gateway/`) |
| Agent | `uv venv && uv pip install -e ".[dev]" && uv run pytest` (no network or API key needed) |
| Crypto | `uv sync --extra dev && uv run pytest` |

The gateway and crypto layers also run `ruff check`, `ruff format --check` and `mypy`
(strict). The contract parity tests compare the copies shared between layers (`gateway/` ↔
`agent/` and `gateway/` ↔ `crypto/`) and fail if they drift. [CI](.github/workflows/ci.yml)
runs all of it, plus a gitleaks secret scan over the whole history, on every push and pull
request. Reviewed false positives are listed in `.gitleaksignore`.

## Environment

All variables live in `.env` (template: `.env.example`).

**You set**

| Variable | Used by | Notes |
|---|---|---|
| `AGENT_LLM_PROVIDER` | agent | `openrouter` (template) or `anthropic`, for both agents |
| `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` | agent | the key for each provider in use; `./sigil up` refuses to start without it |
| `AGENT_MAKER_MODEL`, `AGENT_AUDITOR_MODEL` | agent | empty = the provider's default. OpenRouter's is Nemotron 3 Super, which is text-only: for receipt photos and PDFs, pick a multimodal model |
| `AGENT_OPENROUTER_VISION`, `AGENT_OPENROUTER_DOCUMENTS` | agent | `1` when the OpenRouter model reads images / PDFs. A model that can't is never shown those proofs and can't count them, so a text-only model still works |
| `SIWS_DOMAIN` | gateway | host of the `app_url` the iOS app gives Phantom |

**Generated by `./sigil init`**

| Variable | Used by | Notes |
|---|---|---|
| `SIGIL_OWNER_PASSWORD` | postgres, migrate | owner role, migrations only |
| `SIGIL_APP_PASSWORD` | postgres, gateway | app role |
| `SIGIL_SIGNER_RO_PASSWORD` | postgres, signer | read-only role for `registered_destinations` |
| `JWT_SECRET` | gateway | session tokens |
| `DATA_ENCRYPTION_KEY` | gateway | Fernet key, chats and proofs at rest |
| `AGENT_INBOUND_TOKEN` | gateway, agent | gateway → agent |
| `PLATFORM_MAKER_TOKEN`, `PLATFORM_AUDITOR_TOKEN` | gateway, agent | agent → gateway; must differ |
| `CRYPTO_HMAC_SECRET` | gateway, signer (as `SIGNER_HMAC_SECRET`) | gateway → signer |
| `AGENT_WALLET_PUBKEY` | gateway, signer | the delegate wallet; fund it with devnet SOL for fees |
| `AGENT_WALLET_KEY_ENC`, `SIGNER_MASTER_KEY` | signer | encrypted wallet key and its key |

**Filled by `./sigil devnet`**

| Variable | Used by | Notes |
|---|---|---|
| `AUSD_MINT` | gateway, signer | with `MOCK_CHAIN=true` any valid pubkey works |

**Optional** (defaults in `.env.example`)

| Variable | Used by | Default |
|---|---|---|
| `GATEWAY_BIND` | compose | `127.0.0.1`; `0.0.0.0` to reach it from a phone on your network |
| `POSTGRES_PORT` | compose | `5432` |
| `JWT_TTL_HOURS` | gateway | `12` (max) |
| `APPLE_APP_ID`, `APPLE_APP_ATTEST_PRODUCTION` | gateway | unset (iOS capture off), `true` |
| `CAMERA_ONLY_PROOFS` | gateway, agent | `false` |
| `RATE_LIMIT_PER_MIN`, `DEPENDENT_MESSAGES_PER_DAY`, `LLM_TOKEN_BUDGET_PER_GUARDIAN_PER_DAY` | gateway | `30`, `50`, `200000` |
| `AGENT_MAKER_PROVIDER`, `AGENT_AUDITOR_PROVIDER` | agent | `AGENT_LLM_PROVIDER`; e.g. a cheaper maker with a Claude Auditor |
| `AGENT_MAKER_EFFORT`, `AGENT_AUDITOR_EFFORT` | agent | `high` |
| `OPENROUTER_BASE_URL` | agent | `https://openrouter.ai/api/v1` |
| `AGENT_SCHEDULER` | agent | `1` |
| `PDF_ALLOWED_SIGNERS` | gateway | `LAZADA EXPRESS COMPANY LIMITED` (exact match). The trusted roots go in `config/pdf-trusted-roots.pem` (Lazada's certificate is issued by Thai Digital ID CA G3); while it holds no certificate, no PDF receipt counts |
| `SOLANA_RPC_URL` | signer | `https://api.devnet.solana.com` (devnet only) |
| `SIGNER_MAX_PER_TRANSFER_UNITS`, `SIGNER_MAX_PER_DAY_UNITS` | signer | 20,000 and 50,000 aUSD (amounts are sized for baht) |
| `MOCK_CHAIN` | signer | `false`; `true` skips the chain on transfers |

Service URLs (`AGENT_URL`, `CRYPTO_URL`, `PLATFORM_API_URL`, DB URLs) are fixed in
`docker-compose.yml`; there is nothing to set.
