"""Show, approve or revoke the demo guardian's delegation to the agent wallet.

    uv run python -m scripts.delegation show
    uv run python -m scripts.delegation approve 500   # aUSD
    uv run python -m scripts.delegation revoke        # attack demo 4

Attack demo 4: revoke mid-demo, and the next transfer fails with `INSUFFICIENT_DELEGATION`
(the signer reads the delegation fresh before every transfer, and the token program would
refuse anyway). Uses the demo guardian key from `scripts.setup_devnet`; with a Phantom
guardian, revoke from Phantom instead.
"""

import argparse
import asyncio
import sys

from solders.pubkey import Pubkey
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import approve_checked, revoke
from spl.token.models import ApproveCheckedParams, RevokeParams

from scripts.devnet import DevnetRpc, explorer, load_keypair, load_seed, rpc_url
from signer.chain import AUSD_DECIMALS
from signer.config import AUSD


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    approve_cmd = commands.add_parser("approve")
    approve_cmd.add_argument("amount", type=int, help="aUSD")
    commands.add_parser("revoke")
    args = parser.parse_args()

    seed = load_seed()
    reservoir = Pubkey.from_string(seed["guardian_token_account"])
    rpc = DevnetRpc(rpc_url(), timeout=30)
    try:
        if args.command != "show":
            guardian = load_keypair("guardian")
            if str(guardian.pubkey()) != seed["guardian_wallet"]:
                sys.exit("The guardian is a Phantom wallet: change the delegation in Phantom.")
            payer = load_keypair("payer")
            if args.command == "approve":
                instruction = approve_checked(
                    ApproveCheckedParams(
                        program_id=TOKEN_PROGRAM_ID,
                        source=reservoir,
                        mint=Pubkey.from_string(seed["ausd_mint"]),
                        delegate=Pubkey.from_string(seed["agent_wallet"]),
                        owner=guardian.pubkey(),
                        amount=args.amount * AUSD,
                        decimals=AUSD_DECIMALS,
                    )
                )
            else:
                instruction = revoke(
                    RevokeParams(
                        program_id=TOKEN_PROGRAM_ID, account=reservoir, owner=guardian.pubkey()
                    )
                )
            signature = await rpc.send_and_confirm([instruction], payer, [payer, guardian])
            print(f"{args.command}: {explorer(signature)}")

        account = await rpc.get_token_account(str(reservoir))
        balance = await rpc.get_token_balance(reservoir)
    finally:
        await rpc.aclose()

    if account is None:
        sys.exit(f"{reservoir} is not a token account")
    print(f"Reservoir {reservoir} (owner {account.owner})")
    print(f"  balance   {balance / AUSD:,.2f} aUSD")
    print(f"  delegate  {account.delegate}")
    print(f"  delegated {account.delegated_amount_units / AUSD:,.2f} aUSD")


if __name__ == "__main__":
    asyncio.run(main())
