# Proof images: limits and open questions

For the platform API team. This note covers how the agent layer handles proof images
(receipts, quotes, photos) that dependents upload, the limits on the Claude API side,
and the decisions we need from you. Claude API figures come from Anthropic's
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
6. **Files API.** Uploading images once to Anthropic's Files API and referencing them by
   `file_id` would keep request payloads small. The catch: files then stay stored at
   Anthropic until deleted (inline images are not kept after the request), which is a
   retention decision for photos that may show a minor. Someone also has to hold an
   Anthropic credential to upload and delete them. Worth it?
