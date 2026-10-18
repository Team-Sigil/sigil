# Proofs: limits, integrity and open questions

For the platform API team. This note covers how the agent layer handles proofs
(receipts, quotes, photos) that dependents upload: image limits on the Claude API side,
how forged, reused and mis-paired proofs are caught, and the decisions we need from you. Claude API figures come from Anthropic's
[vision docs](https://platform.claude.com/docs/en/build-with-claude/vision), checked on
2026-10-18 for `claude-opus-5-5`.

## What the agent layer does today

The agent reads proofs from `GET /proofs/{id}` and never trusts the platform's metadata
on its own. Rules are in `maker/context.py` and `config.py`.

| Check | Rule | If it fails |
|---|---|---|
| Proofs per request | The first 3 refs on the request; the rest are ignored | Logged as `proofs_capped` |
| Media type | `image/jpeg`, `image/png`, `image/webp`, `image/gif`, or `text/plain` | Proof skipped |
| Encoding | `data_b64` must be valid base64 | Proof skipped |
| Size | At most 5 MB **decoded** | Proof skipped |
| Declared size | `size_bytes` must equal the decoded length exactly | Proof skipped, and an Auditor review is forced (treated as possible tampering) |
| Response size | A `GET /proofs/{id}` body over ~8 MB is not read | The whole run fails closed (`502` to the gateway) |
| Contract | `data_b64` over 8 MiB of characters | The whole run fails closed |
| Pairing | `request_id` and `dependent_id` must match the request | Another request's proof: skipped, review forced. Missing: shown, but doesn't count as proof |
| Hash | `sha256`, when set, must match the bytes | Proof skipped, review forced |
| Source | Text counts only with `source: "ocr"` (extracted by you from a document) | Typed or unknown text is shown as the dependent's claim, not evidence |
| Flags | `duplicate`, `ai_generated` | Shown, doesn't count as proof, review forced |
| Flags | `near_duplicate`, `screenshot`, `no_camera_metadata`, `old_capture`, `edited`, `ai_suspected`, `rephotographed_screen` | Shown, and an Auditor review is forced |
| Flags | `camera_signed` | Shown as a note; changes no rule. Unknown flags are ignored |
| Camera-only (`AGENT_CAMERA_ONLY_PROOFS=1`, off by default) | Only `photo` images, and `ocr` text whose `derived_from` is a `photo` on the same request, count | Anything else is shown but doesn't count |

The agent does **not** check:

- **Dimensions.** The agent zone's sandbox has no image library, so it cannot read width
  and height.
- **That the bytes match `media_type`.** A PNG labelled `image/jpeg` is sent to Claude as
  JPEG.
- **Animation.** Claude reads only the first frame of an animated GIF or WebP.

An image that the Claude API rejects (wrong type, corrupt, too large) makes the model
call fail. The maker then ends the run with `safe_fallback` and asks the guardian; an
Auditor failure counts as a flag. No money moves, but the request isn't judged either.

## Claude API limits

| Limit | Claude API (direct) | Bedrock / Google Cloud |
|---|---|---|
| Size per image | 10 MB, base64-encoded | 5 MB, base64-encoded |
| Request size | 32 MB | Lower; platform-specific |
| Max dimensions | 8000 × 8000 px | same |
| Requests with more than 20 images | Each image at most 2000 px per side | same |
| Formats | JPEG, PNG, GIF, WebP (first frame only) | same |

**Resolution and cost.** Claude reads images in 28 × 28 px patches:
`⌈width/28⌉ × ⌈height/28⌉` tokens. `claude-opus-5-5` is in the high-resolution tier:
images above **2576 px on the long edge or 4784 tokens** are downscaled automatically,
keeping the aspect ratio. Accuracy drops on images under 200 px.

| Image | Tokens on Opus 5.5 | Cost per 1,000 at $4 / MTok |
|---|---|---|
| Receipt scan, 1568 × 1176 | 2,352 | ~$9.40 |
| 1920 × 1080 photo | 2,691 | ~$10.80 |
| Phone photo, 4032 × 3024 (downscaled) | 4,784 (cap) | ~$19.10 |

## Why it matters for us

- **Images are sent many times per run.** Proof images sit in the maker's first message,
  which is re-sent on every model turn (up to 8), and the Auditor sends them again in
  each of its 2 calls. Today only the system prompt is cached, so every re-send is billed
  in full. Worst case: 3 phone photos ≈ 14,400 tokens per call, about 144,000 tokens or
  ~$0.57 per run in images alone. (The agent team can add a cache breakpoint on the first
  turn; that turns most re-sends into cache reads.)
- **Payload size.** 3 proofs at our 5 MB cap are ~20 MB of base64 per model request,
  well under 32 MB but slow, and that is re-sent on every turn too.
- **Deploying on Bedrock or Google Cloud** lowers the per-image limit to 5 MB *encoded*,
  about 3.75 MB of raw image. Our 5 MB cap would let through images those platforms
  reject.
- **Privacy.** Phone photos usually carry EXIF metadata, often with GPS location.
  Claude doesn't read it, but we store and forward it, and the dependent may be a minor.

## Proposal: normalise at upload

The platform is the only component that can open images safely: it runs outside the
agent sandbox and before anything is stored. We propose the upload endpoint:

1. **Decodes the image** and rejects anything that isn't a valid JPEG, PNG or WebP, based
   on its bytes, not the client's `Content-Type`. Rejects animated images.
2. **Strips all metadata** (EXIF, GPS, XMP) by re-encoding.
3. **Resizes** so the long edge is at most **2576 px** (see question 1 for a lower
   limit). Never upscales. Rejects images under 200 px on the short edge.
4. **Re-encodes** to JPEG (quality ~85) or PNG for screenshots, keeping the result under
   **3.75 MB** so it works on every Claude platform.
5. **Stores `size_bytes` as the exact length of the stored bytes,** and `media_type` from
   the re-encoded format.
6. **Caps uploads** at the gateway (for example 15 MB raw) and at **3 proofs per
   request**, so the agent's cap never silently drops a proof.

Once this ships, the agent can lower `max_proof_bytes` to match, and treat a proof that
breaks these rules as tampering.

## Proof integrity

A receipt photo can't be made unforgeable. The goal is to make forging and reusing
proofs harder, cap what a forged one can get (never more than `max_per_request`, never
past the weekly cap), and show the guardian what's going on.

### What we need on `GET /proofs/{id}`

New optional fields on `Proof` (in `contracts.py`):

| Field | Meaning |
|---|---|
| `request_id`, `dependent_id` | The request and dependent the proof was uploaded for. **Without them a proof never counts as proof** |
| `uploaded_at` | When it was uploaded |
| `sha256` | Hex SHA-256 of the stored bytes (decoded image, or UTF-8 text) |
| `source` | `photo` (a **verified** in-app capture, see [Verified in-app capture](#verified-in-app-capture)), `file` (gallery or file upload, or a capture that failed verification), `ocr` (text you extracted from an uploaded document), `typed` |
| `derived_from` | For `ocr` text: the id of the image proof it was read from |
| `flags` | Any of `duplicate`, `ai_generated`, `near_duplicate`, `screenshot`, `no_camera_metadata`, `old_capture`, `edited`, `ai_suspected`, `rephotographed_screen`, `camera_signed` |

### What the platform should do at upload

1. Bind the proof to the request it was uploaded for, and refuse to attach it to any
   other request.
2. Hash the normalised image (SHA-256) and compute a **perceptual hash** (pHash or
   dHash), which survives cropping, resizing and re-compression.
3. Compare both against every earlier proof **in the family** (all dependents of the
   guardian): exact match → `duplicate`; perceptual match → `near_duplicate`.
4. Read the metadata **before stripping it**: no camera make and model → `no_camera_metadata`;
   screen-sized with no camera data → `screenshot`; capture date more than 30 days
   before upload → `old_capture`; editing software in the metadata → `edited`.
5. Prefer the in-app camera (`source: "photo"`) for receipts; a file upload is easier to
   fake.

The mock (`mock_platform/app.py`, `POST /mock/requests/{rid}/proofs`) does steps 1–3
with exact hashes only, as a reference.

### What the engine must enforce

`TransferInstruction` now carries `proof_ids` (chosen by agent code, not the model)
and `proof_findings` (merchant, date, total in base units, receipt number, whether it
matches the claim, read by the model). For a request transfer at or above the
guardian's proof threshold, the engine must:

1. Check every proof in `proof_ids` exists and belongs to this request and dependent.
   Otherwise: `proof not for this request`.
2. Require at least one that counts as proof (an image or `ocr` text, not typed, not
   `duplicate`) and hasn't been used. Otherwise: `proof required` or `proof already used`.
3. Require `proof_findings` with `matches_claim` and `total` ≥ the amount. Otherwise:
   `proof does not cover the transfer`.
4. Treat a proof as used if its id, its `sha256`, **or its receipt fingerprint**
   (normalised merchant + date + total + receipt number) was used by an earlier transfer
   in the family. The fingerprint catches a new photo of the same receipt.
5. Mark the proofs used **in the same database transaction as the transfer**, and only
   if the transfer goes out.

The agent applies the same rules before calling you, but the engine is the one that
holds: a compromised maker calling `POST /transfers` directly is refused.
`mock_platform/app.py` (`engine_transfer`) implements this as a reference.

### Verified in-app capture

Receipts are taken only with the app's own camera, and each photo is signed on the
device at the moment of capture. "Fingerprint" covers two different jobs here:

| | Cryptographic hash (SHA-256), signed | Perceptual hash (pHash, dHash) |
|---|---|---|
| Changes when one pixel changes | Yes | No |
| Job | **Tamper evidence:** any edit after capture breaks it | **Reuse detection:** the same receipt photographed or cropped again still matches |
| Who computes it | The app, at capture; the platform re-checks | The platform, at upload |

**Once per device: enrol a signing key.**

1. The app creates a key pair in secure hardware (Android Keystore with StrongBox, or
   the iOS Secure Enclave). The private key can't be exported.
2. It proves the key lives in a genuine, unmodified copy of our app on a real device:
   Android key attestation plus Play Integrity, or iOS App Attest.
3. The platform verifies that and registers the public key for this dependent and
   device. A device that fails attestation (rooted, emulator, modified app) can still
   upload, but its photos are `source: "file"`.

**Each capture:**

1. The dependent taps "add receipt" on a request. The app asks the platform for a
   **capture ticket**: a random single-use nonce bound to that request, the dependent
   and the device key, valid for about 5 minutes.
2. The app opens its own camera view. **There is no gallery picker** for receipts.
3. On capture, the app hashes the exact encoded image bytes and signs a claim with the
   device key: `{sha256, nonce, request_id, captured_at, device_key_id, app_version}`.
   Writing the claim as a **C2PA manifest** (with the open-source `c2pa-rs` library)
   keeps the format standard and checkable with existing tools, rather than inventing
   our own.
4. The app uploads the image and the signed claim. Nothing touches the image in between.

**The platform verifies, in order:** the key is registered and attested for this
dependent; the signature is valid; the nonce is unused, unexpired and matches the
request; the SHA-256 of the received bytes equals the claim; `captured_at` is inside
the ticket's window. Only then does it set `source: "photo"`. Any failure → `file`.

**Then it normalises.** It keeps the signed original (encrypted) as evidence, and makes
the resized, metadata-stripped copy the agent sees, as in the
[upload proposal](#proposal-normalise-at-upload). The copy's own SHA-256 is the
`sha256` the agent checks, and the audit log links copy to original. Resizing breaks the
device signature, which is why the platform verifies **before** normalising.

**OCR text** extracted from a photo should carry `derived_from: "<photo proof id>"`, so
the agent can tell text read from a verified photo from text read from an uploaded file.

**What this stops:**

- **Editing a photo after taking it.** The hash no longer matches the signature.
- **Uploading a gallery image, a screenshot, a downloaded receipt or an AI-generated
  file.** There is no way to upload one as a photo.
- **Replaying an old photo** or attaching one photo to a second request. The nonce is
  single-use and bound to one request.
- **Faking a capture from a script.** There is no attested key to sign with.

**What it doesn't stop:**

- **Photographing something fake** (the "analog hole"): a printed fake receipt, or a
  screen showing a generated or edited one. Mitigations: a screen-detection check at
  upload (moiré patterns, pixel grids, refresh banding) setting a new flag
  `rephotographed_screen`, which forces an Auditor review; plus the perceptual hash,
  which catches the same fake shown twice.
- **Photographing someone else's real receipt.** Reuse checks only cover one family. A
  **platform-wide** receipt fingerprint (merchant + date + total + receipt number,
  stored as a keyed hash so it reveals nothing) would catch the same receipt used by
  two families.
- **Compromised devices.** Attestation makes hooking the camera much harder, not
  impossible. Treat it as raising the cost, and keep the review and reuse rules.

**Agent layer: done.** With `AGENT_CAMERA_ONLY_PROOFS=1`, only `photo` images, and
`ocr` text whose `derived_from` is a non-duplicate `photo` on the same request, count
as proof. File uploads are still shown, with a note that they can't be used as
evidence. It's **off by default**: turn it on when the app ships the camera flow, and
turn on the same rule in the engine at the same time (the mock's
`camera_only_proofs` is the reference). Until then, today's rule applies.

### AI-generated images

Claude can't tell a generated or edited image from a real photo (Anthropic's vision docs
say so), so the agent can't do this check itself. Three kinds of tools could, each with
real limits. Facts below were checked on 2026-10-18.

| Tool | What it tells you | Limits for us |
|---|---|---|
| **Watermark detection** (Google SynthID) | The image was made or edited by a generator that embeds SynthID: Google's own models and, per Google, partners such as OpenAI, NVIDIA and Kakao | **Public since 2026-10-07, but only as a web portal; no API has been announced**, so it can't run on every upload. A hit is strong evidence; **no hit proves nothing**, because many generators, and every non-AI editing tool, add no watermark |
| **Provenance** (C2PA Content Credentials) | A signed record of where the image came from: a camera that signs at capture (e.g. Google Pixel's camera app by default, some Leica, Sony and Canon models), or a generator or editor that records "AI-generated" or "edited" | Most phones (including iPhones) don't sign photos, and credentials are easy to strip, so **a missing credential means nothing**. A credential proves where the image came from, not that the receipt is genuine. It must be read **before** our upload step strips metadata |
| **Classifiers** (third-party "is this AI?" models) | A probability that an image is generated | Probabilistic: false positives on blurry or low-light phone photos (exactly what kids upload), easy to evade with re-photographing or noise, and results change with every model release. Uploading a minor's photos to a third-party service also needs a data-processing agreement |

**Generative AI isn't the main way to fake a receipt.** Receipt-generator websites,
ordinary photo editing, and reusing someone else's real receipt are cheaper and leave
no AI watermark. AI detection covers one slice of forgery; the pairing, reuse and
review rules above cover more.

**Proposal:**

1. **In-app camera first.** See [Verified in-app capture](#verified-in-app-capture). A
   generated image would then have to be held up to the camera, which is more effort,
   and the screen-detection flag is aimed at exactly that.
2. **Read C2PA at upload, before stripping metadata.** A credential that says
   "AI-generated" or records an AI edit → new flag `ai_generated`. A valid camera
   capture signature → new flag `camera_signed` (a positive signal, shown to the model
   and the Auditor). No credential → nothing.
3. **SynthID when an API exists.** Until then, it's a manual tool: a guardian or support
   person can check a disputed receipt in the portal. If Google offers an API, run it at
   upload and set `ai_generated` on a hit.
4. **Classifiers only as a review trigger, never a rejection.** A high score → new flag
   `ai_suspected`, which forces an Auditor review. Never refuse a proof on a classifier
   alone; that would punish honest kids with bad photos.
5. **A detector never approves anything.** No result makes a proof count that otherwise
   wouldn't, and a clean result is not evidence the receipt is real.

**Agent layer: done.** `ai_generated` makes a proof not count, like `duplicate`, and
forces a review. `ai_suspected` and `rephotographed_screen` force a review. `camera_signed`
is the only *positive* flag: shown as a note, it changes no rule. The engine (mock)
refuses `ai_generated` proofs too.

### What stays possible

A convincing forgery for a shop never used before still gets through, up to
`max_per_request`: today as an uploaded file, and with verified capture only as a
printed fake or a screen held up to the camera. The weekly summary and the Auditor (which reviews
anything at or above the review threshold) are what catch patterns. The real fix is not
needing proof at all: pay the merchant directly, or verify e-receipts with merchants or
payment networks.

## Questions for the API team

1. **Long-edge limit.** Should it be 2576 px (best for small print) or 1568 px (about half
   the tokens and cost)? Test a sample of real receipts at both sizes before deciding.
2. **Which proofs count.** When a request has more than 3, the agent uses the first 3.
   Should it be the 3 most recent? Or should the platform refuse a 4th upload?
3. **Where we deploy.** Claude API direct, Bedrock or Google Cloud? This decides the
   per-image size limit.
4. **PDF receipts.** Should we support `application/pdf`? Claude accepts PDFs as
   document blocks, but the agent doesn't send them today.
5. **Rejection message.** What does the dependent see when an upload is rejected, so
   the agent's "please send a clearer photo" reply matches it?
6. **Perceptual hash and threshold.** Which algorithm (pHash, dHash) and what distance
   counts as a near-duplicate? Too strict misses re-photographed receipts; too loose
   flags every receipt from the same shop.
7. **Receipt fingerprint collisions.** Two real purchases with the same merchant, date
   and total but no receipt number would collide. Is that rare enough to leave to the
   guardian, or should OCR always try to extract a receipt number?
8. **Old proofs.** Should a proof uploaded for one request be usable for a follow-up
   request (for example, "half now, half after the receipt")? Today it's single-use.
9. **Verified capture.** Can the mobile team build the camera-only flow (hardware key,
   attestation, capture tickets, C2PA claim)? What happens on devices without secure
   hardware or that fail attestation: `file` and guardian review, or no receipts at all?
   And is a platform-wide receipt fingerprint acceptable on privacy grounds?
10. **AI detection.** Do we read C2PA at upload, and is a third-party classifier or
   screen-detection model worth the cost, false positives and data-processing agreement?
   Who watches for a SynthID API?
11. **Files API.** Uploading images once to Anthropic's Files API and referencing them by
   `file_id` would keep request payloads small. The catch: files then stay stored at
   Anthropic until deleted (inline images are not kept after the request), which is a
   retention decision for photos that may show a minor. Someone also has to hold an
   Anthropic credential to upload and delete them. Worth it?
