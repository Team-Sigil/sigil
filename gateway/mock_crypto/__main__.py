"""Serve the mock crypto layer: `python -m mock_crypto` (port 8300, or MOCK_CRYPTO_PORT)."""

import os

import uvicorn

from mock_crypto.app import MockCrypto, create_mock_app

if __name__ == "__main__":
    mock = MockCrypto(secret=os.environ["CRYPTO_HMAC_SECRET"])
    uvicorn.run(
        create_mock_app(mock),
        host=os.environ.get("MOCK_CRYPTO_HOST", "127.0.0.1"),
        port=int(os.environ.get("MOCK_CRYPTO_PORT", "8300")),
    )
