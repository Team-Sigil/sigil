"""Proof integrity: what counts as proof, request pairing, forged and reused proofs.
The agent checks what it can see; the engine has the final say, so a compromised maker
calling it directly is refused too."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from agent_layer.contracts import Outcome, Proof, ProofRef
from agent_layer.money import to_base
from mock_platform.app import ADMIN_TOKEN, AUDITOR_TOKEN, MAKER_TOKEN
from tests.conftest import AGREE
from tests.fakes import all_text, findings, finish, reply, transfer

T = to_base
PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-pixels" * 20


def _image(pid: str, data: bytes = PNG, **kw) -> Proof:
    return Proof(id=pid, media_type="image/png", size_bytes=len(data), data_b64=base64.b64encode(data).decode(), **kw)


def _ref(pid: str, media_type: str = "image/png") -> ProofRef:
    return ProofRef(id=pid, media_type=media_type)


def _send_30(make_agent, *, proof=None, auditor=AGREE):
    return make_agent(
        [reply(transfer(30, reasoning="Receipt matches a school calculator.", proof=proof or findings(30))),
         reply(finish("sent_full", "Sent!"))],
        auditor,
    )


def _paid_first(platform, record):
    """The proof didn't count, so nothing relied on it: the money went out first, after an
    Auditor review, and the receipt is still owed."""
    assert platform.sent and platform.sent[-1].proof_ids == []
    assert "pay_first" in " ".join(record.guardrail_events)
    reviewed = [v.submission for v in platform.verdicts.values()]
    assert any("pay_first" in v.review_triggers for v in reviewed)
    assert len(platform.open_obligations("d1")) == 1


# ------------------------------------------------------------ what counts as proof


def test_a_valid_image_proof_unlocks_the_threshold(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))
    agent, *_ = _send_30(make_agent)
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL
    assert platform.sent[0].proof_ids == ["p1"]
    assert platform.sent[0].proof_findings.total == T(30)


@pytest.mark.parametrize("proof,ref_type,event", [
    (Proof(id="p1", media_type="application/zip", size_bytes=3, text="zip"), "application/zip", "unsupported type"),
    (_image("p1").model_copy(update={"size_bytes": 5}), "image/png", "size mismatch"),
    (_image("p1", sha256="0" * 64), "image/png", "hash mismatch"),
])
def test_a_skipped_proof_is_not_relied_on(platform, make_agent, add_request, proof, ref_type, event):
    add_request(amount=30, proofs=[_ref("p1", ref_type)])
    platform.add_proof(proof)
    agent, maker, _ = _send_30(make_agent)
    record = agent.handle_request("r1")
    assert any(event in e for e in record.guardrail_events)
    _paid_first(platform, record)
    assert "None of the proofs can be used as evidence" in all_text(maker.calls[0]["messages"])


@pytest.mark.parametrize("source", ["typed", None])
def test_typed_text_is_a_claim_not_proof(platform, make_agent, add_request, source):
    add_request(amount=30, proofs=[_ref("p1", "text/plain")])
    platform.add_proof(Proof(id="p1", media_type="text/plain", size_bytes=20, text="Office Mart 30.00", source=source))
    agent, maker, _ = _send_30(make_agent)
    record = agent.handle_request("r1")
    _paid_first(platform, record)
    assert "typed by the dependent" in all_text(maker.calls[0]["messages"])


def test_proof_findings_are_required_and_checked(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))
    agent, maker, _ = make_agent([
        reply(transfer(30)),                                        # no findings
        reply(transfer(30, proof=findings(30, matches=False))),     # doesn't match the claim
        reply(finish("declined", "That receipt doesn't match.")),
    ])
    record = agent.handle_request("r1")
    codes = " ".join(record.guardrail_events)
    assert "proof_findings_required" in codes and platform.sent == []


def test_transfer_is_capped_at_the_proof_total(platform, make_agent, add_request):
    add_request(amount=35, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))
    agent, *_ = make_agent(
        [reply(transfer(35, proof=findings(28))), reply(transfer(28, proof=findings(28))),
         reply(finish("sent_partial", "Sent what the receipt covers."))],
        AGREE,
    )
    record = agent.handle_request("r1")
    assert "proof_total_too_low" in " ".join(record.guardrail_events)
    assert record.outcome is Outcome.SENT_PARTIAL and record.amount_sent == T(28)


# ------------------------------------------------------------------- pairing


def test_a_proof_for_another_request_is_skipped(platform, make_agent, add_request):
    add_request("r0", amount=5)
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", request_id="r0", dependent_id="d1"))
    agent, maker, _ = _send_30(make_agent)
    record = agent.handle_request("r1")
    _paid_first(platform, record)
    assert "p1 belongs to another request" in " ".join(record.guardrail_events)
    assert "platform_text_anomaly" in all_text(maker.calls[0]["messages"])  # forces a review


def test_a_proof_the_platform_cant_pair_is_shown_but_does_not_count(platform, make_agent, add_request):
    # Two requests reference the same proof, so the platform can't say whose it is.
    add_request("r0", amount=30, proofs=[_ref("p1")])
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))
    agent, maker, _ = _send_30(make_agent)
    record = agent.handle_request("r1")
    _paid_first(platform, record)
    assert "proof_unpaired: p1" in record.guardrail_events
    assert "didn't confirm image proof 0 was uploaded for this request" in all_text(maker.calls[0]["messages"])


# ----------------------------------------------------------- platform flags


def test_flagged_proof_forces_a_review_and_is_shown(platform, make_agent, add_request):
    add_request(amount=10, proofs=[_ref("p1")])  # 10.00 normally skips the Auditor
    platform.add_proof(_image("p1", flags=["screenshot", "made up by the platform"]))
    agent, maker, _ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))], AGREE)
    record = agent.handle_request("r1")
    verdict = next(iter(platform.verdicts.values())).submission
    assert "proof_concerns" in verdict.review_triggers
    shown = all_text(maker.calls[0]["messages"])
    assert "The platform flagged image proof 0: screenshot." in shown
    assert "made up by the platform" not in shown  # only allowlisted flags reach the prompt
    assert "proof_flags_ignored: p1" in record.guardrail_events


def test_a_duplicate_does_not_count(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", flags=["duplicate"]))
    agent, *_ = _send_30(make_agent)
    record = agent.handle_request("r1")
    _paid_first(platform, record)


def test_upload_binds_hashes_and_flags_exact_duplicates(platform, base_url, add_request):
    add_request("r0", amount=30)
    add_request("r1", amount=30)
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    first = _image("p0").model_dump(mode="json", exclude_none=True)
    httpx.post(f"{base_url}/mock/requests/r0/proofs", json=first, headers=admin).raise_for_status()
    again = _image("p1").model_dump(mode="json", exclude_none=True)  # the same receipt, re-uploaded
    httpx.post(f"{base_url}/mock/requests/r1/proofs", json=again, headers=admin).raise_for_status()
    p0, p1 = platform.proof("p0"), platform.proof("p1")
    assert (p0.request_id, p0.dependent_id, p0.sha256) == ("r0", "d1", hashlib.sha256(PNG).hexdigest())
    assert p0.flags == [] and p1.flags == ["duplicate"] and p1.uploaded_at is not None


# --------------------------------------------------------- reuse, at the engine


def test_the_same_receipt_cannot_back_two_transfers(platform, make_agent, add_request):
    """Re-uploaded under a new id without the platform noticing: the engine still
    refuses, because the content hash was used."""
    add_request("r0", amount=30, proofs=[_ref("p0")])
    platform.add_proof(_image("p0"))
    agent, *_ = _send_30(make_agent)
    assert agent.handle_request("r0").outcome is Outcome.SENT_FULL

    add_request("r1", amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))  # same bytes, no flag
    agent, *_ = _send_30(make_agent)
    record = agent.handle_request("r1")
    assert len(platform.sent) == 1
    assert "engine_blocked: proof already used" in record.guardrail_events


def test_a_new_photo_of_the_same_receipt_cannot_back_two_transfers(platform, make_agent, add_request):
    receipt = findings(30, "Office Mart", receipt_number="A-1029")
    add_request("r0", amount=30, proofs=[_ref("p0")])
    platform.add_proof(_image("p0"))
    agent, *_ = _send_30(make_agent, proof=receipt)
    agent.handle_request("r0")

    add_request("r1", amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", data=PNG + b"cropped"))  # different bytes, same receipt
    agent, *_ = _send_30(make_agent, proof={**receipt, "merchant": "OFFICE MART"})
    record = agent.handle_request("r1")
    assert len(platform.sent) == 1
    assert "engine_blocked: proof already used" in record.guardrail_events


def test_a_blocked_transfer_does_not_use_up_the_proof(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))
    platform.dependents["d1"].frozen = True
    agent, *_ = _send_30(make_agent)
    agent.handle_request("r1")
    assert platform.used_proofs == set()


def _verdict(base_url, key, amount=T(30)):
    body = {"dependent_id": "d1", "request_id": "r1", "idempotency_key": key, "amount": amount,
            "category": "education", "verdict": "agree", "reason": "ok", "concerns": [],
            "independent_assessment": {"stance": "supports", "notes": "ok"}, "review_triggers": [],
            "model": "test"}
    resp = httpx.post(f"{base_url}/agent/v1/auditor/verdicts", json=body,
                      headers={"Authorization": f"Bearer {AUDITOR_TOKEN}"})
    resp.raise_for_status()
    return resp.json()["verdict_id"]


def _direct_transfer(base_url, key, **extra):
    body = {"dependent_id": "d1", "request_id": "r1", "trigger": "request", "amount": T(30), "purpose": "x",
            "category": "education", "category_confidence": 1.0, "reasoning": "trust me",
            "idempotency_key": key, "policy_version": 3, "auditor_verdict_id": _verdict(base_url, key), **extra}
    return httpx.post(f"{base_url}/agent/v1/transfers", json=body,
                      headers={"Authorization": f"Bearer {MAKER_TOKEN}", "Idempotency-Key": key}).json()


def test_engine_pays_first_only_after_review_and_records_the_receipt_owed(platform, base_url, add_request):
    add_request(amount=30)
    unreviewed = _direct_transfer(base_url, "evil-noreview", auditor_verdict_id=None)
    assert (unreviewed["status"], unreviewed["reason"]) == ("blocked", "auditor verdict required")

    resp = _direct_transfer(base_url, "noproof")
    assert resp["status"] == "sent" and resp["proof_owed"]["amount"] == T(30)
    assert len(platform.open_obligations("d1")) == 1


def test_engine_refuses_a_proof_from_another_request(platform, base_url, add_request):
    add_request("r0", amount=30, proofs=[_ref("p0")])
    add_request(amount=30)
    platform.add_proof(_image("p0"))
    resp = _direct_transfer(base_url, "evil-borrowed", proof_ids=["p0"],
                            proof_findings={"merchant": "m", "date": "d", "total": T(30), "matches_claim": True})
    assert resp["reason"] == "proof not for this request"


def test_engine_never_counts_typed_text_and_refuses_findings_that_dont_cover_it(platform, base_url, add_request):
    add_request(amount=30, proofs=[_ref("p1", "text/plain")])
    platform.add_proof(Proof(id="p1", media_type="text/plain", size_bytes=5, text="30.00", source="typed"))
    resp = _direct_transfer(base_url, "evil-typed", proof_ids=["p1"],
                            proof_findings={"merchant": "m", "date": "d", "total": T(30), "matches_claim": True})
    assert resp["status"] == "sent" and resp["proof_owed"] is not None  # typed text settles nothing

    add_request(amount=30)
    platform.add_proof(_image("p2"))
    platform.requests["r1"] = platform.requests["r1"].model_copy(update={"proofs": [_ref("p2")]})
    resp = _direct_transfer(base_url, "evil-low", proof_ids=["p2"],
                            proof_findings={"merchant": "m", "date": "d", "total": T(10), "matches_claim": True})
    assert resp["reason"] == "proof does not cover the transfer"


# ------------------------------------------------- AI detection and camera-only


def test_an_ai_generated_proof_does_not_count_and_forces_review(platform, make_agent, add_request):
    add_request(amount=10, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", flags=["ai_generated"]))
    agent, maker, _ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))], AGREE)
    agent.handle_request("r1")
    verdict = next(iter(platform.verdicts.values())).submission
    assert "proof_concerns" in verdict.review_triggers
    assert "flagged image proof 0: ai_generated" in all_text(maker.calls[0]["messages"])

    add_request("r2", amount=30, proofs=[_ref("p2")])
    platform.add_proof(_image("p2", data=PNG + b"2", flags=["ai_generated"]))
    agent, *_ = _send_30(make_agent)
    record = agent.handle_request("r2")
    _paid_first(platform, record)


@pytest.mark.parametrize("flag", ["ai_suspected", "rephotographed_screen"])
def test_suspicion_flags_force_review_but_still_count(platform, make_agent, add_request, flag):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", flags=[flag]))
    agent, *_ = _send_30(make_agent)
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL
    assert "proof_concerns" in next(iter(platform.verdicts.values())).submission.review_triggers


def test_camera_signed_is_a_note_not_a_concern(platform, make_agent, add_request):
    add_request(amount=10, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", flags=["camera_signed"]))
    agent, maker, _ = make_agent([reply(transfer(10)), reply(finish("sent_full", "Sent!"))])
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL and platform.verdicts == {}  # no review forced
    assert "valid camera signature" in all_text(maker.calls[0]["messages"])


def _camera_only(platform, make_agent, proof=None, auditor=AGREE):
    from agent_layer.config import Limits

    platform.camera_only_proofs = True
    return make_agent(
        [reply(transfer(30, reasoning="Receipt matches a school calculator.", proof=proof or findings(30))),
         reply(finish("sent_full", "Sent!"))],
        auditor, limits=Limits(camera_only_proofs=True),
    )


def test_camera_only_accepts_a_verified_capture(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", source="photo"))
    agent, maker, _ = _camera_only(platform, make_agent)
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL
    assert "taken with the app's camera and verified" in all_text(maker.calls[0]["messages"])


@pytest.mark.parametrize("source", ["file", None])
def test_camera_only_refuses_uploaded_images(platform, make_agent, add_request, source):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", source=source))
    agent, maker, _ = _camera_only(platform, make_agent)
    record = agent.handle_request("r1")
    _paid_first(platform, record)
    assert "proof_not_camera: p1" in record.guardrail_events
    assert "wasn't taken with the app's camera" in all_text(maker.calls[0]["messages"])


@pytest.mark.parametrize("photo,why", [
    (dict(source="file"), "the photo was uploaded, not taken in the app"),
    (dict(source="photo", flags=["duplicate"]), "the photo is a duplicate"),
])
def test_camera_only_ocr_text_doesnt_count_when_its_photo_wouldnt(platform, make_agent, add_request, photo, why):
    add_request(amount=30, proofs=[_ref("txt", "text/plain"), _ref("img")])
    platform.add_proof(Proof(id="txt", media_type="text/plain", size_bytes=20, text="Office Mart 30.00",
                             source="ocr", derived_from="img"))
    platform.add_proof(_image("img", **photo))
    agent, *_ = _camera_only(platform, make_agent)
    record = agent.handle_request("r1")
    _paid_first(platform, record)  # not relied on: {why}
    assert "proof_not_camera: txt" in record.guardrail_events


def test_camera_only_ocr_text_from_a_verified_photo_counts(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("txt", "text/plain"), _ref("img")])
    platform.add_proof(Proof(id="txt", media_type="text/plain", size_bytes=20, text="Office Mart 30.00",
                             source="ocr", derived_from="img"))
    platform.add_proof(_image("img", source="photo"))
    agent, *_ = _camera_only(platform, make_agent)
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL
    assert platform.sent[0].proof_ids == ["txt", "img"]


def test_engine_enforces_camera_only_for_a_compromised_maker(platform, base_url, add_request):
    platform.camera_only_proofs = True
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1", source="file"))
    resp = _direct_transfer(base_url, "evil-file", proof_ids=["p1"],
                            proof_findings={"merchant": "m", "date": "d", "total": T(30), "matches_claim": True})
    assert resp["status"] == "sent" and resp["proof_owed"] is not None  # an upload settles nothing


def test_camera_only_is_read_from_the_environment(monkeypatch):
    from agent_layer.config import Settings

    for k, v in {"PLATFORM_API_URL": "http://p", "PLATFORM_MAKER_TOKEN": "m", "PLATFORM_AUDITOR_TOKEN": "a"}.items():
        monkeypatch.setenv(k, v)
    assert Settings.from_env().limits.camera_only_proofs is False
    monkeypatch.setenv("AGENT_CAMERA_ONLY_PROOFS", "1")
    assert Settings.from_env().limits.camera_only_proofs is True


# ------------------------------------------------------- text-only models (e.g. Nemotron)


def _text_only_agent(maker_api, auditor_api, maker_steps, auditor_steps=()):
    from agent_layer.auditor.auditor import Auditor
    from agent_layer.maker.agent import MakerAgent
    from tests.fakes import ScriptedLLM

    maker = ScriptedLLM(list(maker_steps), supports_images=False)
    auditor = ScriptedLLM(list(auditor_steps), supports_images=False)
    return MakerAgent(maker, maker_api, Auditor(auditor, auditor_api)), maker, auditor


def test_a_text_only_model_never_sees_or_relies_on_an_image_proof(platform, maker_api, auditor_api, add_request):
    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))
    agent, maker, _ = _text_only_agent(maker_api, auditor_api, [
        reply(transfer(30, reasoning="The receipt matches.", proof=findings(30))),  # it would invent findings
        reply(finish("sent_full", "Sent!")),
    ], AGREE)
    record = agent.handle_request("r1")
    first = maker.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in first] == ["text"]  # no image block sent
    assert "you can't see images" in all_text(maker.calls[0]["messages"])
    assert "proof_unseen: p1" in record.guardrail_events
    _paid_first(platform, record)  # the unseen photo backs nothing: the receipt is still owed


def test_a_text_only_model_can_still_use_text_proofs(platform, maker_api, auditor_api, add_request):
    add_request(amount=30, proofs=[_ref("p1", "text/plain")])
    platform.add_proof(Proof(id="p1", media_type="text/plain", size_bytes=20, text="Office Mart 30.00",
                             source="ocr"))
    agent, *_ = _text_only_agent(maker_api, auditor_api,
                                 [reply(transfer(30, proof=findings(30))), reply(finish("sent_full", "Sent!"))],
                                 AGREE)
    assert agent.handle_request("r1").outcome is Outcome.SENT_FULL


def test_a_text_only_auditor_is_told_it_cant_see_the_images(platform, maker_api, auditor_api, add_request):
    """A vision maker and a text-only Auditor: the Auditor gets no image blocks, and is
    told that the case's images exist but are unseen."""
    from agent_layer.auditor.auditor import Auditor
    from agent_layer.maker.agent import MakerAgent
    from tests.fakes import ScriptedLLM

    add_request(amount=30, proofs=[_ref("p1")])
    platform.add_proof(_image("p1"))
    auditor = ScriptedLLM(list(AGREE), supports_images=False)
    maker = ScriptedLLM([reply(transfer(30, proof=findings(30))), reply(finish("sent_full", "Sent!"))])
    record = MakerAgent(maker, maker_api, Auditor(auditor, auditor_api)).handle_request("r1")
    first = auditor.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in first] == ["text"]
    assert "1 image proof(s) were attached, but you can't see images" in first[0]["text"]
    assert "auditor_proof_unseen: 1" in record.guardrail_events


# ---------------------------------------------------------- PDF receipts (online orders)

PDF = b"%PDF-1.7\n% Lazada order 1234567 Office Mart calculator 30.00\n%%EOF\n"


def _pdf(pid: str, *, signed: bool = True, data: bytes = PDF, **kw) -> Proof:
    if not signed:  # the platform withholds an unverified PDF's content
        return Proof(id=pid, media_type="application/pdf", size_bytes=len(data), flags=["unchecked_document"], **kw)
    return Proof(id=pid, media_type="application/pdf", size_bytes=len(data), flags=["issuer_signed"],
                 data_b64=base64.b64encode(data).decode(), **kw)


ORDER = findings(30, "Lazada", receipt_number="1234567")


def test_a_signed_pdf_is_read_and_counts(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(_pdf("p1"))
    agent, maker, auditor = _send_30(make_agent, proof=ORDER)
    record = agent.handle_request("r1")
    assert record.outcome is Outcome.SENT_FULL and platform.sent[0].proof_ids == ["p1"]
    first = maker.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in first] == ["document", "text"]
    assert first[0]["source"] == {"type": "base64", "media_type": "application/pdf",
                                  "data": base64.b64encode(PDF).decode()}
    assert "PDF receipt signed by the shop" in first[1]["text"]
    assert [b["type"] for b in auditor.calls[0]["messages"][0]["content"]] == ["document", "text"]
    assert platform.sent[0].proof_findings.receipt_number == "1234567"


def test_an_unchecked_pdf_is_never_shown_and_never_counts(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(_pdf("p1", signed=False))
    agent, maker, _ = _send_30(make_agent, proof=ORDER)
    record = agent.handle_request("r1")
    _paid_first(platform, record)
    assert [b["type"] for b in maker.calls[0]["messages"][0]["content"]] == ["text"]
    assert "upload the original receipt PDF" in all_text(maker.calls[0]["messages"])
    assert "proof_unchecked_document: p1" in record.guardrail_events


@pytest.mark.parametrize("proof,event", [
    (Proof(id="p1", media_type="application/pdf", size_bytes=len(PDF),
           data_b64=base64.b64encode(PDF).decode()), "PDF without a verified signature"),  # content, no flag
    (_pdf("p1").model_copy(update={"size_bytes": 3}), "size mismatch"),
    (_pdf("p1", sha256="0" * 64), "hash mismatch"),
])
def test_a_pdf_that_doesnt_check_out_is_skipped(platform, make_agent, add_request, proof, event):
    add_request(amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(proof)
    agent, maker, _ = _send_30(make_agent, proof=ORDER)
    record = agent.handle_request("r1")
    _paid_first(platform, record)
    assert any(event in e for e in record.guardrail_events)
    assert [b["type"] for b in maker.calls[0]["messages"][0]["content"]] == ["text"]


def test_a_model_that_cant_read_pdfs_never_relies_on_one(platform, maker_api, auditor_api, add_request):
    from agent_layer.auditor.auditor import Auditor
    from agent_layer.maker.agent import MakerAgent
    from tests.fakes import ScriptedLLM

    add_request(amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(_pdf("p1"))
    maker = ScriptedLLM([reply(transfer(30, proof=ORDER)), reply(finish("sent_full", "Sent!"))],
                        supports_documents=False)
    record = MakerAgent(maker, maker_api, Auditor(ScriptedLLM(list(AGREE)), auditor_api)).handle_request("r1")
    assert [b["type"] for b in maker.calls[0]["messages"][0]["content"]] == ["text"]
    assert "you can't read PDFs" in all_text(maker.calls[0]["messages"])
    assert "proof_unseen: p1" in record.guardrail_events
    _paid_first(platform, record)


def test_an_auditor_that_cant_read_pdfs_is_told(platform, maker_api, auditor_api, add_request):
    from agent_layer.auditor.auditor import Auditor
    from agent_layer.maker.agent import MakerAgent
    from tests.fakes import ScriptedLLM

    add_request(amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(_pdf("p1"))
    auditor = ScriptedLLM(list(AGREE), supports_documents=False)
    maker = ScriptedLLM([reply(transfer(30, proof=ORDER)), reply(finish("sent_full", "Sent!"))])
    record = MakerAgent(maker, maker_api, Auditor(auditor, auditor_api)).handle_request("r1")
    first = auditor.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in first] == ["text"]
    assert "1 PDF receipt(s) were attached, but you can't read PDFs" in first[0]["text"]
    assert "auditor_document_unseen: 1" in record.guardrail_events


def test_a_signed_pdf_counts_in_camera_only_mode(platform, make_agent, add_request):
    add_request(amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(_pdf("p1"))
    agent, *_ = _camera_only(platform, make_agent, proof=ORDER)
    assert agent.handle_request("r1").outcome is Outcome.SENT_FULL  # agent and engine agree


def test_engine_refuses_an_unchecked_pdf(platform, base_url, add_request):
    add_request(amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(_pdf("p1", signed=False))
    resp = _direct_transfer(base_url, "evil-pdf", proof_ids=["p1"],
                            proof_findings={"merchant": "Lazada", "date": "d", "total": T(30), "matches_claim": True})
    assert resp["status"] == "sent" and resp["proof_owed"] is not None  # an unchecked PDF settles nothing


def test_the_same_order_pdf_cannot_back_two_transfers(platform, make_agent, add_request):
    add_request("r0", amount=30, proofs=[_ref("p0", "application/pdf")])
    platform.add_proof(_pdf("p0"))
    agent, *_ = _send_30(make_agent, proof=ORDER)
    agent.handle_request("r0")
    add_request("r1", amount=30, proofs=[_ref("p1", "application/pdf")])
    platform.add_proof(_pdf("p1"))  # same bytes, same order
    agent, *_ = _send_30(make_agent, proof=ORDER)
    record = agent.handle_request("r1")
    assert len(platform.sent) == 1 and "engine_blocked: proof already used" in record.guardrail_events


def test_the_newest_proofs_are_read_when_there_are_many(platform, make_agent, add_request):
    """A request can carry any number of proofs; the latest upload must always be read."""
    add_request(amount=30, proofs=[_ref(f"p{i}") for i in range(4)])
    for i in range(4):
        platform.add_proof(_image(f"p{i}", data=PNG + bytes([i])))
    agent, *_ = _send_30(make_agent)
    record = agent.handle_request("r1")
    fetched = [path for _, method, path in platform.http_log if method == "GET" and "/proofs/" in path]
    assert fetched == ["/agent/v1/proofs/p1", "/agent/v1/proofs/p2", "/agent/v1/proofs/p3"]
    assert "proofs_capped: 4" in record.guardrail_events
