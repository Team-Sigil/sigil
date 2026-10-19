"""The signer's own durable state in SQLite (SEC-07): one row per idempotency key, and the
nonces seen in the last 10 minutes.

A transfer row moves `claimed → sent → confirmed | failed`. Its signature and last valid
block height are written *before* the transaction is sent, so after a crash a retry asks the
chain about that signature instead of signing again. A row is worked on only under a lease
(`lease_token`, `lease_until`); a crashed worker's lease simply expires.

Every write is one short `BEGIN IMMEDIATE` transaction, so claims are atomic even across
processes. Calls are synchronous: they are local and take well under a millisecond.
"""

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from signer.contracts import CryptoTransferRequest

NONCE_TTL_SECONDS = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    transfer_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    source_token_account TEXT NOT NULL,
    destination_token_account TEXT NOT NULL,
    mint TEXT NOT NULL,
    amount_units INTEGER NOT NULL,
    day TEXT NOT NULL,
    state TEXT NOT NULL,
    tx_signature TEXT,
    last_valid_block_height INTEGER,
    raw_tx BLOB,
    error_code TEXT,
    lease_token TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transfers_day ON transfers (day, source_token_account);
CREATE TABLE IF NOT EXISTS nonces (
    nonce TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
"""


class TransferState(StrEnum):
    CLAIMED = "claimed"  # passed the checks and counted against the caps; nothing signed
    SENT = "sent"  # signed; the signature is stored; outcome not known yet
    CONFIRMED = "confirmed"
    FAILED = "failed"


FINAL_STATES = (TransferState.CONFIRMED, TransferState.FAILED)


@dataclass(frozen=True)
class TransferRecord:
    idempotency_key: str
    request_hash: str
    family_id: str
    source_token_account: str
    destination_token_account: str
    mint: str
    amount_units: int
    state: TransferState
    tx_signature: str | None
    last_valid_block_height: int | None
    raw_tx: bytes | None
    error_code: str | None

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_STATES


class ClaimOutcome(StrEnum):
    ACQUIRED = "acquired"  # this caller now holds the lease and must settle the transfer
    FINAL = "final"  # already settled; return the stored result
    BUSY = "busy"  # another request holds the lease
    MISMATCH = "mismatch"  # same key, different request
    OVER_DAY_CAP = "over_day_cap"


@dataclass(frozen=True)
class Claim:
    outcome: ClaimOutcome
    record: TransferRecord | None = None


@dataclass(frozen=True)
class DayCaps:
    day: str
    max_per_day_units: int
    max_per_source_per_day_units: int | None


class SignerStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we issue BEGIN/COMMIT ourselves.
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")  # a stored signature must survive a crash
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ---- nonces

    def remember_nonce(self, nonce: str, now: float) -> bool:
        """Record a nonce; False if it was already seen within the TTL."""
        with self._transaction():
            self._db.execute("DELETE FROM nonces WHERE seen_at < ?", (now - NONCE_TTL_SECONDS,))
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO nonces (nonce, seen_at) VALUES (?, ?)", (nonce, now)
            )
            return cursor.rowcount == 1

    # ---- transfers

    def get(self, idempotency_key: str) -> TransferRecord | None:
        row = self._db.execute(
            "SELECT * FROM transfers WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return _record(row) if row else None

    def claim(
        self,
        req: CryptoTransferRequest,
        request_hash: str,
        caps: DayCaps,
        lease_token: str,
        lease_until: float,
        now: float,
    ) -> Claim:
        """Take the lease on `req.idempotency_key`. A new key is inserted only if it fits the
        day caps; an existing, unsettled key whose lease expired is taken over as it is
        (its amount already counts against the caps)."""
        with self._transaction():
            row = self._db.execute(
                "SELECT * FROM transfers WHERE idempotency_key = ?", (req.idempotency_key,)
            ).fetchone()
            if row is not None:
                record = _record(row)
                if record.is_final:
                    return Claim(ClaimOutcome.FINAL, record)
                if row["lease_until"] > now:
                    return Claim(ClaimOutcome.BUSY, record)
                if record.request_hash != request_hash:
                    return Claim(ClaimOutcome.MISMATCH, record)
                self._db.execute(
                    "UPDATE transfers SET lease_token = ?, lease_until = ?, updated_at = ? "
                    "WHERE idempotency_key = ?",
                    (lease_token, lease_until, now, req.idempotency_key),
                )
                return Claim(ClaimOutcome.ACQUIRED, record)

            # Unsettled transfers count too: they may still land.
            day_total = self._db.execute(
                "SELECT COALESCE(SUM(amount_units), 0) FROM transfers WHERE day = ? AND state != ?",
                (caps.day, TransferState.FAILED),
            ).fetchone()[0]
            if day_total + req.amount_units > caps.max_per_day_units:
                return Claim(ClaimOutcome.OVER_DAY_CAP)
            if caps.max_per_source_per_day_units is not None:
                source_total = self._db.execute(
                    "SELECT COALESCE(SUM(amount_units), 0) FROM transfers "
                    "WHERE day = ? AND source_token_account = ? AND state != ?",
                    (caps.day, req.source_token_account, TransferState.FAILED),
                ).fetchone()[0]
                if source_total + req.amount_units > caps.max_per_source_per_day_units:
                    return Claim(ClaimOutcome.OVER_DAY_CAP)

            self._db.execute(
                "INSERT INTO transfers (idempotency_key, request_hash, transfer_id, family_id, "
                "source_token_account, destination_token_account, mint, amount_units, day, "
                "state, lease_token, lease_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req.idempotency_key,
                    request_hash,
                    str(req.transfer_id),
                    str(req.family_id),
                    req.source_token_account,
                    req.destination_token_account,
                    req.mint,
                    req.amount_units,
                    caps.day,
                    TransferState.CLAIMED,
                    lease_token,
                    lease_until,
                    now,
                    now,
                ),
            )
        inserted = self.get(req.idempotency_key)
        assert inserted is not None
        return Claim(ClaimOutcome.ACQUIRED, inserted)

    def mark_sent(
        self,
        idempotency_key: str,
        lease_token: str,
        tx_signature: str,
        last_valid_block_height: int,
        raw_tx: bytes,
        now: float,
    ) -> bool:
        """Store the signature before sending. False if the lease was lost: then the caller
        must not send."""
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE transfers SET state = ?, tx_signature = ?, last_valid_block_height = ?, "
                "raw_tx = ?, updated_at = ? "
                "WHERE idempotency_key = ? AND lease_token = ? AND state IN (?, ?)",
                (
                    TransferState.SENT,
                    tx_signature,
                    last_valid_block_height,
                    raw_tx,
                    now,
                    idempotency_key,
                    lease_token,
                    TransferState.CLAIMED,
                    TransferState.SENT,
                ),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        idempotency_key: str,
        lease_token: str,
        state: TransferState,
        tx_signature: str | None,
        error_code: str | None,
        now: float,
    ) -> bool:
        """Store the final result. False if the lease was lost."""
        assert state in FINAL_STATES
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE transfers SET state = ?, tx_signature = ?, error_code = ?, "
                "lease_token = NULL, lease_until = 0, updated_at = ? "
                "WHERE idempotency_key = ? AND lease_token = ? AND state IN (?, ?)",
                (
                    state,
                    tx_signature,
                    error_code,
                    now,
                    idempotency_key,
                    lease_token,
                    TransferState.CLAIMED,
                    TransferState.SENT,
                ),
            )
            return cursor.rowcount == 1

    def release(self, idempotency_key: str, lease_token: str, now: float) -> None:
        """Give up the lease without a final result, so the next retry can reconcile."""
        with self._transaction():
            self._db.execute(
                "UPDATE transfers SET lease_token = NULL, lease_until = 0, updated_at = ? "
                "WHERE idempotency_key = ? AND lease_token = ?",
                (now, idempotency_key, lease_token),
            )

    def _transaction(self) -> "_Immediate":
        return _Immediate(self._db)


class _Immediate:
    """`BEGIN IMMEDIATE … COMMIT`, rolled back on error."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._db.execute("ROLLBACK" if exc_type else "COMMIT")


def _record(row: sqlite3.Row) -> TransferRecord:
    return TransferRecord(
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        family_id=row["family_id"],
        source_token_account=row["source_token_account"],
        destination_token_account=row["destination_token_account"],
        mint=row["mint"],
        amount_units=row["amount_units"],
        state=TransferState(row["state"]),
        tx_signature=row["tx_signature"],
        last_valid_block_height=row["last_valid_block_height"],
        raw_tx=row["raw_tx"],
        error_code=row["error_code"],
    )
