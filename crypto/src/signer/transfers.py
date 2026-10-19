"""The signer's transfer workflow (SPEC §9, SEC-03, SEC-07).

What the gateway concludes from each answer decides how we may answer:
- `confirmed` → money moved;
- `failed` or a 4xx → money did not and will not move (the gateway releases its reservation);
- 503 → unknown (the gateway retries the same key until it gets a definite answer).
So `failed` and 4xx are returned only when nothing was or can still be sent; anything else
is 503.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from signer.chain import (
    ChainUnavailableError,
    SignatureStatus,
    SolanaRpc,
    TransactionRejectedError,
    build_transfer,
)
from signer.contracts import (
    CryptoTransferRequest,
    CryptoTransferResponse,
    CryptoTransferStatus,
    Delegation,
)
from signer.keys import AgentWallet
from signer.registry import Registry
from signer.store import ClaimOutcome, DayCaps, SignerStore, TransferRecord, TransferState

logger = logging.getLogger(__name__)

# Longer than one /v1/transfer can take (RPC timeouts + the confirmation wait), so a live
# worker never loses its lease; a crashed worker's lease frees the key after this.
LEASE_SECONDS = 120.0
CONFIRM_POLL_SECONDS = 1.0


class TransferRefusedError(Exception):
    """Refused before anything was signed: a 4xx."""

    def __init__(self, status_code: int, error_code: str) -> None:
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code


class OutcomeUnknownError(Exception):
    """The transfer may still land, or another request is working on it: a 503."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class SignerLimits:
    ausd_mint: str
    max_per_transfer_units: int
    max_per_day_units: int
    max_per_source_per_day_units: int | None


def request_hash(req: CryptoTransferRequest) -> str:
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class Signer:
    def __init__(
        self,
        *,
        store: SignerStore,
        registry: Registry,
        rpc: SolanaRpc,
        wallet: AgentWallet,
        limits: SignerLimits,
        mock_chain: bool = False,
        confirm_timeout_seconds: float = 40.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._registry = registry
        self._rpc = rpc
        self._wallet = wallet
        self._limits = limits
        self._mock_chain = mock_chain
        self._confirm_timeout = confirm_timeout_seconds
        self._clock = clock
        self._sleep = sleep

    async def get_delegation(self, token_account: str) -> Delegation | None:
        account = await self._rpc.get_token_account(token_account)
        if account is None:
            return None
        return Delegation(
            token_account=account.token_account,
            owner=account.owner,
            delegate=account.delegate,
            delegated_amount_units=account.delegated_amount_units,
        )

    async def transfer(self, req: CryptoTransferRequest) -> CryptoTransferResponse:
        # Idempotency first: a retry of a transfer we already sent must get its result back
        # even if the registration or today's caps would now refuse it.
        existing = self._store.get(req.idempotency_key)
        if existing is not None and existing.is_final:
            return _response(existing)

        if existing is None:
            await self._check_request(req)

        now = self._clock()
        lease = uuid.uuid4().hex
        caps = DayCaps(
            day=datetime.fromtimestamp(now, UTC).date().isoformat(),
            max_per_day_units=self._limits.max_per_day_units,
            max_per_source_per_day_units=self._limits.max_per_source_per_day_units,
        )
        claim = self._store.claim(req, request_hash(req), caps, lease, now + LEASE_SECONDS, now)
        match claim.outcome:
            case ClaimOutcome.FINAL:
                assert claim.record is not None
                return _response(claim.record)
            case ClaimOutcome.BUSY:
                raise OutcomeUnknownError("IN_PROGRESS")
            case ClaimOutcome.MISMATCH:
                # Same key, different body. The first request may still land, so this is not
                # a refusal the gateway may act on.
                logger.warning("Key %s reused with a different request", req.idempotency_key)
                raise OutcomeUnknownError("IDEMPOTENCY_MISMATCH")
            case ClaimOutcome.OVER_DAY_CAP:
                raise TransferRefusedError(403, "OVER_SIGNER_CAP")
        assert claim.record is not None

        settled = False
        try:
            response = await self._settle(claim.record, lease)
            settled = True
            return response
        finally:
            if not settled:
                # Let the next retry reconcile straight away instead of waiting for the lease.
                self._store.release(req.idempotency_key, lease, self._clock())

    async def _check_request(self, req: CryptoTransferRequest) -> None:
        """SPEC §9 steps 3-4 (registered accounts, mint, per-transfer cap) for a key we have
        never seen. Nothing is signed yet, so a refusal here is a 4xx."""
        registered = await self._registry.lookup(req.family_id)
        if registered is None:
            logger.warning("Transfer %s: family %s not registered", req.transfer_id, req.family_id)
            raise TransferRefusedError(403, "SIGNER_REJECTED")
        if req.destination_token_account != registered.dependent_token_account:
            logger.warning("Transfer %s: destination is not the family's", req.transfer_id)
            raise TransferRefusedError(403, "SIGNER_REJECTED")
        # Every guardian's reservoir delegates to the same agent wallet, so the source must be
        # this family's, or one family could be paid from another guardian's reservoir.
        if req.source_token_account != registered.guardian_token_account:
            logger.warning("Transfer %s: source is not the family's", req.transfer_id)
            raise TransferRefusedError(403, "SIGNER_REJECTED")
        if req.mint != self._limits.ausd_mint:
            logger.warning("Transfer %s: mint is not aUSD", req.transfer_id)
            raise TransferRefusedError(403, "SIGNER_REJECTED")
        if req.amount_units > self._limits.max_per_transfer_units:
            raise TransferRefusedError(403, "OVER_SIGNER_CAP")

    async def _settle(self, record: TransferRecord, lease: str) -> CryptoTransferResponse:
        if self._mock_chain:
            return self._finish(record, lease, TransferState.CONFIRMED, f"mock_{uuid.uuid4().hex}")

        if record.state is TransferState.SENT:
            response = await self._reconcile(record, lease)
            if response is not None:
                return response
            logger.warning(
                "Transfer %s: %s expired without landing; signing a new transaction",
                record.idempotency_key,
                record.tx_signature,
            )

        # Check 6: the on-chain delegation, read fresh.
        account = await self._rpc.get_token_account(record.source_token_account)
        if (
            account is None
            or account.delegate != str(self._wallet.pubkey)
            or account.delegated_amount_units < record.amount_units
        ):
            return self._finish(
                record, lease, TransferState.FAILED, None, "INSUFFICIENT_DELEGATION"
            )

        # Step 7: sign, store the signature, and only then send (SEC-07).
        blockhash, last_valid_block_height = await self._rpc.get_latest_blockhash()
        tx = build_transfer(
            self._wallet,
            record.source_token_account,
            record.mint,
            record.destination_token_account,
            record.amount_units,
            blockhash,
        )
        signature, raw_tx = str(tx.signatures[0]), bytes(tx)
        stored = self._store.mark_sent(
            record.idempotency_key, lease, signature, last_valid_block_height, raw_tx, self._clock()
        )
        if not stored:
            raise OutcomeUnknownError("IN_PROGRESS")
        try:
            await self._rpc.send_transaction(raw_tx)
        except TransactionRejectedError as exc:
            # Refused in preflight, never forwarded; any earlier signature has expired.
            return self._finish(record, lease, TransferState.FAILED, None, exc.error_code)
        logger.info("Transfer %s sent: %s", record.idempotency_key, signature)

        status = await self._wait_for_confirmation(signature)
        if status is None:
            raise OutcomeUnknownError("NOT_CONFIRMED_YET")
        return self._outcome(record, lease, signature, status)

    async def _reconcile(self, record: TransferRecord, lease: str) -> CryptoTransferResponse | None:
        """A key that was signed and sent before but has no result. Returns the result, raises
        OutcomeUnknownError while it may still land, or returns None once its blockhash has
        expired unseen, when it can never land and a new transaction is safe."""
        assert record.tx_signature is not None and record.last_valid_block_height is not None
        # Block height first: if it is already past, a "not found" below is final.
        expired = await self._rpc.get_block_height() > record.last_valid_block_height
        status = await self._rpc.get_signature_status(record.tx_signature)
        if status is not None:
            return self._outcome(record, lease, record.tx_signature, status)
        if expired:
            return None
        if record.raw_tx is not None:
            # Same bytes, same signature: rebroadcasting can't pay twice, and helps it land.
            # Any error is fine here: the next retry asks the chain again.
            with contextlib.suppress(ChainUnavailableError, TransactionRejectedError):
                await self._rpc.send_transaction(record.raw_tx)
        raise OutcomeUnknownError("IN_FLIGHT")

    async def _wait_for_confirmation(self, signature: str) -> SignatureStatus | None:
        deadline = self._clock() + self._confirm_timeout
        while self._clock() < deadline:
            try:
                status = await self._rpc.get_signature_status(signature)
            except ChainUnavailableError:
                status = None
            if status is not None and (status.error_code or status.is_confirmed):
                return status
            await self._sleep(CONFIRM_POLL_SECONDS)
        return None

    def _outcome(
        self, record: TransferRecord, lease: str, signature: str, status: SignatureStatus
    ) -> CryptoTransferResponse:
        if status.error_code is not None:
            logger.warning(
                "Transfer %s landed and failed (%s): %s",
                record.idempotency_key,
                status.error_code,
                signature,
            )
            # Kept in our ledger for the explorer; the contract's `failed` carries no signature.
            return self._finish(record, lease, TransferState.FAILED, signature, status.error_code)
        if status.is_confirmed:
            return self._finish(record, lease, TransferState.CONFIRMED, signature)
        raise OutcomeUnknownError("NOT_CONFIRMED_YET")

    def _finish(
        self,
        record: TransferRecord,
        lease: str,
        state: TransferState,
        signature: str | None,
        error_code: str | None = None,
    ) -> CryptoTransferResponse:
        stored = self._store.finish(
            record.idempotency_key, lease, state, signature, error_code, self._clock()
        )
        if not stored:
            # Lost the lease: another request may have signed again under this key, so our
            # view of the outcome may no longer be the key's.
            raise OutcomeUnknownError("IN_PROGRESS")
        logger.info("Transfer %s %s %s", record.idempotency_key, state, signature or error_code)
        final = self._store.get(record.idempotency_key)
        assert final is not None
        return _response(final)


def _response(record: TransferRecord) -> CryptoTransferResponse:
    if record.state is TransferState.CONFIRMED:
        return CryptoTransferResponse(
            status=CryptoTransferStatus.CONFIRMED, tx_signature=record.tx_signature
        )
    return CryptoTransferResponse(
        status=CryptoTransferStatus.FAILED, error_code=record.error_code or "TX_FAILED"
    )
