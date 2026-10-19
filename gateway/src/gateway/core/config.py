from functools import cached_property, lru_cache
from pathlib import Path
from typing import Self

from cryptography.x509 import Certificate, load_pem_x509_certificate
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

    # Signed PDF receipts from online orders. A PDF counts as proof only when its signature
    # verifies against one of these roots and names one of these shops; without roots, no
    # PDF ever counts. PEM file holding the issuing certificate(s), and a comma-separated
    # list of accepted signer names.
    pdf_trusted_roots_path: Path | None = None
    # The organisation on Lazada Thailand's receipt-signing certificate (checked against a
    # real receipt, issued by Thai Digital ID CA G3). Exact names, comma-separated.
    pdf_allowed_signers: str = "LAZADA EXPRESS COMPANY LIMITED"

    @property
    def allowed_pdf_signers(self) -> list[str]:
        return [name.strip() for name in self.pdf_allowed_signers.split(",") if name.strip()]

    @cached_property
    def pdf_trusted_roots(self) -> list[Certificate]:
        """The certificates that may issue a receipt-signing certificate."""
        if self.pdf_trusted_roots_path is None:
            return []
        data = self.pdf_trusted_roots_path.read_bytes()
        return [
            load_pem_x509_certificate(block + b"-----END CERTIFICATE-----\n")
            for block in data.split(b"-----END CERTIFICATE-----")
            if b"BEGIN CERTIFICATE" in block
        ]

    # How often pending transfers (crypto layer down at send time) are retried by their key.
    pending_retry_seconds: int = Field(default=60, gt=0)

    rate_limit_per_min: int = Field(default=30, gt=0)
    # Each dependent message (new request, follow-up or proof) runs the agent.
    dependent_messages_per_day: int = Field(default=50, gt=0)
    llm_token_budget_per_guardian_per_day: int = Field(default=200_000, ge=0)

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
