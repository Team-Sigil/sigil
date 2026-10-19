# Agent layer handoff: PDF receipts for online orders

> Written from [SPEC.md](../SPEC.md) and the [README](../README.md), which are the source of
> truth. If this guide disagrees with them, they win.

For Alwin, from the gateway. Gateway branch `feat/proof-after-payment`; checked against
agent-layer `main` at `0fbe4b2` (OpenRouter).

**Short version: the agent layer has to change.** A dependent who bought something online
can't photograph the receipt, so they upload the shop's PDF instead. The gateway verifies
the shop's digital signature and tells the agent whether it did. Today the agent skips any
`application/pdf` proof as an unsupported type, so a valid Lazada receipt would be ignored.
This note says what the gateway sends and what the agent needs to do with it.

Related but separate: proof now comes **after** payment (SPEC §8.9). That changes the
maker's `proof_required` guardrail and adds a settle call; it gets its own handoff. This one
covers only how a PDF proof is read.

## What the gateway does with a PDF

- **Typed from the bytes.** A file starting with `%PDF-` is a PDF, whatever its
  `Content-Type`. At most **5 MB** (your `max_proof_bytes`, so nothing the gateway accepts
  is too large for you), 3 proofs per request like any other proof.
- **Signature check at upload** (`proof_pdf.py`). The signed byte range must cover the whole
  file except the signature itself, the digest must match, the CMS signature must verify,
  the signing certificate must chain to a root in `PDF_TRUSTED_ROOTS_PATH` and have been
  valid now (the signing time in a signature is the signer's own claim, so it isn't
  relied on), be meant for signing (key usage and extended key usage, when present), and
  its organisation name must exactly match a name on `PDF_ALLOWED_SIGNERS`. **Only Lazada
  is accepted for now.**
- **Flag.** A PDF that passes gets `issuer_signed`. One that fails, for any reason, gets
  `unchecked_document`. A signature only proves that Lazada produced the file and nobody
  changed it. It doesn't prove the order is this dependent's or matches the amount asked;
  the findings and the engine still decide that.
- **What the agent receives** from `GET /proofs`:

  | Field | Signed PDF (`issuer_signed`) | Unchecked PDF (`unchecked_document`) |
  |---|---|---|
  | `media_type` | `application/pdf` | `application/pdf` |
  | `source` | `file` (the contract's values are fixed) | `file` |
  | `data_b64` | the PDF exactly as uploaded, base64 | `null`; withheld, never shown to the model |
  | `text` | `null` | `null` |
  | `sha256` | hash of the PDF bytes, so your existing hash check works | hash of the PDF bytes |
  | `uploaded_at` / `captured_at` | upload time / signing time | upload time / `null` |

  The model reads the PDF itself, so the agent needs a model that accepts documents (we're
  moving to a multimodal model).
- **Camera-only mode.** `CAMERA_ONLY_PROOFS` is about images. A signed PDF counts whether
  it is on or off, because an online order has no paper to photograph.
- **Engine rule** (`engine/proofs.py:counts_as_proof`): a PDF counts only with
  `issuer_signed` and no disqualifying flag (`duplicate`, `ai_generated`). After that, the
  single-use and coverage checks are the same as for a photo.

## What the agent layer needs to change

| Where | Today | Change |
|---|---|---|
| `contracts.py` `PROOF_FLAGS` | Doesn't know the two new flags; `proof_parts` logs `proof_flags_ignored` | Add `issuer_signed` to `PROOF_FLAGS_POSITIVE` and `unchecked_document` to `PROOF_FLAGS_DISQUALIFYING`. The gateway's `contracts/agent.py` is a verbatim copy, so we change both together (`test_contracts.py` checks it) |
| `maker/context.py` `_proof_parts` | `application/pdf` falls into `proof_skipped … unsupported type` | Signed PDF with `data_b64`: decode and run the same size and hash checks as for an image, then add a document block, e.g. `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": …}}`. Unchecked PDF: no content; add a note (below) and don't qualify it |
| `llm.py` / `llm_openrouter.py` / `config.py` | Know text and image blocks. `supports_images` is `True` for `AnthropicLLM` and comes from `AGENT_OPENROUTER_VISION` for `OpenRouterLLM`, which raises `LLMUnavailable` on an image it can't read. Maker and Auditor can use different providers | Mirror that for documents. Add `supports_documents` to the `LLM` protocol: `True` for `AnthropicLLM` (Claude reads PDFs; the `document` block passes through as is), and an `AGENT_OPENROUTER_DOCUMENTS` setting for `OpenRouterLLM`, which converts the block to a `file` part (`{"type": "file", "file": {"filename": "receipt.pdf", "file_data": "data:application/pdf;base64,…"}}`) and raises `LLMUnavailable` when it can't read documents. Pass `documents_readable=self._llm.supports_documents` into `prepare_request_input`, as `images_readable` is now; when it's false, treat a signed PDF like an unseen image (`proof_unseen`, a note, not qualified) |
| `maker/context.py` `typed` | `not is_image and source != "ocr"` treats every non-image as typed | A PDF with `issuer_signed` is a document, not the dependent's claim. Count it in `qualifying_ids` when paired and not disqualified |
| `maker/context.py` `camera_ok` / `_taken_in_app` | Non-images qualify in camera-only mode only as OCR from an in-app photo | Exempt signed PDFs, to match the engine |
| `auditor/auditor.py` | Gets `case.proof_images`; a text-only Auditor gets none and is told how many it can't see (`auditor_proof_unseen`) | Add the signed PDFs to the case the same way (e.g. `case.proof_documents`), labelled as signed Lazada documents. Send them only when the Auditor's own model `supports_documents`; otherwise tell it they exist and are unseen, as for images |
| `maker/prompts.py` | Proof means a photo of a receipt | Add one line: for online orders the dependent can upload the order's PDF receipt; the platform has already checked it came from Lazada unchanged; read the merchant, date, total and order number from it as usual |
| `i18n.py` | — | Text for the unchecked case (below) |
| `mock_platform` and tests | Images and text only | Seed a signed-PDF proof (`data_b64` + `issuer_signed`) and an unchecked one (no content + `unchecked_document`), and test both paths, including camera-only on and a model without document input |

Notes for the model, roughly:

- signed: "Document proof {n} is a PDF receipt signed by Lazada: the platform verified it
  came from the shop unchanged. It doesn't show who placed the order, so check it matches
  the request."
- unchecked: "A PDF was attached, but its signature couldn't be verified, so you can't see
  it and it can't be used as evidence. Ask the dependent to upload the original receipt PDF
  from their Lazada order, not a copy, screenshot or printout."

## Findings from a PDF

`ProofFindings` stays the same. Please fill `receipt_number` with the **Lazada order
number** whenever the receipt shows one. It is part of the receipt fingerprint
(`merchant | date | total | receipt_number`) that stops one receipt backing two transfers,
including across families. Without it, two different orders from the same shop on the same
day for the same total would look identical.

## What stays the same

- Proof pairing, `request_id` / `dependent_id` checks and the 3-proof limit.
- `duplicate` still disqualifies (same PDF bytes uploaded before, by any of the guardian's
  dependents).
- Image proofs, OCR proofs and typed text are unchanged.

## Checked against a real receipt (2026-10-19)

A real Lazada Thailand invoice is signed and verifies: the signature covers the whole file,
the digest and signature match, and the certificate may sign. Its signer is
`LAZADA EXPRESS COMPANY LIMITED` (now the default `PDF_ALLOWED_SIGNERS`), issued by Thai
Digital ID CA G3 under Thailand's ETDA root; configure that CA in `PDF_TRUSTED_ROOTS_PATH`.
Its signature is BER-encoded and ends in zero bytes, which the verifier first broke by
stripping them as padding; fixed, with a regression test. Until the CA is configured, every
Lazada PDF still arrives as `unchecked_document`, and the agent asks for the original.
