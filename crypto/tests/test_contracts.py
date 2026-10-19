"""`signer/contracts.py` is a verbatim copy of the gateway's `contracts/crypto.py`, the source
of truth for the API shapes. Checked when the gateway repo sits next to this one."""

from pathlib import Path

import pytest

GATEWAY_CONTRACT = (
    Path(__file__).parents[2] / "gateway" / "src" / "gateway" / "contracts" / "crypto.py"
)
OUR_CONTRACT = Path(__file__).parents[1] / "src" / "signer" / "contracts.py"


@pytest.mark.skipif(not GATEWAY_CONTRACT.exists(), reason="gateway/ not found next to crypto/")
def test_contract_matches_gateway() -> None:
    assert OUR_CONTRACT.read_text() == GATEWAY_CONTRACT.read_text()
