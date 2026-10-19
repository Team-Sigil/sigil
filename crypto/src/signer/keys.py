"""The agent wallet key (SEC-13): Fernet-encrypted at rest, decrypted once at startup, kept
only in this process's memory, never logged or returned.
"""

from cryptography.fernet import Fernet, InvalidToken
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

SECRET_KEY_BYTES = 64  # solana keypair: 32-byte seed + 32-byte public key


class AgentKeyError(Exception):
    """The agent wallet key is missing, undecryptable or doesn't match AGENT_WALLET_PUBKEY.
    Messages never include key material."""


def encrypt_secret_key(secret_key: bytes, master_key: str) -> str:
    return Fernet(master_key.encode()).encrypt(secret_key).decode()


class AgentWallet:
    """Signs transactions as the agent wallet. `repr`/`str` show only the public key:
    solders' own `str(Keypair)` is the secret key in base58."""

    __slots__ = ("_keypair",)

    def __init__(self, keypair: Keypair) -> None:
        self._keypair = keypair

    @classmethod
    def from_encrypted(cls, key_enc: str, master_key: str, expected_pubkey: str) -> "AgentWallet":
        try:
            secret_key = Fernet(master_key.encode()).decrypt(key_enc.encode())
        except (InvalidToken, ValueError):
            # `from None`: the cause's message could quote part of the key material.
            raise AgentKeyError(
                "AGENT_WALLET_KEY_ENC can't be decrypted with SIGNER_MASTER_KEY"
            ) from None
        if len(secret_key) != SECRET_KEY_BYTES:
            raise AgentKeyError("AGENT_WALLET_KEY_ENC is not a 64-byte Solana secret key")
        try:
            keypair = Keypair.from_bytes(secret_key)
        except ValueError:
            raise AgentKeyError("AGENT_WALLET_KEY_ENC is not a valid Solana secret key") from None
        if str(keypair.pubkey()) != expected_pubkey:
            raise AgentKeyError("AGENT_WALLET_KEY_ENC does not derive to AGENT_WALLET_PUBKEY")
        return cls(keypair)

    @property
    def pubkey(self) -> Pubkey:
        return self._keypair.pubkey()

    def sign_transaction(self, instructions: list[Instruction], blockhash: Hash) -> Transaction:
        return Transaction.new_signed_with_payer(
            instructions, self._keypair.pubkey(), [self._keypair], blockhash
        )

    def __repr__(self) -> str:
        return f"AgentWallet({self._keypair.pubkey()})"

    __str__ = __repr__

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("AgentWallet can't be pickled")
