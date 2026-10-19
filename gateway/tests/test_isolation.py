import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "gateway"
ENGINE_TRANSFERS = SRC / "engine" / "transfers.py"
BUILDS_TRANSFER = re.compile(r"(?<!class )CryptoTransferRequest\(")


def test_only_the_engine_builds_crypto_transfers() -> None:
    """SEC-02: every transfer passes the engine; no other code path reaches the signer."""
    builders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if BUILDS_TRANSFER.search(path.read_text())
    ]
    assert builders == ["engine/transfers.py"]
