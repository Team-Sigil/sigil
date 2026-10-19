import os

from cryptography.fernet import Fernet

# Test-only values, set before any gateway module reads settings.
TEST_ENV = {
    "DATABASE_URL": "postgresql+psycopg://sigil_app:sigil_app@localhost:5432/sigil_test",
    "JWT_SECRET": "test-jwt-secret-that-is-long-enough-for-hs256",
    "SIWS_DOMAIN": "localhost:3000",
    "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "AGENT_URL": "http://agent.test:8100",
    "AGENT_INBOUND_TOKEN": "test-agent-inbound-token",
    "PLATFORM_MAKER_TOKEN": "test-maker-token-0123456789",
    "PLATFORM_AUDITOR_TOKEN": "test-auditor-token-0123456789",
    "CRYPTO_HMAC_SECRET": "test-crypto-hmac-secret",
    "AUSD_MINT": "So11111111111111111111111111111111111111112",
    "AGENT_WALLET_PUBKEY": "So11111111111111111111111111111111111111112",
}
for _key, _value in TEST_ENV.items():
    os.environ[_key] = _value
