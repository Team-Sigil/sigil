"""Which proofs count, and the keys that make a proof single-use.

Ported from the agent layer's reference engine (`mock_platform/app.py`, as of 72b80be).
"""

import re

from gateway.contracts.agent import PROOF_FLAGS_DISQUALIFYING, Proof, ProofFindingsRecord


def counts_as_proof(
    proof: Proof, *, camera_only: bool = False, origin: Proof | None = None
) -> bool:
    """An image, or text the platform extracted from one; never typed text, a duplicate or an
    AI-generated image. With `camera_only`, only a verified in-app capture (`source: "photo"`),
    or OCR text read from one on the same request (`origin`)."""
    if set(proof.flags) & PROOF_FLAGS_DISQUALIFYING or proof.source == "typed":
        return False
    is_image = proof.media_type.startswith("image/")
    if not (is_image or proof.source == "ocr"):
        return False
    if not camera_only:
        return True
    if is_image:
        return proof.source == "photo"
    return (
        origin is not None
        and origin.source == "photo"
        and origin.request_id == proof.request_id
        and not set(origin.flags) & PROOF_FLAGS_DISQUALIFYING
    )


def receipt_fingerprint(findings: ProofFindingsRecord) -> str:
    """The same receipt read twice gives the same fingerprint, whatever the photo."""

    def norm(value: str | None) -> str:
        return re.sub(r"\W+", "", (value or "").casefold())

    return "|".join(
        [
            norm(findings.merchant),
            re.sub(r"\D", "", findings.date),
            str(findings.total),
            norm(findings.receipt_number),
        ]
    )


def proof_keys(proof: Proof, findings: ProofFindingsRecord | None) -> list[str]:
    """A proof is used if its id, its content hash or its receipt fingerprint backed an earlier
    transfer in the family."""
    keys = [f"id:{proof.id}"]
    if proof.sha256:
        keys.append(f"sha256:{proof.sha256}")
    if findings:
        keys.append(f"receipt:{receipt_fingerprint(findings)}")
    return keys
