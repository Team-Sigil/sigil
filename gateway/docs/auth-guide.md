# Sign-In With Solana (SIWS) for Sigil

> Written from [SPEC.md](../SPEC.md) and the [README](../README.md), which are the source of
> truth. If this guide disagrees with them, they win.

How users sign in to the gateway with their Phantom wallet, and what each side needs to make
it work. The App API reference is in the [README](../README.md) ("App API"); the
multi-dependent changes are in [app-handoff.md](app-handoff.md).

## How it works

There are no passwords. The user proves they own a Solana wallet by signing a one-time text
with Phantom, and the gateway checks the signature.

```
iOS app                     Gateway                         Phantom
   │ POST /auth/nonce {wallet} ──▶ creates a single-use nonce
   │ ◀── {nonce, message, expires_at}
   │ signMessage(message) ─────────────────────────────────▶ user approves
   │ ◀────────────────────────────────────────── signature (base58)
   │ POST /auth/verify {wallet, message, signature_b58} ──▶ checks it
   │ ◀── {token, role, family_id, family_ids}
   │ Authorization: Bearer <token> on every later call
```

The message the gateway builds looks like this:

```
sigil.app wants you to sign in with your Solana account:
Dpz6xo6tnBkxeKLoD1tEV6tPv21SSJPLEVNpsfv8ZrDN

Sign in to Sigil.

Nonce: bwF3KaT5z4N8qRzangyigaXeZb-uidbE
Issued At: 2026-10-19T04:55:32Z
Expiration Time: 2026-10-19T05:00:32Z
```

When you call `/auth/verify`, the gateway:

- rebuilds this text from its own records and requires an exact match;
- verifies the ed25519 signature against the wallet;
- uses up the nonce (one use, 5 minutes, tied to that wallet and to `purpose: "login"`; a
  failed attempt also uses it up);
- returns a JWT valid for at most 12 hours.

**Role is automatic.** A wallet registered as a dependent signs in as a dependent; every
other wallet is a guardian. A new wallet becomes a guardian with `family_ids: []`, which
means "show onboarding".

## 1. Backend setup (the gateway)

Three settings in `.env`:

| Variable | What to set | Why |
|---|---|---|
| `JWT_SECRET` | a long random value: `openssl rand -hex 32` | signs session tokens; never commit it |
| `SIWS_DOMAIN` | your app's domain, e.g. `sigil.app` | the first line of the message; must equal the host of the `app_url` the app gives Phantom (see below) |
| `JWT_TTL_HOURS` | `12` or less | session length (capped at 12 by SEC-09) |

Your own `.env` may still say `SIWS_DOMAIN=localhost:3000` from the web-app days; set it to
the host of the `app_url` your app gives Phantom.

Then start the stack (Postgres holds the nonces):

```fish
docker compose up -d --build
curl -s http://127.0.0.1:8000/healthz
```

**Test it without a phone.** This script stands in for Phantom with a throwaway key:

```python
import base58, httpx
from nacl.signing import SigningKey

key = SigningKey.generate()                      # Phantom holds this in the real app
wallet = base58.b58encode(bytes(key.verify_key)).decode()

with httpx.Client(base_url="http://127.0.0.1:8000") as c:
    message = c.post("/api/v1/auth/nonce",
                     json={"wallet": wallet, "purpose": "login"}).json()["message"]
    signature = base58.b58encode(key.sign(message.encode()).signature).decode()
    session = c.post("/api/v1/auth/verify", json={
        "wallet": wallet, "message": message, "signature_b58": signature}).json()
    print(c.get("/api/v1/me",
                headers={"Authorization": f"Bearer {session['token']}"}).json())
```

Save it as a file and run it with `uv run python <file>` from the repo.

## 2. App side (iOS with Phantom)

**Use Phantom's `signMessage`, not `signIn`.** Phantom's `signIn` builds its own message,
with extra fields and formatting that won't match the gateway's text, so verification fails.
Have Phantom sign the gateway's `message` byte for byte (SPEC §15).

A native iOS app talks to Phantom through **Phantom's deeplinks**: Phantom opens, the user
approves, and Phantom redirects back to your app with an encrypted reply. These steps follow
Phantom's deeplink docs; check parameter names against the current docs.

**Setup, once:**

- Register a redirect URL scheme (e.g. `sigil://`) or a universal link.
- Add a NaCl library (e.g. `swift-sodium` or `TweetNacl`) and a Base58 library.
- Generate an x25519 keypair for the app (`dapp_encryption_public_key`) and keep it in the
  Keychain.

**Step A: connect, once per Phantom session.** Open:

```
https://phantom.app/ul/v1/connect
  ?app_url=https://sigil.app          ← its host must equal SIWS_DOMAIN
  &dapp_encryption_public_key=<base58 x25519 public key>
  &redirect_link=sigil://onConnect
  &cluster=devnet
```

Phantom redirects back with `phantom_encryption_public_key`, `nonce` and `data`. Then:

1. Compute the shared secret: `box.before(phantomPublicKey, appSecretKey)`.
2. Decrypt `data` with it and the `nonce`. You get `{public_key, session}`. `public_key` is
   the **wallet address**.
3. Keep `session` and the shared secret for the next step.

**Step B: get the text from the gateway.**

```
POST /api/v1/auth/nonce   {"wallet": "<public_key>", "purpose": "login"}
→ {"nonce": "...", "message": "sigil.app wants you to sign in ...", "expires_at": "..."}
```

**Step C: have Phantom sign it.**

1. Encrypt this JSON with the shared secret and a fresh 24-byte nonce:

   ```json
   {"message": "<base58 of the UTF-8 bytes of message>", "session": "<session>", "display": "utf8"}
   ```

2. Open:

   ```
   https://phantom.app/ul/v1/signMessage
     ?dapp_encryption_public_key=...&nonce=<base58 nonce>&redirect_link=sigil://onSign&payload=<base58 ciphertext>
   ```

3. Phantom shows the text, and the user taps Approve. Decrypt the reply's `data` to get
   `{signature}` (base58).

**Step D: exchange the signature for a token.**

```
POST /api/v1/auth/verify
{"wallet": "<public_key>", "message": "<message, unchanged>", "signature_b58": "<signature>"}
→ {"token": "...", "role": "guardian", "family_id": null, "family_ids": []}
```

Keep the token in the Keychain and send `Authorization: Bearer <token>` on every call.

**If Phantom shows a red "domain mismatch" warning:** Phantom recognises sign-in style
messages and compares the domain on the first line with your `app_url`. Make sure
`SIWS_DOMAIN` equals the `app_url` host.

## 3. After sign-in

| `role` | `family_ids` | Show |
|---|---|---|
| guardian | `[]` | onboarding: register the first dependent |
| guardian | one id | guardian home |
| guardian | several | dependent picker, then send `X-Family-Id` (see `app-handoff.md`) |
| dependent | one id | dependent home |

- **No refresh token.** When any call returns `401`, run the sign-in again.
- **Sign-out:** delete the token from the Keychain.

## 4. The same pattern for guardian approvals

Registering a dependent, activating a policy, answering a question and freezing all reuse the
same steps:

1. Ask `POST /auth/nonce` with the matching `purpose`: `register-dependent`,
   `activate-policy`, `answer-question` or `freeze`.
2. Build the text in the README's "Guardian signatures" section.
3. Sign it with Phantom `signMessage`.
4. Send the signature and nonce to that route.

For these purposes the nonce response has `message: null`, because the app builds the text
itself.

## 5. Errors

| Response | Meaning | What to do |
|---|---|---|
| `401` "Nonce is invalid, expired or already used." | more than 5 minutes passed, or the nonce was already used | get a new nonce and sign again |
| `401` "Signature does not match the sign-in message." | the text changed (whitespace, line breaks, a trailing newline) or the wrong key signed | sign the `message` exactly as received |
| `401` "Not a sign-in message." | there is no `Nonce:` line | send the gateway's message unchanged |
| `422 VALIDATION_ERROR` | the wallet or signature isn't valid base58 | check the encoding |
| `429 RATE_LIMITED` | too many attempts from one address | wait a minute |

## Checklist

- [ ] `JWT_SECRET` set to a random value
- [ ] `SIWS_DOMAIN` = host of Phantom's `app_url`
- [ ] Phantom in testnet mode (devnet)
- [ ] App uses `signMessage` (not `signIn`) on the exact gateway text
- [ ] Token in the Keychain; on `401`, sign in again
