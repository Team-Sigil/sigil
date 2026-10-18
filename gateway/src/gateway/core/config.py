from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_JWT_TTL_HOURS = 12
MIN_TOKEN_CHARS = 16


class Settings(BaseSettings):
    """Gateway settings from environment variables (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    database_url_migrations: str | None = None

    # Sessions for the app layer (Sign-In With Solana, SPEC SEC-09).
    jwt_secret: SecretStr
    jwt_ttl_hours: int = Field(default=MAX_JWT_TTL_HOURS, gt=0, le=MAX_JWT_TTL_HOURS)
    siws_domain: str

    # Chats and proofs at rest (SPEC SEC-15).
    data_encryption_key: SecretStr

    # Agent layer: we call its inbound API with AGENT_INBOUND_TOKEN; it calls our platform
    # API with the maker or auditor token.
    agent_url: str = "http://agent:8100"
    agent_inbound_token: SecretStr
    platform_maker_token: SecretStr = Field(min_length=MIN_TOKEN_CHARS)
    platform_auditor_token: SecretStr = Field(min_length=MIN_TOKEN_CHARS)

    # Crypto layer: HMAC-signed transfer instructions and delegation reads.
    crypto_url: str = "http://crypto:8300"
    crypto_hmac_secret: SecretStr
    ausd_mint: str
    agent_wallet_pubkey: str

    # Verified in-app capture (iOS App Attest). Off (no device can enrol) until the app id is set.
    apple_app_id: str | None = None  # "<Team ID>.<bundle id>"
    apple_app_attest_production: bool = True
    # Only verified captures count as proof; must match the agent's AGENT_CAMERA_ONLY_PROOFS.
    camera_only_proofs: bool = False

    rate_limit_per_min: int = Field(default=30, gt=0)
    llm_token_budget_per_family_per_day: int = Field(default=200_000, ge=0)

    @model_validator(mode="after")
    def distinct_agent_tokens(self) -> Self:
        # Only the Auditor's token may post verdicts; a shared token would let the maker
        # forge its own "agree".
        maker = self.platform_maker_token.get_secret_value()
        if maker == self.platform_auditor_token.get_secret_value():
            raise ValueError("PLATFORM_MAKER_TOKEN and PLATFORM_AUDITOR_TOKEN must differ")
        return self

    def secret_values(self) -> list[str]:
        """Plain secret values, used by the log redaction filter."""
        secrets = [
            self.jwt_secret,
            self.data_encryption_key,
            self.agent_inbound_token,
            self.platform_maker_token,
            self.platform_auditor_token,
            self.crypto_hmac_secret,
        ]
        return [s.get_secret_value() for s in secrets if s.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
