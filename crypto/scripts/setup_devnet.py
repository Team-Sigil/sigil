"""Set up the demo on devnet (SPEC §3, handoff §7). Safe to re-run: it reuses `.devnet/`.

    AGENT_WALLET_PUBKEY=<pubkey> uv run python -m scripts.setup_devnet
    ... --guardian <Phantom wallet>   # use a real Phantom wallet as the guardian
    ... --dependents 2 --fund 1000000 --delegate 500000

1. a payer keypair (mint authority and fee payer for setup), funded by airdrop;
2. the aUSD mint, 6 decimals (AUSD_MINT);
3. the guardian's aUSD token account (the reservoir), minted `--fund` aUSD;
4. one aUSD token account per dependent;
5. the delegation: the guardian approves the agent wallet for `--delegate` aUSD. Done here
   with the demo guardian key; with `--guardian` (Phantom) the app does this step instead.

Writes the addresses to `.devnet/seed.json` and prints what the signer and gateway need.
"""

import argparse
import asyncio
import json
import os
import sys

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, create_account
from spl.token.constants import MINT_LEN, TOKEN_PROGRAM_ID
from spl.token.instructions import (
    approve_checked,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    initialize_mint,
    mint_to_checked,
)
from spl.token.models import ApproveCheckedParams, InitializeMintParams, MintToCheckedParams

from scripts.devnet import (
    FAUCET_URL,
    SEED_FILE,
    DevnetRpc,
    explorer,
    load_or_create_keypair,
    rpc_url,
)
from signer.chain import AUSD_DECIMALS, ChainUnavailableError
from signer.config import AUSD

LAMPORTS_PER_SOL = 1_000_000_000
MIN_PAYER_LAMPORTS = LAMPORTS_PER_SOL // 2
MIN_AGENT_LAMPORTS = LAMPORTS_PER_SOL // 10


async def ensure_sol(rpc: DevnetRpc, pubkey: Pubkey, minimum: int, label: str) -> None:
    balance = await rpc.get_balance(pubkey)
    if balance >= minimum:
        return
    print(f"Airdropping 1 SOL to the {label} {pubkey} ...")
    try:
        signature = await rpc.request_airdrop(pubkey, LAMPORTS_PER_SOL)
        await rpc.wait_for(signature)
    except ChainUnavailableError:
        sys.exit(
            f"Airdrop refused (devnet rate limit). Fund {pubkey} at {FAUCET_URL} "
            "or with `solana airdrop 1 <pubkey> --url devnet`, then re-run."
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--agent", default=os.environ.get("AGENT_WALLET_PUBKEY"))
    parser.add_argument("--guardian", help="a Phantom wallet to use as the guardian")
    parser.add_argument("--dependents", type=int, default=2)
    # Baht-sized amounts: a demo reservoir of 1,000,000, half of it approved to the agent.
    parser.add_argument("--fund", type=int, default=1_000_000, help="aUSD minted to the guardian")
    parser.add_argument("--delegate", type=int, default=500_000, help="aUSD approved to the agent")
    args = parser.parse_args()
    if not args.agent:
        sys.exit("Set AGENT_WALLET_PUBKEY or pass --agent (see scripts.new_agent_wallet)")
    agent = Pubkey.from_string(args.agent)

    rpc = DevnetRpc(rpc_url(), timeout=30)
    try:
        payer = load_or_create_keypair("payer")
        await ensure_sol(rpc, payer.pubkey(), MIN_PAYER_LAMPORTS, "setup payer")
        await ensure_sol(rpc, agent, MIN_AGENT_LAMPORTS, "agent wallet")

        mint_keypair = load_or_create_keypair("ausd-mint")
        mint = mint_keypair.pubkey()
        if not await rpc.account_exists(mint):
            rent = await rpc.get_minimum_balance_for_rent_exemption(MINT_LEN)
            signature = await rpc.send_and_confirm(
                [
                    create_account(
                        CreateAccountParams(
                            from_pubkey=payer.pubkey(),
                            to_pubkey=mint,
                            lamports=rent,
                            space=MINT_LEN,
                            owner=TOKEN_PROGRAM_ID,
                        )
                    ),
                    initialize_mint(
                        InitializeMintParams(
                            decimals=AUSD_DECIMALS,
                            program_id=TOKEN_PROGRAM_ID,
                            mint=mint,
                            mint_authority=payer.pubkey(),
                        )
                    ),
                ],
                payer,
                [payer, mint_keypair],
            )
            print(f"Created aUSD mint {mint}: {explorer(signature)}")

        guardian_keypair: Keypair | None = None
        if args.guardian:
            guardian = Pubkey.from_string(args.guardian)
        else:
            guardian_keypair = load_or_create_keypair("guardian")
            guardian = guardian_keypair.pubkey()
        dependents = [
            load_or_create_keypair(f"dependent-{i + 1}").pubkey() for i in range(args.dependents)
        ]

        owners = [guardian, *dependents]
        await rpc.send_and_confirm(
            [create_idempotent_associated_token_account(payer.pubkey(), o, mint) for o in owners],
            payer,
            [payer],
        )
        reservoir = get_associated_token_address(guardian, mint)
        dependent_accounts = [get_associated_token_address(d, mint) for d in dependents]

        missing = args.fund * AUSD - await rpc.get_token_balance(reservoir)
        if missing > 0:
            await rpc.send_and_confirm(
                [
                    mint_to_checked(
                        MintToCheckedParams(
                            program_id=TOKEN_PROGRAM_ID,
                            mint=mint,
                            dest=reservoir,
                            mint_authority=payer.pubkey(),
                            amount=missing,
                            decimals=AUSD_DECIMALS,
                        )
                    )
                ],
                payer,
                [payer],
            )
            print(f"Minted {missing / AUSD:,.2f} aUSD to the reservoir {reservoir}")

        if guardian_keypair is not None:
            signature = await rpc.send_and_confirm(
                [
                    approve_checked(
                        ApproveCheckedParams(
                            program_id=TOKEN_PROGRAM_ID,
                            source=reservoir,
                            mint=mint,
                            delegate=agent,
                            owner=guardian,
                            amount=args.delegate * AUSD,
                            decimals=AUSD_DECIMALS,
                        )
                    )
                ],
                payer,
                [payer, guardian_keypair],
            )
            print(f"Approved the agent for {args.delegate} aUSD: {explorer(signature)}")
        else:
            print("Guardian is a Phantom wallet: approve the agent wallet from the app.")

        seed = {
            "ausd_mint": str(mint),
            "agent_wallet": str(agent),
            "guardian_wallet": str(guardian),
            "guardian_token_account": str(reservoir),
            "dependents": [
                {"wallet": str(w), "token_account": str(a)}
                for w, a in zip(dependents, dependent_accounts, strict=True)
            ],
        }
        SEED_FILE.write_text(json.dumps(seed, indent=2))
        delegation = await rpc.get_token_account(str(reservoir))
    finally:
        await rpc.aclose()

    print(f"\nWrote {SEED_FILE}:\n{json.dumps(seed, indent=2)}")
    if delegation is not None:
        print(
            f"\nReservoir delegate: {delegation.delegate}, "
            f"delegated {delegation.delegated_amount_units / AUSD:,.2f} aUSD"
        )
    print(f"\nFor both .env files:\nAUSD_MINT={mint}\nAGENT_WALLET_PUBKEY={agent}")


if __name__ == "__main__":
    asyncio.run(main())
