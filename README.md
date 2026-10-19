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

## Quick start

Needs Docker and openssl. No Python or uv on the host: the devnet scripts run inside the
signer image.

```sh
git clone git@github.com:Team-Sigil/sigil.git && cd sigil
./sigil init      # creates .env with generated secrets and the agent wallet
# set the model provider key (OPENROUTER_API_KEY or ANTHROPIC_API_KEY) and SIWS_DOMAIN in .env
./sigil devnet    # aUSD mint + demo accounts on devnet, writes AUSD_MINT
./sigil up        # build and start; gateway on http://127.0.0.1:8000
```

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
| `./sigil crypto <script> [args]` | any script in `crypto/scripts`, e.g. `crypto delegation revoke` |
| `./sigil <other>` | passed to `docker compose`: `down`, `ps`, `logs -f gateway`, `down -v` (wipes data) |

`./sigil up` always rebuilds from the working tree, so local changes in any folder are picked
up.

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
| `SIGNER_MAX_PER_TRANSFER_UNITS`, `SIGNER_MAX_PER_DAY_UNITS` | signer | 200 and 300 aUSD |
| `MOCK_CHAIN` | signer | `false`; `true` skips the chain on transfers |

Service URLs (`AGENT_URL`, `CRYPTO_URL`, `PLATFORM_API_URL`, DB URLs) are fixed in
`docker-compose.yml`; there is nothing to set.
