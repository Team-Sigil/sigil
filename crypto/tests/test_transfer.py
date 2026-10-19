"""POST /v1/transfer: the signer's own checks (SEC-03), idempotency and never signing twice
(SEC-07), and the transaction it sends (SEC-15)."""

import asyncio
import uuid

from solders.hash import Hash
from spl.token.constants import TOKEN_PROGRAM_ID

from signer.chain import build_transfer
from signer.config import AUSD
from signer.contracts import CryptoTransferRequest
from signer.keys import AgentWallet
from signer.store import DayCaps, SignerStore
from signer.transfers import LEASE_SECONDS, request_hash
from tests.conftest import SignerClient, World, pubkey, serve


def balance(world: World, account: str) -> int:
    return world.chain.token_accounts[account].amount


async def test_cleared_transfer_is_sent_and_confirmed(client: SignerClient, world: World) -> None:
    response = await client.transfer(world.transfer_body(amount_units=8 * AUSD))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["error_code"] is None
    assert body["tx_signature"] == str(world.chain.landed[0].signatures[0])
    assert balance(world, world.dependent_account) == 8 * AUSD
    assert balance(world, world.reservoir) == 992 * AUSD
    assert world.chain.token_accounts[world.reservoir].delegated_amount == 492 * AUSD


async def test_transaction_is_one_transfer_checked_and_no_memo(
    client: SignerClient, world: World
) -> None:
    """SEC-15: nothing but the transfer goes on chain; the agent wallet is payer and delegate."""
    await client.transfer(world.transfer_body(amount_units=5 * AUSD))
    (tx,) = world.chain.landed
    message = tx.message
    keys = [str(k) for k in message.account_keys]
    (ix,) = message.instructions
    assert keys[ix.program_id_index] == str(TOKEN_PROGRAM_ID)
    data = bytes(ix.data)
    assert data[0] == 12  # TransferChecked
    assert int.from_bytes(data[1:9], "little") == 5 * AUSD
    assert data[9] == 6
    assert [keys[i] for i in ix.accounts] == [
        world.reservoir,
        world.mint,
        world.dependent_account,
        str(world.agent.pubkey()),
    ]
    assert keys[0] == str(world.agent.pubkey())  # fee payer
    assert len(tx.signatures) == 1


# ---- SEC-03: registered destination and source, mint, caps


async def test_destination_not_the_familys_is_refused(client: SignerClient, world: World) -> None:
    attacker = pubkey()
    response = await client.transfer(world.transfer_body(destination_token_account=attacker))
    assert response.status_code == 403
    assert response.json() == {"error_code": "SIGNER_REJECTED"}
    assert world.chain.send_calls == 0


async def test_other_familys_destination_is_refused(client: SignerClient, world: World) -> None:
    _, sibling_account = world.add_family()
    response = await client.transfer(world.transfer_body(destination_token_account=sibling_account))
    assert response.status_code == 403
    assert world.chain.send_calls == 0


async def test_another_guardians_reservoir_is_refused(client: SignerClient, world: World) -> None:
    """Every reservoir delegates to the same agent wallet, so a compromised gateway could
    otherwise pay this family from another guardian's money."""
    other_reservoir = pubkey()
    world.chain.token_accounts[other_reservoir] = type(world.chain.token_accounts[world.reservoir])(
        owner=pubkey(),
        mint=world.mint,
        amount=1_000 * AUSD,
        delegate=str(world.agent.pubkey()),
        delegated_amount=500 * AUSD,
    )
    response = await client.transfer(world.transfer_body(source_token_account=other_reservoir))
    assert response.status_code == 403
    assert response.json() == {"error_code": "SIGNER_REJECTED"}
    assert world.chain.send_calls == 0
    assert balance(world, other_reservoir) == 1_000 * AUSD


async def test_siblings_share_one_reservoir(client: SignerClient, world: World) -> None:
    sibling, sibling_account = world.add_family()
    response = await client.transfer(
        world.transfer_body(family_id=str(sibling), destination_token_account=sibling_account)
    )
    assert response.json()["status"] == "confirmed"
    assert balance(world, sibling_account) == 8 * AUSD


async def test_unregistered_family_is_refused(client: SignerClient, world: World) -> None:
    response = await client.transfer(world.transfer_body(family_id=str(uuid.uuid4())))
    assert response.status_code == 403
    assert world.chain.send_calls == 0


async def test_wrong_mint_is_refused(client: SignerClient, world: World) -> None:
    response = await client.transfer(world.transfer_body(mint=pubkey()))
    assert response.status_code == 403
    assert response.json() == {"error_code": "SIGNER_REJECTED"}


async def test_over_per_transfer_cap_is_refused(client: SignerClient, world: World) -> None:
    response = await client.transfer(world.transfer_body(amount_units=200 * AUSD + 1))
    assert response.status_code == 403
    assert response.json() == {"error_code": "OVER_SIGNER_CAP"}
    assert world.chain.send_calls == 0


async def test_exactly_per_transfer_cap_is_allowed(client: SignerClient, world: World) -> None:
    response = await client.transfer(world.transfer_body(amount_units=200 * AUSD))
    assert response.json()["status"] == "confirmed"


async def test_over_per_day_cap_is_refused(client: SignerClient, world: World) -> None:
    assert (await client.transfer(world.transfer_body(amount_units=200 * AUSD))).status_code == 200
    assert (await client.transfer(world.transfer_body(amount_units=100 * AUSD))).status_code == 200
    response = await client.transfer(world.transfer_body(amount_units=1))
    assert response.status_code == 403
    assert response.json() == {"error_code": "OVER_SIGNER_CAP"}
    assert len(world.chain.landed) == 2


async def test_day_cap_resets_next_utc_day(client: SignerClient, world: World) -> None:
    await client.transfer(world.transfer_body(amount_units=200 * AUSD))
    await client.transfer(world.transfer_body(amount_units=100 * AUSD))
    world.clock.now += 86_400
    response = await client.transfer(world.transfer_body(amount_units=1 * AUSD))
    assert response.json()["status"] == "confirmed"


async def test_failed_transfers_dont_use_up_the_day(client: SignerClient, world: World) -> None:
    account = world.chain.token_accounts[world.reservoir]
    account.delegate, account.delegated_amount = None, 0
    for _ in range(3):
        failed = await client.transfer(world.transfer_body(amount_units=150 * AUSD))
        assert failed.json()["status"] == "failed"
    account.delegate, account.delegated_amount = str(world.agent.pubkey()), 500 * AUSD
    response = await client.transfer(world.transfer_body(amount_units=150 * AUSD))
    assert response.json()["status"] == "confirmed"


async def test_per_source_day_cap(world: World) -> None:
    async for client in serve(
        world.app(signer_max_per_source_per_day_units=50 * AUSD), world.clock
    ):
        assert (
            await client.transfer(world.transfer_body(amount_units=50 * AUSD))
        ).status_code == 200
        response = await client.transfer(world.transfer_body(amount_units=1))
        assert response.status_code == 403
        assert response.json() == {"error_code": "OVER_SIGNER_CAP"}


async def test_extra_fields_are_rejected(client: SignerClient, world: World) -> None:
    body = world.transfer_body()
    body["memo"] = "hello"
    response = await client.transfer(body)
    assert response.status_code == 422
    assert world.chain.send_calls == 0


async def test_float_amount_is_rejected(client: SignerClient, world: World) -> None:
    response = await client.transfer(world.transfer_body(amount_units=8.5))
    assert response.status_code == 422


# ---- SEC-03: on-chain delegation


async def test_revoked_delegation_fails(client: SignerClient, world: World) -> None:
    """Attack demo 4: the guardian revokes mid-demo; the next transfer fails."""
    account = world.chain.token_accounts[world.reservoir]
    account.delegate, account.delegated_amount = None, 0
    response = await client.transfer(world.transfer_body())
    assert response.status_code == 200
    assert response.json() == {
        "status": "failed",
        "tx_signature": None,
        "error_code": "INSUFFICIENT_DELEGATION",
    }
    assert world.chain.send_calls == 0


async def test_too_small_delegation_fails(client: SignerClient, world: World) -> None:
    world.chain.token_accounts[world.reservoir].delegated_amount = 7 * AUSD
    response = await client.transfer(world.transfer_body(amount_units=8 * AUSD))
    assert response.json()["error_code"] == "INSUFFICIENT_DELEGATION"
    assert world.chain.send_calls == 0


async def test_delegated_to_someone_else_fails(client: SignerClient, world: World) -> None:
    world.chain.token_accounts[world.reservoir].delegate = pubkey()
    response = await client.transfer(world.transfer_body())
    assert response.json()["error_code"] == "INSUFFICIENT_DELEGATION"


async def test_preflight_rejection_is_a_definite_failure(
    client: SignerClient, world: World
) -> None:
    world.chain.token_accounts[world.reservoir].amount = 1 * AUSD  # delegated, but not funded
    response = await client.transfer(world.transfer_body(amount_units=8 * AUSD))
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "INSUFFICIENT_FUNDS"
    assert world.chain.landed == []


async def test_landed_with_error_is_a_definite_failure(client: SignerClient, world: World) -> None:
    world.chain.land_failing = True
    world.chain.token_accounts[world.reservoir].amount = 1 * AUSD
    response = await client.transfer(world.transfer_body(amount_units=8 * AUSD))
    assert response.json()["status"] == "failed"
    assert response.json()["tx_signature"] is None


# ---- SEC-07: idempotency and never signing twice


async def test_same_key_twice_returns_same_result_one_transaction(
    client: SignerClient, world: World
) -> None:
    body = world.transfer_body()
    first = await client.transfer(body)
    second = await client.transfer(body)
    assert first.json() == second.json()
    assert first.json()["status"] == "confirmed"
    assert len(world.chain.landed) == 1
    assert world.chain.send_calls == 1


async def test_retry_after_day_cap_is_full_still_confirms(
    client: SignerClient, world: World
) -> None:
    sent = world.transfer_body(amount_units=200 * AUSD)
    first = await client.transfer(sent)
    await client.transfer(world.transfer_body(amount_units=100 * AUSD))  # day is now full
    retry = await client.transfer(sent)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert len(world.chain.landed) == 2


async def test_retry_after_revoke_still_confirms(client: SignerClient, world: World) -> None:
    sent = world.transfer_body()
    first = await client.transfer(sent)
    account = world.chain.token_accounts[world.reservoir]
    account.delegate, account.delegated_amount = None, 0
    assert (await client.transfer(sent)).json() == first.json()


async def test_retry_after_registration_changed_still_confirms(
    client: SignerClient, world: World
) -> None:
    sent = world.transfer_body()
    first = await client.transfer(sent)
    world.registry.families.clear()
    assert (await client.transfer(sent)).json() == first.json()


async def test_failed_result_is_final(client: SignerClient, world: World) -> None:
    """The gateway released the reservation on `failed`; the key must never move money later."""
    account = world.chain.token_accounts[world.reservoir]
    account.delegate, account.delegated_amount = None, 0
    body = world.transfer_body()
    assert (await client.transfer(body)).json()["status"] == "failed"
    account.delegate, account.delegated_amount = str(world.agent.pubkey()), 500 * AUSD
    assert (await client.transfer(body)).json()["status"] == "failed"
    assert world.chain.send_calls == 0


async def test_concurrent_same_key_signs_once(client: SignerClient, world: World) -> None:
    body = world.transfer_body()
    responses = await asyncio.gather(*(client.transfer(body) for _ in range(5)))
    codes = sorted(r.status_code for r in responses)
    assert len(world.chain.landed) == 1
    assert 200 in codes
    assert set(codes) <= {200, 503}
    for r in responses:
        if r.status_code == 503:
            assert r.json() == {"error_code": "IN_PROGRESS"}


async def test_same_key_with_different_body_is_not_a_refusal(
    client: SignerClient, world: World
) -> None:
    world.chain.drop_sends = True  # first attempt stays in flight
    body = world.transfer_body()
    assert (await client.transfer(body)).status_code == 503
    changed = dict(body, amount_units=9 * AUSD)
    response = await client.transfer(changed)
    assert response.status_code == 503
    assert response.json() == {"error_code": "IDEMPOTENCY_MISMATCH"}


async def test_lost_response_after_send_then_retry_sends_nothing_new(
    client: SignerClient, world: World
) -> None:
    """The transaction landed but the RPC connection dropped: we don't know, so 503; the retry
    finds the stored signature on chain."""
    world.chain.lose_send_responses = True
    body = world.transfer_body()
    first = await client.transfer(body)
    assert first.status_code == 503
    assert len(world.chain.landed) == 1

    world.chain.lose_send_responses = False
    retry = await client.transfer(body)
    assert retry.json()["status"] == "confirmed"
    assert retry.json()["tx_signature"] == str(world.chain.landed[0].signatures[0])
    assert len(world.chain.landed) == 1
    assert balance(world, world.dependent_account) == 8 * AUSD


async def test_not_yet_confirmed_is_503_then_confirmed(client: SignerClient, world: World) -> None:
    world.chain.land_as = "processed"
    body = world.transfer_body()
    first = await client.transfer(body)
    assert first.status_code == 503
    assert first.json() == {"error_code": "NOT_CONFIRMED_YET"}
    signature = str(world.chain.landed[0].signatures[0])
    world.chain.statuses[signature]["confirmationStatus"] = "confirmed"
    retry = await client.transfer(body)
    assert retry.json() == {"status": "confirmed", "tx_signature": signature, "error_code": None}
    assert len(world.chain.landed) == 1


def crashed_after_signing(world: World, body: dict[str, object]) -> tuple[str, int]:
    """Leave the store as a process that died right after storing the signature and before
    sending: the row is `sent`, the transaction never reached the chain."""
    store = SignerStore(world.db_path)
    req = CryptoTransferRequest.model_validate(body)
    caps = DayCaps(
        day="2026-10-19", max_per_day_units=300 * AUSD, max_per_source_per_day_units=None
    )
    now = world.clock.now
    claim = store.claim(req, request_hash(req), caps, "dead-worker", now + LEASE_SECONDS, now)
    assert claim.record is not None
    last_valid = world.chain.finalized_height + 150
    tx = build_transfer(
        AgentWallet(world.agent),
        req.source_token_account,
        req.mint,
        req.destination_token_account,
        req.amount_units,
        Hash.new_unique(),
    )
    assert store.mark_sent(
        req.idempotency_key, "dead-worker", str(tx.signatures[0]), last_valid, bytes(tx), now
    )
    store.close()
    return str(tx.signatures[0]), last_valid


async def test_crash_before_send_retry_rebroadcasts_same_signature(world: World) -> None:
    body = world.transfer_body()
    signature, _ = crashed_after_signing(world, body)
    async for client in serve(world.app(), world.clock):
        busy = await client.transfer(body)  # the dead worker's lease hasn't expired yet
        assert busy.status_code == 503
        assert busy.json() == {"error_code": "IN_PROGRESS"}

        world.clock.now += LEASE_SECONDS + 1
        retry = await client.transfer(body)
        # Blockhash still valid: we rebroadcast the stored bytes, never a new transaction.
        assert retry.status_code == 503
        assert retry.json() == {"error_code": "IN_FLIGHT"}
        assert [str(tx.signatures[0]) for tx in world.chain.landed] == [signature]

        final = await client.transfer(body)
        assert final.json()["status"] == "confirmed"
        assert final.json()["tx_signature"] == signature
        assert len(world.chain.landed) == 1


async def test_crash_before_send_while_chain_still_dropping_stays_unknown(world: World) -> None:
    body = world.transfer_body()
    crashed_after_signing(world, body)
    world.chain.drop_sends = True
    world.clock.now += LEASE_SECONDS + 1
    async for client in serve(world.app(), world.clock):
        for _ in range(3):
            response = await client.transfer(body)
            assert response.status_code == 503
        assert world.chain.landed == []


async def test_expired_blockhash_signs_a_new_transaction_once(world: World) -> None:
    body = world.transfer_body()
    old_signature, last_valid = crashed_after_signing(world, body)
    world.clock.now += LEASE_SECONDS + 1
    world.chain.finalized_height = last_valid + 1  # the old transaction can never land
    async for client in serve(world.app(), world.clock):
        response = await client.transfer(body)
        assert response.json()["status"] == "confirmed"
        assert response.json()["tx_signature"] != old_signature
        again = await client.transfer(body)
        assert again.json() == response.json()
    assert len(world.chain.landed) == 1
    assert balance(world, world.dependent_account) == 8 * AUSD


async def test_expired_but_revoked_meanwhile_fails(world: World) -> None:
    body = world.transfer_body()
    _, last_valid = crashed_after_signing(world, body)
    world.clock.now += LEASE_SECONDS + 1
    world.chain.finalized_height = last_valid + 1
    account = world.chain.token_accounts[world.reservoir]
    account.delegate, account.delegated_amount = None, 0
    async for client in serve(world.app(), world.clock):
        response = await client.transfer(body)
        assert response.json()["error_code"] == "INSUFFICIENT_DELEGATION"


async def test_ledger_survives_restart(world: World) -> None:
    body = world.transfer_body()
    async for client in serve(world.app(), world.clock):
        first = await client.transfer(body)
    async for client in serve(world.app(), world.clock):
        assert (await client.transfer(body)).json() == first.json()
    assert len(world.chain.landed) == 1


async def test_chain_down_before_signing_is_503(client: SignerClient, world: World) -> None:
    world.chain.down = True
    body = world.transfer_body()
    response = await client.transfer(body)
    assert response.status_code == 503
    world.chain.down = False
    assert (await client.transfer(body)).json()["status"] == "confirmed"
    assert len(world.chain.landed) == 1


# ---- MOCK_CHAIN


async def test_mock_chain_skips_chain_but_keeps_checks(world: World) -> None:
    async for client in serve(world.app(mock_chain=True), world.clock):
        body = world.transfer_body()
        response = await client.transfer(body)
        assert response.json()["status"] == "confirmed"
        assert response.json()["tx_signature"].startswith("mock_")
        assert (await client.transfer(body)).json() == response.json()

        refused = await client.transfer(world.transfer_body(destination_token_account=pubkey()))
        assert refused.status_code == 403
    assert world.chain.send_calls == 0
