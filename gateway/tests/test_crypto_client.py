import time
import uuid

import httpx
import pytest

from gateway.contracts.crypto import (
    CryptoTransferRequest,
    CryptoTransferStatus,
    CryptoUnavailableError,
    Delegation,
)
from gateway.crypto_client import HttpCryptoClient, sign
from mock_crypto.app import MockCrypto, create_mock_app

SECRET = "test-crypto-hmac-secret"
SOURCE = "So11111111111111111111111111111111111111112"
DEST = "Dest1111111111111111111111111111111111111111"
AGENT = "Agent111111111111111111111111111111111111111"
AUSD = 1_000_000


def client_for(mock: MockCrypto, secret: str = SECRET) -> HttpCryptoClient:
    return HttpCryptoClient(
        "http://crypto", secret, transport=httpx.ASGITransport(app=create_mock_app(mock))
    )


def transfer(key: str = "req:r1:1", amount: int = 8 * AUSD) -> CryptoTransferRequest:
    return CryptoTransferRequest(
        transfer_id=uuid.uuid4(),
        family_id=uuid.uuid4(),
        amount_units=amount,
        source_token_account=SOURCE,
        destination_token_account=DEST,
        mint=SOURCE,
        idempotency_key=key,
    )


@pytest.fixture
def mock() -> MockCrypto:
    m = MockCrypto(secret=SECRET)
    m.delegations[SOURCE] = Delegation(
        token_account=SOURCE, owner="Guardian1111", delegate=AGENT, delegated_amount_units=50 * AUSD
    )
    return m


async def test_reads_the_delegation(mock: MockCrypto) -> None:
    delegation = await client_for(mock).get_delegation(SOURCE)
    assert (delegation.owner, delegation.delegate) == ("Guardian1111", AGENT)
    assert delegation.delegated_amount_units == 50 * AUSD


async def test_transfer_is_confirmed_once_per_key(mock: MockCrypto) -> None:
    client = client_for(mock)
    first = await client.transfer(transfer())
    again = await client.transfer(transfer())
    assert first.status == CryptoTransferStatus.CONFIRMED and first.tx_signature
    assert again.tx_signature == first.tx_signature
    assert len(mock.sent) == 1
    assert mock.delegations[SOURCE].delegated_amount_units == 42 * AUSD


async def test_insufficient_delegation_fails(mock: MockCrypto) -> None:
    result = await client_for(mock).transfer(transfer(amount=51 * AUSD))
    assert (result.status, result.error_code) == ("failed", "INSUFFICIENT_DELEGATION")


async def test_wrong_secret_is_rejected(mock: MockCrypto) -> None:
    client = client_for(mock, secret="wrong")
    result = await client.transfer(transfer())
    assert (result.status, result.error_code) == ("failed", "SIGNER_REJECTED")
    with pytest.raises(CryptoUnavailableError):
        await client.get_delegation(SOURCE)
    assert mock.sent == []


async def test_replayed_and_stale_requests_are_rejected(mock: MockCrypto) -> None:
    app = create_mock_app(mock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
        body = transfer().model_dump_json().encode()
        ts, nonce = str(int(time.time())), str(uuid.uuid4())
        headers = {
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(SECRET, ts, nonce, body),
        }
        assert (await c.post("/v1/transfer", content=body, headers=headers)).status_code == 200
        assert (await c.post("/v1/transfer", content=body, headers=headers)).status_code == 401

        old = str(int(time.time()) - 120)
        stale = {"X-Timestamp": old, "X-Nonce": "n2", "X-Signature": sign(SECRET, old, "n2", body)}
        assert (await c.post("/v1/transfer", content=body, headers=stale)).status_code == 401


async def test_tampered_body_is_rejected(mock: MockCrypto) -> None:
    app = create_mock_app(mock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://c") as c:
        body = transfer().model_dump_json().encode()
        ts, nonce = str(int(time.time())), str(uuid.uuid4())
        headers = {
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(SECRET, ts, nonce, body),
        }
        tampered = body.replace(b"8000000", b"9000000")
        assert (await c.post("/v1/transfer", content=tampered, headers=headers)).status_code == 401


async def test_unreachable_crypto_layer_means_unknown_outcome() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = HttpCryptoClient("http://crypto", SECRET, transport=httpx.MockTransport(refuse))
    with pytest.raises(CryptoUnavailableError):
        await client.transfer(transfer())


async def test_server_error_means_unknown_outcome() -> None:
    client = HttpCryptoClient(
        "http://crypto", SECRET, transport=httpx.MockTransport(lambda _: httpx.Response(502))
    )
    with pytest.raises(CryptoUnavailableError):
        await client.transfer(transfer())


async def test_odd_token_account_never_reaches_the_network(mock: MockCrypto) -> None:
    with pytest.raises(CryptoUnavailableError):
        await client_for(mock).get_delegation("../admin")
