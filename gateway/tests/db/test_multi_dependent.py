"""A guardian with several dependents: one family each, one shared reservoir."""

import asyncio

import httpx
import pytest
from nacl.signing import SigningKey
from sqlalchemy.exc import IntegrityError

from gateway.contracts.agent import ProofFindingsRecord
from gateway.db.models import UsedProofKey
from tests.db import test_app_families_policy as families
from tests.db.test_agent_api import MAKER
from tests.db.test_app_auth import login
from tests.db.test_app_requests import JPEG, new_request, upload
from tests.db.test_schema import _transfer
from tests.db.world import AUSD, World, wallet


class TestSessions:
    async def test_guardian_registers_a_second_dependent(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        response = await families.TestFamilies().register(
            app_client,
            world.guardian_key,
            guardian_token_account=world.family.guardian_token_account,
        )
        assert response.status_code == 200, response.text
        second = response.json()["family"]["id"]

        headers = await login(app_client, world.guardian_key)
        listed = (await app_client.get("/api/v1/families", headers=headers)).json()
        assert [f["id"] for f in listed] == [str(world.family.id), second]
        me = (await app_client.get("/api/v1/me", headers=headers)).json()
        assert (me["family_id"], me["family_ids"]) == (None, [str(world.family.id), second])

    async def test_second_dependent_must_use_the_same_reservoir(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        other = await families.TestFamilies().register(app_client, world.guardian_key)
        assert other.status_code == 409
        same = await families.TestFamilies().register(
            app_client,
            world.guardian_key,
            guardian_token_account=world.family.guardian_token_account,
        )
        assert same.status_code == 200, same.text

    async def test_guardian_with_several_families_must_choose_one(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        sibling = await world.sibling()
        headers = await login(app_client, world.guardian_key)

        unspecified = await app_client.get("/api/v1/family", headers=headers)
        assert unspecified.status_code == 400
        assert unspecified.json()["error"]["code"] == "FAMILY_REQUIRED"

        chosen = await app_client.get(
            "/api/v1/family", headers=headers | {"X-Family-Id": str(sibling.family.id)}
        )
        assert chosen.json()["id"] == str(sibling.family.id)

    async def test_guardian_cannot_pick_another_guardians_family(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        """SEC-10: naming a family you don't guard looks exactly like naming none that exists."""
        stranger = await login(app_client, SigningKey.generate())
        response = await app_client.get(
            "/api/v1/budget", headers=stranger | {"X-Family-Id": str(world.family.id)}
        )
        assert response.status_code == 404

    async def test_dependent_cannot_pick_a_siblings_family(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        sibling = await world.sibling()
        headers = await login(app_client, world.dependent_key)
        response = await app_client.get(
            "/api/v1/history", headers=headers | {"X-Family-Id": str(sibling.family.id)}
        )
        assert response.status_code == 404

    async def test_a_dependent_still_has_only_one_guardian(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        response = await families.TestFamilies().register(
            app_client, SigningKey.generate(), dependent_wallet=wallet(world.dependent_key)
        )
        assert response.status_code == 409


class TestSharedReservoir:
    async def test_a_siblings_pending_money_is_not_available(self, world: World) -> None:
        """One delegation backs all dependents: money cleared for one, not yet settled, cannot
        be cleared again for another."""
        sibling = await world.sibling()
        world.crypto.delegated = 10 * AUSD
        world.crypto.mode = "unavailable"  # the first transfer stays pending

        first = await world.engine.submit(world.instruction(await world.add_request(10 * AUSD)))
        assert first.status == "pending"
        second = await sibling.engine.submit(
            sibling.instruction(await sibling.add_request(10 * AUSD))
        )
        assert (second.status, second.reason) == ("blocked", "insufficient delegation")

    async def test_parallel_requests_from_siblings_cannot_overspend(self, world: World) -> None:
        """SEC-08 across a shared reservoir: room for one transfer, two dependents ask at once."""
        sibling = await world.sibling()
        world.crypto.delegated = 10 * AUSD
        world.crypto.mode = "unavailable"

        a = world.instruction(await world.add_request(10 * AUSD))
        b = sibling.instruction(await sibling.add_request(10 * AUSD))
        results = await asyncio.gather(world.engine.submit(a), sibling.engine.submit(b))
        assert sorted(r.status for r in results) == ["blocked", "pending"]

    async def test_weekly_caps_stay_per_dependent(self, world: World) -> None:
        sibling = await world.sibling()
        for _ in range(5):
            request = await world.add_request(20 * AUSD)
            result = await world.engine.submit(world.instruction(request, amount=20 * AUSD))
            assert result.status == "sent", result
        request = await world.add_request(20 * AUSD)
        result = await world.engine.submit(world.instruction(request, amount=1))
        assert (result.status, result.reason) == ("blocked", "over weekly cap")
        # The first dependent spent their whole cap; the second still has a full week.
        request = await sibling.add_request(20 * AUSD)
        result = await sibling.engine.submit(sibling.instruction(request, amount=20 * AUSD))
        assert result.status == "sent", result


class TestSiblingsCannotShareReceipts:
    """Attack: one dependent passes a paid receipt to a sibling, who claims it again."""

    @staticmethod
    def findings(receipt_number: str) -> ProofFindingsRecord:
        return ProofFindingsRecord(
            merchant="Book Shop",
            date="2026-10-14",
            total=30 * AUSD,
            receipt_number=receipt_number,
            matches_claim=True,
        )

    async def test_same_receipt_file_is_refused_for_a_sibling(self, world: World) -> None:
        sibling = await world.sibling()
        # Different findings, so only the shared file hash can catch the reuse.
        for who, receipt_number, expected in (
            (world, "R-1", "sent"),
            (sibling, "R-2", "proof already used"),
        ):
            request = await who.add_request(30 * AUSD)
            proof = await who.add_proof(request, content=b"the-same-receipt-photo")
            result = await who.engine.submit(
                who.instruction(
                    request,
                    amount=30 * AUSD,
                    proof_ids=[str(proof.id)],
                    proof_findings=self.findings(receipt_number),
                )
            )
            assert (result.reason if result.status == "blocked" else result.status) == expected

    async def test_upload_of_a_siblings_receipt_is_flagged(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        sibling = await world.sibling()
        first = await login(app_client, world.dependent_key)
        second = await login(app_client, sibling.dependent_key)
        request_a = (await new_request(app_client, first))["request"]["id"]
        request_b = (await new_request(app_client, second))["request"]["id"]
        await upload(app_client, first, request_a, JPEG)
        again = await upload(app_client, second, request_b, JPEG)
        assert again.json()["proof"]["flags"] == ["duplicate"]

    async def test_database_refuses_a_key_used_by_a_sibling(self, world: World) -> None:
        """Even past the engine's check, one receipt key backs one transfer per guardian."""
        sibling = await world.sibling()
        async with world.sessionmaker() as session:
            transfers = [_transfer(w.family, f"req:{w.family.id}:1") for w in (world, sibling)]
            session.add_all(transfers)
            await session.flush()
            session.add_all(
                UsedProofKey(
                    guardian_user_id=w.family.guardian_user_id,
                    key="sha256:abc",
                    family_id=w.family.id,
                    transfer_id=t.id,
                )
                for w, t in zip((world, sibling), transfers, strict=True)
            )
            with pytest.raises(IntegrityError, match="pk_used_proof_keys"):
                await session.commit()


class TestPlatformApi:
    async def test_usage_is_shared_by_the_guardians_dependents(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        await world.sibling()
        guardian = str(world.family.guardian_user_id)
        report = {"family_id": guardian, "input_tokens": 300, "output_tokens": 200}
        await app_client.post("/agent/v1/usage", headers=MAKER, json=report)
        await app_client.post("/agent/v1/usage", headers=MAKER, json=report)
        status = await app_client.get(f"/agent/v1/usage/{guardian}", headers=MAKER)
        assert status.json() == {"remaining_tokens": 200_000 - 1_000}

    async def test_guardian_notification_reaches_them_without_choosing_a_family(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        await world.sibling()
        note = {
            "audience": "guardian",
            "recipient_id": str(world.family.guardian_user_id),
            "message": "The shared reservoir runs out in about 2 weeks.",
            "dedupe_key": None,
        }
        sent = await app_client.post("/agent/v1/notifications", headers=MAKER, json=note)
        assert sent.status_code == 200, sent.text

        headers = await login(app_client, world.guardian_key)
        listed = (await app_client.get("/api/v1/notifications", headers=headers)).json()
        assert [(n["id"], n["family_id"]) for n in listed] == [(sent.json()["id"], None)]

    async def test_context_counts_the_whole_reservoir(
        self, app_client: httpx.AsyncClient, world: World
    ) -> None:
        sibling = await world.sibling()
        world.crypto.mode = "unavailable"
        await world.engine.submit(world.instruction(await world.add_request(10 * AUSD)))
        context = await app_client.get(
            f"/agent/v1/dependents/{sibling.dependent_id}/context", headers=MAKER
        )
        assert context.json()["delegated_amount"] == 500 * AUSD - 8 * AUSD
