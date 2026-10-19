"""Shared helpers for the devnet scripts: demo keypairs in `.devnet/` (git-ignored, devnet
only), and sending a transaction until it is confirmed.

These keys are throwaway demo keys. The agent wallet's key is never stored here: it exists
only encrypted, in AGENT_WALLET_KEY_ENC.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from signer.chain import SolanaRpc
from signer.config import DevnetOnlyError, require_devnet

STATE_DIR = Path(".devnet")
SEED_FILE = STATE_DIR / "seed.json"
DEFAULT_RPC_URL = "https://api.devnet.solana.com"
FAUCET_URL = "https://faucet.solana.com"
CONFIRM_TIMEOUT_SECONDS = 60


class DevnetRpc(SolanaRpc):
    """The service's RPC client plus what only the setup scripts need."""

    async def account_exists(self, pubkey: Pubkey) -> bool:
        result = await self._call("getAccountInfo", [str(pubkey), {"encoding": "base64"}])
        return result["value"] is not None

    async def get_balance(self, pubkey: Pubkey) -> int:
        result = await self._call("getBalance", [str(pubkey), {"commitment": "confirmed"}])
        return int(result["value"])

    async def get_token_balance(self, token_account: Pubkey) -> int:
        result = await self._call("getTokenAccountBalance", [str(token_account)])
        return int(result["value"]["amount"])

    async def get_minimum_balance_for_rent_exemption(self, size: int) -> int:
        return int(await self._call("getMinimumBalanceForRentExemption", [size]))

    async def request_airdrop(self, pubkey: Pubkey, lamports: int) -> str:
        return str(await self._call("requestAirdrop", [str(pubkey), lamports]))

    async def send_and_confirm(
        self, instructions: list[Instruction], payer: Keypair, signers: list[Keypair]
    ) -> str:
        blockhash, _ = await self.get_latest_blockhash()
        tx = Transaction.new_signed_with_payer(instructions, payer.pubkey(), signers, blockhash)
        await self.send_transaction(bytes(tx))
        signature = str(tx.signatures[0])
        await self.wait_for(signature)
        return signature

    async def wait_for(self, signature: str) -> None:
        for _ in range(CONFIRM_TIMEOUT_SECONDS):
            status = await self.get_signature_status(signature)
            if status is not None and status.error_code:
                sys.exit(f"transaction {signature} failed: {status.error_code}")
            if status is not None and status.is_confirmed:
                return
            await asyncio.sleep(1)
        sys.exit(f"transaction {signature} not confirmed after {CONFIRM_TIMEOUT_SECONDS} s")


def rpc_url() -> str:
    url = os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC_URL)
    try:
        require_devnet(url)
    except DevnetOnlyError as exc:
        sys.exit(str(exc))
    return url


def load_or_create_keypair(name: str) -> Keypair:
    """`.devnet/<name>.json` in solana-keygen format, so the Solana CLI can use it too."""
    path = STATE_DIR / f"{name}.json"
    if path.exists():
        return Keypair.from_bytes(bytes(json.loads(path.read_text())))
    STATE_DIR.mkdir(exist_ok=True)
    keypair = Keypair()
    path.write_text(json.dumps(list(bytes(keypair))))
    path.chmod(0o600)
    return keypair


def load_keypair(name: str) -> Keypair:
    path = STATE_DIR / f"{name}.json"
    if not path.exists():
        sys.exit(f"{path} not found: run `python -m scripts.setup_devnet` first")
    return Keypair.from_bytes(bytes(json.loads(path.read_text())))


def load_seed() -> dict[str, Any]:
    if not SEED_FILE.exists():
        sys.exit(f"{SEED_FILE} not found: run `python -m scripts.setup_devnet` first")
    seed: dict[str, Any] = json.loads(SEED_FILE.read_text())
    return seed


def explorer(signature: str) -> str:
    return f"https://explorer.solana.com/tx/{signature}?cluster=devnet"
