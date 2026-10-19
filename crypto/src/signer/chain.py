"""Solana devnet over JSON-RPC (httpx), and the one transaction the signer ever builds:
`transfer_checked` as the reservoir's delegate.
"""

import base64
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import transfer_checked
from spl.token.models import TransferCheckedParams

from signer.keys import AgentWallet

AUSD_DECIMALS = 6
DEVNET_GENESIS_HASH = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
RPC_TIMEOUT_SECONDS = 10.0
PREFLIGHT_FAILURE = -32002  # "Transaction simulation failed": the node did not forward it

# SPL token program errors worth naming for the gateway; others become TX_FAILED / TX_REJECTED.
TOKEN_ERROR_CODES = {1: "INSUFFICIENT_FUNDS", 4: "OWNER_MISMATCH", 17: "ACCOUNT_FROZEN"}


class ChainUnavailableError(Exception):
    """The RPC could not be reached or answered outside JSON-RPC. Outcome unknown."""


class TransactionRejectedError(Exception):
    """The RPC refused the transaction in preflight; it was never forwarded."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class TokenAccount:
    token_account: str
    owner: str
    mint: str
    delegate: str | None
    delegated_amount_units: int


class Confirmation(StrEnum):
    PROCESSED = "processed"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"


@dataclass(frozen=True)
class SignatureStatus:
    confirmation: Confirmation | None
    error_code: str | None  # set when the transaction landed and failed

    @property
    def is_confirmed(self) -> bool:
        return self.confirmation in (Confirmation.CONFIRMED, Confirmation.FINALIZED)


def error_code_for(err: Any, default: str) -> str:
    """Map a transaction error such as `{"InstructionError": [0, {"Custom": 1}]}`."""
    if isinstance(err, dict) and isinstance(err.get("InstructionError"), list):
        detail = err["InstructionError"][-1]
        if isinstance(detail, dict) and detail.get("Custom") in TOKEN_ERROR_CODES:
            return TOKEN_ERROR_CODES[detail["Custom"]]
    return default


def build_transfer(
    wallet: AgentWallet,
    source: str,
    mint: str,
    destination: str,
    amount_units: int,
    blockhash: Hash,
) -> Transaction:
    """One `transfer_checked` instruction, signed by the agent wallet as fee payer and as the
    reservoir's delegate. No memo or any other instruction (SEC-15)."""
    instruction = transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=Pubkey.from_string(source),
            mint=Pubkey.from_string(mint),
            dest=Pubkey.from_string(destination),
            owner=wallet.pubkey,
            amount=amount_units,
            decimals=AUSD_DECIMALS,
        )
    )
    return wallet.sign_transaction([instruction], blockhash)


class SolanaRpc:
    def __init__(
        self,
        rpc_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = RPC_TIMEOUT_SECONDS,
    ) -> None:
        self._url = rpc_url
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._next_id = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_token_account(self, token_account: str) -> TokenAccount | None:
        """The SPL token account, read fresh at `confirmed`; None if it doesn't exist or is not
        an SPL token account."""
        result = await self._call(
            "getAccountInfo", [token_account, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict):
            return None
        data = value.get("data")
        # Non-token accounts come back base64-encoded (a list), not parsed.
        if not isinstance(data, dict) or data.get("program") != "spl-token":
            return None
        parsed = data.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") != "account":
            return None
        try:
            info = parsed["info"]
            delegate = info.get("delegate")
            # The amount is a decimal string on chain; never go through a float.
            delegated = int(info["delegatedAmount"]["amount"]) if delegate else 0
            return TokenAccount(
                token_account=token_account,
                owner=str(info["owner"]),
                mint=str(info["mint"]),
                delegate=str(delegate) if delegate else None,
                delegated_amount_units=delegated,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainUnavailableError("getAccountInfo: unexpected token account") from exc

    async def get_latest_blockhash(self) -> tuple[Hash, int]:
        result = await self._call("getLatestBlockhash", [{"commitment": "confirmed"}])
        value = result["value"]
        return Hash.from_string(value["blockhash"]), int(value["lastValidBlockHeight"])

    async def get_block_height(self) -> int:
        # Finalized: once this passes a transaction's last valid block height, that
        # transaction can never land.
        return int(await self._call("getBlockHeight", [{"commitment": "finalized"}]))

    async def get_genesis_hash(self) -> str:
        return str(await self._call("getGenesisHash", []))

    async def send_transaction(self, raw_tx: bytes) -> None:
        """Raises TransactionRejectedError on a preflight failure (never forwarded) and
        ChainUnavailableError when the outcome is unknown."""
        params = [
            base64.b64encode(raw_tx).decode(),
            {"encoding": "base64", "preflightCommitment": "confirmed"},
        ]
        try:
            await self._call("sendTransaction", params)
        except RpcError as exc:
            if exc.code == PREFLIGHT_FAILURE:
                err = exc.data.get("err") if isinstance(exc.data, dict) else None
                raise TransactionRejectedError(error_code_for(err, "TX_REJECTED")) from exc
            raise ChainUnavailableError(f"sendTransaction failed ({exc.code})") from exc

    async def get_signature_status(self, signature: str) -> SignatureStatus | None:
        """None if the cluster has no record of the signature."""
        result = await self._call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
        )
        status = result["value"][0]
        if status is None:
            return None
        err = status.get("err")
        confirmation = status.get("confirmationStatus")
        return SignatureStatus(
            confirmation=Confirmation(confirmation) if confirmation else None,
            error_code=error_code_for(err, "TX_FAILED") if err is not None else None,
        )

    async def _call(self, method: str, params: list[Any]) -> Any:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        try:
            response = await self._http.post(self._url, json=payload)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainUnavailableError(f"{method}: {type(exc).__name__}") from None
        if not isinstance(body, dict):
            raise ChainUnavailableError(f"{method}: malformed response")
        if "error" in body:
            error = body["error"] if isinstance(body["error"], dict) else {}
            raise RpcError(method, error.get("code"), error.get("message"), error.get("data"))
        if response.status_code != 200 or "result" not in body:
            raise ChainUnavailableError(f"{method}: HTTP {response.status_code}")
        return body["result"]


class RpcError(ChainUnavailableError):
    """A JSON-RPC error object. Treated as 'outcome unknown' unless the caller knows better."""

    def __init__(self, method: str, code: Any, message: Any, data: Any) -> None:
        super().__init__(f"{method}: RPC error {code}: {message}")
        self.code = code
        self.data = data
