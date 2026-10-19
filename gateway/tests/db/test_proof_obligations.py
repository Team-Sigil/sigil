"""Proof after payment (SPEC §8.9).

A transfer at or above the proof threshold goes out first, because a dependent cannot buy
anything before being paid, and leaves a receipt owed. While that receipt is outstanding the
dependent gets no more money, and the single-use rules still mean one receipt backs one
transfer.
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from gateway.agent_api import service
from gateway.agent_api.service import SettledObligation, SettleObligation
from gateway.contracts.agent import ProofFindingsRecord
from gateway.db.models import ProofObligation, ReceiptFingerprint, Transfer
from gateway.engine.proofs import keyed_fingerprint
from gateway.enums import ObligationStatus
from tests.db.test_agent_api import MAKER
from tests.db.test_app_auth import login
from tests.db.test_app_requests import JPEG, new_request, upload
from tests.db.world import AUSD, World
from tests.images import jpeg

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

FINDINGS = ProofFindingsRecord(
    merchant="Book Shop", date="2026-10-19", total=30 * AUSD, matches_claim=True
)
KEY = b"test-fingerprint-key"


async def settle_directly(
    world: World, obligation: ProofObligation, proof_id: uuid.UUID, *, camera_only: bool = False
) -> SettledObligation:
    async with world.sessionmaker() as session:
        return await service.settle_obligation(
            session,
            str(obligation.id),
            SettleObligation(proof_id=str(proof_id), findings=FINDINGS),
            camera_only=camera_only,
            fingerprint_key=KEY,
        )


async def open_obligation(world: World) -> ProofObligation:
    async with world.sessionmaker() as session:
        found = await session.scalar(
            select(ProofObligation).where(ProofObligation.status == ObligationStatus.OPEN)
        )
        assert found is not None
        return found


class TestOwingAReceipt:
    async def test_transfer_at_the_threshold_is_sent_and_owes_a_receipt(self, world: World) -> None:
        request = await world.add_request(30 * AUSD)
        result = await world.engine.submit(world.instruction(request, amount=30 * AUSD))

        assert result.status == "sent"
        obligation = await open_obligation(world)
        assert obligation.amount == 30 * AUSD
        assert obligation.request_id == request.id

    async def test_below_the_threshold_owes_nothing(self, world: World) -> None:
        request = await world.add_request(10 * AUSD)
        assert (await world.engine.submit(world.instruction(request))).status == "sent"
        async with world.sessionmaker() as session:
            assert await session.scalar(select(ProofObligation)) is None

    async def test_a_failed_transfer_owes_nothing(self, world: World) -> None:
        """Nothing was paid, so there is nothing to show a receipt for."""
        world.crypto.mode = "fail"
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        async with world.sessionmaker() as session:
            assert await session.scalar(select(ProofObligation)) is None

    async def test_an_outstanding_receipt_blocks_the_next_transfer(self, world: World) -> None:
        first = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(first, amount=30 * AUSD))

        second = await world.add_request(10 * AUSD)
        result = await world.engine.submit(world.instruction(second))
        assert (result.status, result.reason) == ("blocked", "proof outstanding")

    async def test_a_pending_transfer_that_owes_a_receipt_blocks_the_next(
        self, world: World
    ) -> None:
        """The obligation row only appears once the money lands, but money still in flight
        must hold back the next request too."""
        world.crypto.mode = "unavailable"
        first = await world.add_request(30 * AUSD)
        result = await world.engine.submit(world.instruction(first, amount=30 * AUSD))
        assert result.status != "sent"
        async with world.sessionmaker() as session:
            assert await session.scalar(select(ProofObligation)) is None

        world.crypto.mode = "confirm"
        second = await world.add_request(10 * AUSD)
        result = await world.engine.submit(world.instruction(second))
        assert (result.status, result.reason) == ("blocked", "proof outstanding")

    async def test_the_allowance_still_runs(self, world: World) -> None:
        """The guardian's standing instruction is not held back by a late receipt."""
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))

        allowance = world.instruction(
            None,
            trigger="allowance",
            amount=30 * AUSD,
            category="allowance",
            idempotency_key=f"allowance:{world.dependent_id}:2026-W42",
        )
        assert (await world.engine.submit(allowance)).status == "sent"


class TestSettling:
    async def settle(
        self, client: httpx.AsyncClient, obligation: ProofObligation, proof_id: str, **over: Any
    ) -> httpx.Response:
        body = {"proof_id": proof_id, "findings": FINDINGS.model_dump(mode="json")} | over
        return await client.post(
            f"/agent/v1/proof-obligations/{obligation.id}/settle", headers=MAKER, json=body
        )

    async def test_a_receipt_settles_it_and_unblocks_the_dependent(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        obligation = await open_obligation(world)

        blocked = await app_client.post(
            "/api/v1/requests", headers=headers, json={"message": "more please"}
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "PROOF_OUTSTANDING"

        uploaded = await upload(app_client, headers, str(request.id), JPEG)
        proof_id = uploaded.json()["proof"]["id"]
        response = await self.settle(app_client, obligation, proof_id)
        assert response.json() == {"settled": True, "reason": None}

        assert (await new_request(app_client, headers))["request"]["id"]

    async def test_a_receipt_that_does_not_cover_the_amount_is_refused(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        obligation = await open_obligation(world)

        uploaded = await upload(app_client, headers, str(request.id), JPEG)
        short = FINDINGS.model_copy(update={"total": 5 * AUSD}).model_dump(mode="json")
        response = await self.settle(
            app_client, obligation, uploaded.json()["proof"]["id"], findings=short
        )
        assert response.json()["settled"] is False
        assert (await open_obligation(world)).id == obligation.id

    async def test_a_receipt_cannot_settle_twice(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """One receipt backs one payment: a new photo of the same receipt, uploaded for the
        next request, is refused on its findings."""
        headers = await login(app_client, world.dependent_key)
        first = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(first, amount=30 * AUSD))
        obligation = await open_obligation(world)
        uploaded = await upload(app_client, headers, str(first.id), JPEG)
        settled = await self.settle(app_client, obligation, uploaded.json()["proof"]["id"])
        assert settled.json() == {"settled": True, "reason": None}

        second = await world.add_request(30 * AUSD)
        await world.engine.submit(
            world.instruction(second, amount=30 * AUSD, idempotency_key=f"req:{second.id}:1")
        )
        again = await open_obligation(world)
        retaken = await upload(app_client, headers, str(second.id), jpeg(7))
        response = await self.settle(app_client, again, retaken.json()["proof"]["id"])
        assert response.json() == {"settled": False, "reason": "proof already used"}

    async def test_a_receipt_recorded_in_any_family_cannot_settle(self, world: World) -> None:
        """The keyed fingerprint is shared by all families, as in the engine."""
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        obligation = await open_obligation(world)
        proof = await world.add_proof(request, source="photo")
        async with world.sessionmaker() as session:
            session.add(
                ReceiptFingerprint(
                    key_hash=keyed_fingerprint(FINDINGS, KEY),
                    family_id=world.family.id,
                    transfer_id=obligation.transfer_id,
                )
            )
            await session.commit()

        result = await settle_directly(world, obligation, proof.id)
        assert result == SettledObligation(settled=False, reason="proof already used")

    async def test_camera_only_applies_to_settling(self, world: World) -> None:
        """With CAMERA_ONLY_PROOFS, a plain file settles nothing, as the engine would rule."""
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        obligation = await open_obligation(world)
        upload_ = await world.add_proof(request, b"from the gallery", source="file")
        photo = await world.add_proof(request, b"taken in the app", source="photo")

        refused = await settle_directly(world, obligation, upload_.id, camera_only=True)
        assert refused == SettledObligation(settled=False, reason="not usable as proof")
        accepted = await settle_directly(world, obligation, photo.id, camera_only=True)
        assert accepted == SettledObligation(settled=True, reason=None)

    async def test_the_receipt_can_be_sent_when_the_request_already_has_3_proofs(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """Proofs from before the money went out must not lock the dependent out of
        sending the receipt, and the agent reads the ones sent for it."""
        headers = await login(app_client, world.dependent_key)
        request = await world.add_request(30 * AUSD)
        for seed in (11, 12, 13):
            assert (
                await upload(app_client, headers, str(request.id), jpeg(seed))
            ).status_code == 200
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        await open_obligation(world)

        receipt = await upload(app_client, headers, str(request.id), jpeg(14))
        assert receipt.status_code == 200, receipt.text
        seen = await app_client.get(f"/agent/v1/requests/{request.id}", headers=MAKER)
        assert [p["id"] for p in seen.json()["proofs"]] == [receipt.json()["proof"]["id"]]


class TestOnlineOrders:
    async def test_a_pdf_receipt_is_accepted(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """An online order cannot be photographed, so its PDF is stored. Whether the
        document is genuine is not checked yet, which the flag records."""
        headers = await login(app_client, world.dependent_key)
        request = await world.add_request(30 * AUSD)
        response = await upload(
            app_client, headers, str(request.id), PDF, content_type="application/pdf"
        )
        assert response.status_code == 200, response.text
        proof = response.json()["proof"]
        assert proof["media_type"] == "application/pdf"
        assert "unchecked_document" in proof["flags"]

    @pytest.mark.parametrize("data", [b"not a pdf at all", b"%PDF"])
    async def test_something_that_is_not_a_pdf_is_refused(
        self, app_client: httpx.AsyncClient, world: World, data: bytes
    ) -> None:
        """Typed from the bytes, never the Content-Type."""
        headers = await login(app_client, world.dependent_key)
        request = await world.add_request(30 * AUSD)
        response = await upload(
            app_client, headers, str(request.id), data, content_type="application/pdf"
        )
        assert response.status_code == 422

    async def test_an_unsigned_pdf_does_not_settle_the_debt(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """It is stored, but nothing ties it to the shop, so it is not evidence."""
        headers = await login(app_client, world.dependent_key)
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        obligation = await open_obligation(world)

        uploaded = await upload(
            app_client, headers, str(request.id), PDF, content_type="application/pdf"
        )
        response = await TestSettling().settle(
            app_client, obligation, uploaded.json()["proof"]["id"]
        )
        assert response.json() == {"settled": False, "reason": "not usable as proof"}

    async def test_a_signed_pdf_settles_an_obligation(
        self, app_client: httpx.AsyncClient, world: World, signed_receipt: bytes
    ) -> None:
        headers = await login(app_client, world.dependent_key)
        request = await world.add_request(30 * AUSD)
        await world.engine.submit(world.instruction(request, amount=30 * AUSD))
        obligation = await open_obligation(world)

        uploaded = await upload(
            app_client, headers, str(request.id), signed_receipt, content_type="application/pdf"
        )
        assert "issuer_signed" in uploaded.json()["proof"]["flags"]
        response = await TestSettling().settle(
            app_client, obligation, uploaded.json()["proof"]["id"]
        )
        assert response.json() == {"settled": True, "reason": None}


async def test_the_app_can_see_what_is_owed(app_client: httpx.AsyncClient, world: World) -> None:
    headers = await login(app_client, world.dependent_key)
    request = await world.add_request(30 * AUSD)
    await world.engine.submit(world.instruction(request, amount=30 * AUSD))

    response = await app_client.get("/api/v1/proof-obligations", headers=headers)
    assert response.status_code == 200
    owed = response.json()
    assert len(owed) == 1 and owed[0]["amount"] == 30 * AUSD

    async with world.sessionmaker() as session:
        transfer = await session.scalar(select(Transfer))
        assert transfer is not None and transfer.proof_owed is True
