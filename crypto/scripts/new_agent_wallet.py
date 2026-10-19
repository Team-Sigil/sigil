"""Create the agent wallet and print its env lines (SEC-13).

    uv run python -m scripts.new_agent_wallet                    # new keypair, new master key
    uv run python -m scripts.new_agent_wallet --keypair id.json  # import a solana-keygen file

Prints AGENT_WALLET_PUBKEY, AGENT_WALLET_KEY_ENC and SIGNER_MASTER_KEY for the signer's
`.env`. The plain secret key is never written to disk. Give only AGENT_WALLET_PUBKEY to the
gateway. Then fund the wallet with a little devnet SOL for fees.
"""

import argparse
import json
from pathlib import Path

from cryptography.fernet import Fernet
from solders.keypair import Keypair

from scripts.devnet import FAUCET_URL
from signer.keys import encrypt_secret_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keypair", type=Path, help="existing solana-keygen JSON file")
    parser.add_argument("--master-key", help="existing SIGNER_MASTER_KEY (Fernet key)")
    args = parser.parse_args()

    if args.keypair:
        keypair = Keypair.from_bytes(bytes(json.loads(args.keypair.read_text())))
    else:
        keypair = Keypair()
    master_key = args.master_key or Fernet.generate_key().decode()

    print(f"AGENT_WALLET_PUBKEY={keypair.pubkey()}")
    print(f"AGENT_WALLET_KEY_ENC={encrypt_secret_key(bytes(keypair), master_key)}")
    print(f"SIGNER_MASTER_KEY={master_key}")
    print(f"\n# Fund it for fees: solana airdrop 1 {keypair.pubkey()} --url devnet")
    print(f"# or {FAUCET_URL}; scripts.setup_devnet also tries an airdrop.")


if __name__ == "__main__":
    main()
