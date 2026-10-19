from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AUSD = 1_000_000  # base units per aUSD (6 decimals)
MIN_SECRET_CHARS = 16


class DevnetOnlyError(ValueError):
    """SOLANA_RPC_URL is not a devnet endpoint (SEC-14)."""


def require_devnet(rpc_url: str) -> None:
    # SPEC §9 asks for "contains devnet"; checking the host is stricter, so a path or query
    # such as `https://mainnet.example/?devnet` can't pass.
    host = urlsplit(rpc_url).hostname or ""
    if "devnet" not in host:
        raise DevnetOnlyError("SOLANA_RPC_URL must be a Solana devnet endpoint")


class Settings(BaseSettings):
    """Signer settings from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Shared with the gateway: its CRYPTO_HMAC_SECRET, AUSD_MINT and AGENT_WALLET_PUBKEY.
    signer_hmac_secret: SecretStr = Field(min_length=MIN_SECRET_CHARS)
    ausd_mint: str
    agent_wallet_pubkey: str

    # The agent wallet's secret key, Fernet-encrypted with SIGNER_MASTER_KEY (SEC-13).
    agent_wallet_key_enc: SecretStr
    signer_master_key: SecretStr

    solana_rpc_url: str
    # Read-only login for the `registered_destinations` view (SPEC §12).
    database_url_readonly: SecretStr

    # The signer's own caps, independent of the policy engine (SEC-03). "Day" is the UTC day.
    signer_max_per_transfer_units: int = Field(default=200 * AUSD, gt=0)
    signer_max_per_day_units: int = Field(default=300 * AUSD, gt=0)
    # Optional cap per source reservoir, so one guardian can't use up everyone's day.
    signer_max_per_source_per_day_units: int | None = Field(default=None, gt=0)

    # Skip the delegation check and the chain on /v1/transfer; signatures are `mock_<uuid>`.
    mock_chain: bool = False

    # The signer's own durable state: idempotency ledger and seen nonces (SEC-07).
    signer_db_path: str = "data/signer.sqlite3"
    # How long one /v1/transfer waits for `confirmed` before answering 503; must stay well
    # under the gateway's 60 s timeout.
    confirm_timeout_seconds: float = Field(default=40.0, gt=0, le=50)

    signer_host: str = "127.0.0.1"
    signer_port: int = 8300

    @model_validator(mode="after")
    def check_devnet(self) -> Self:
        require_devnet(self.solana_rpc_url)
        return self

    def secret_values(self) -> list[str]:
        """Values the log filter must never let through (SEC-13)."""
        return [
            self.signer_hmac_secret.get_secret_value(),
            self.agent_wallet_key_enc.get_secret_value(),
            self.signer_master_key.get_secret_value(),
            self.database_url_readonly.get_secret_value(),
            self.solana_rpc_url,  # may carry an RPC provider's API key
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
