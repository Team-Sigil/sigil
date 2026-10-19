"""An in-memory Solana JSON-RPC for tests, served through `httpx.MockTransport`.

It decodes and verifies each transaction it receives and executes `transfer_checked` the
way the SPL token program does for a delegate (balance, delegated amount, mint, decimals),
so tests exercise the exact bytes the signer would send to devnet.
"""

import base64
import json
import struct
from dataclasses import dataclass, field
from typing import Any

import httpx
from solders.hash import Hash
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID

from signer.chain import DEVNET_GENESIS_HASH

TRANSFER_CHECKED = 12
BLOCKHASH_VALIDITY = 150


@dataclass
class FakeTokenAccount:
    owner: str
    mint: str
    amount: int
    delegate: str | None = None
    delegated_amount: int = 0


@dataclass
class FakeChain:
    token_accounts: dict[str, FakeTokenAccount] = field(default_factory=dict)
    other_accounts: set[str] = field(default_factory=set)  # exist, but aren't token accounts
    statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
    landed: list[Transaction] = field(default_factory=list)
    send_calls: int = 0
    finalized_height: int = 1_000
    genesis_hash: str = DEVNET_GENESIS_HASH
    # Knobs for failure scenarios.
    down: bool = False  # every call fails at the transport
    drop_sends: bool = False  # accept sends but never land them
    lose_send_responses: bool = False  # land the send, then drop the connection
    land_failing: bool = False  # skip preflight; a failing transfer lands with an error
    land_as: str = "confirmed"  # confirmation status of landed transactions

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("chain down", request=request)
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        try:
            result = getattr(self, f"rpc_{method}")(*params)
        except RpcFailureError as failure:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": failure.error}
            )
        if method == "sendTransaction" and self.lose_send_responses:
            raise httpx.ReadTimeout("connection lost after send", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    # ---- RPC methods

    def rpc_getGenesisHash(self) -> str:  # noqa: N802
        return self.genesis_hash

    def rpc_getBlockHeight(self, _config: dict[str, Any]) -> int:  # noqa: N802
        return self.finalized_height

    def rpc_getLatestBlockhash(self, _config: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        blockhash = str(Hash.new_unique())
        return {
            "context": {"slot": 1},
            "value": {
                "blockhash": blockhash,
                "lastValidBlockHeight": self.finalized_height + BLOCKHASH_VALIDITY,
            },
        }

    def rpc_getAccountInfo(self, pubkey: str, _config: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        if pubkey in self.other_accounts:
            value: dict[str, Any] | None = {
                "data": ["", "base64"],
                "owner": "11111111111111111111111111111111",
            }
        elif pubkey in self.token_accounts:
            account = self.token_accounts[pubkey]
            info: dict[str, Any] = {
                "owner": account.owner,
                "mint": account.mint,
                "state": "initialized",
                "isNative": False,
                "tokenAmount": {"amount": str(account.amount), "decimals": 6},
            }
            if account.delegate:
                info["delegate"] = account.delegate
                info["delegatedAmount"] = {"amount": str(account.delegated_amount), "decimals": 6}
            value = {
                "data": {"program": "spl-token", "parsed": {"type": "account", "info": info}},
                "owner": str(TOKEN_PROGRAM_ID),
            }
        else:
            value = None
        return {"context": {"slot": 1}, "value": value}

    def rpc_getSignatureStatuses(  # noqa: N802
        self, signatures: list[str], _config: dict[str, Any]
    ) -> dict[str, Any]:
        return {"context": {"slot": 1}, "value": [self.statuses.get(s) for s in signatures]}

    def rpc_sendTransaction(self, encoded: str, _config: dict[str, Any]) -> str:  # noqa: N802
        self.send_calls += 1
        tx = Transaction.from_bytes(base64.b64decode(encoded))
        tx.verify()  # raises on a bad signature
        signature = str(tx.signatures[0])
        if signature in self.statuses:
            raise RpcFailureError(-32002, "already processed", {"err": "AlreadyProcessed"})
        if self.drop_sends:
            return signature
        err = self._execute(tx)
        if err is not None and not self.land_failing:
            raise RpcFailureError(-32002, "Transaction simulation failed", {"err": err})
        self.landed.append(tx)
        self.statuses[signature] = {"err": err, "confirmationStatus": self.land_as}
        return signature

    # ---- helpers

    def _execute(self, tx: Transaction) -> dict[str, Any] | None:
        message = tx.message
        keys = [str(k) for k in message.account_keys]
        for index, ix in enumerate(message.instructions):
            if keys[ix.program_id_index] != str(TOKEN_PROGRAM_ID):
                return {"InstructionError": [index, "IncorrectProgramId"]}
            data = bytes(ix.data)
            if data[0] != TRANSFER_CHECKED:
                return {"InstructionError": [index, "InvalidInstructionData"]}
            amount, decimals = struct.unpack("<QB", data[1:10])
            source_key, mint, dest_key, authority = (keys[i] for i in ix.accounts)
            source = self.token_accounts.get(source_key)
            dest = self.token_accounts.get(dest_key)
            if source is None or dest is None:
                return {"InstructionError": [index, "InvalidAccountData"]}
            if mint != source.mint or mint != dest.mint or decimals != 6:
                return {"InstructionError": [index, {"Custom": 3}]}  # MintMismatch
            if source.amount < amount:
                return {"InstructionError": [index, {"Custom": 1}]}  # InsufficientFunds
            if authority == source.owner:
                pass
            elif authority == source.delegate:
                if source.delegated_amount < amount:
                    return {"InstructionError": [index, {"Custom": 1}]}
                source.delegated_amount -= amount
                if source.delegated_amount == 0:
                    source.delegate = None
            else:
                return {"InstructionError": [index, {"Custom": 4}]}  # OwnerMismatch
            source.amount -= amount
            dest.amount += amount
        return None


class RpcFailureError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.error = {"code": code, "message": message, "data": data}
