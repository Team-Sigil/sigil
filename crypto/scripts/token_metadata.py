"""Give the aUSD mint a name and symbol, so wallets show "aUSD" instead of "Unknown Token".

    uv run python -m scripts.token_metadata show
    uv run python -m scripts.token_metadata set                  # name "Sigil aUSD", symbol "aUSD"
    uv run python -m scripts.token_metadata set --name "..." --symbol "..." --uri "https://..."

Writes a Metaplex Token Metadata account for the mint, signed by the demo payer from
`scripts.setup_devnet` (the mint authority). The first `set` creates it; later runs update
it. Display only: the signer and the gateway identify aUSD by its mint address, never by
its name.
"""

import argparse
import asyncio
import struct
import sys

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.sysvar import RENT

from scripts.devnet import DevnetRpc, explorer, load_keypair, load_seed, rpc_url

METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
CREATE_METADATA_ACCOUNT_V3 = 33
UPDATE_METADATA_ACCOUNT_V2 = 15
MAX_NAME, MAX_SYMBOL, MAX_URI = 32, 10, 200


def metadata_address(mint: Pubkey) -> Pubkey:
    seeds = [b"metadata", bytes(METADATA_PROGRAM_ID), bytes(mint)]
    return Pubkey.find_program_address(seeds, METADATA_PROGRAM_ID)[0]


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


def _data_v2(name: str, symbol: str, uri: str) -> bytes:
    # DataV2: name, symbol, uri, seller fee 0, no creators, no collection, no uses.
    return _string(name) + _string(symbol) + _string(uri) + struct.pack("<H", 0) + b"\0\0\0"


def create_metadata(
    mint: Pubkey, authority: Pubkey, name: str, symbol: str, uri: str
) -> Instruction:
    """CreateMetadataAccountV3, with `authority` as mint authority, payer and update authority."""
    data = (
        bytes([CREATE_METADATA_ACCOUNT_V3])
        + _data_v2(name, symbol, uri)
        + b"\1"  # is_mutable
        + b"\0"  # no collection details
    )
    accounts = [
        AccountMeta(metadata_address(mint), is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(authority, is_signer=True, is_writable=False),
        AccountMeta(authority, is_signer=True, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(RENT, is_signer=False, is_writable=False),
    ]
    return Instruction(METADATA_PROGRAM_ID, data, accounts)


def update_metadata(
    mint: Pubkey, authority: Pubkey, name: str, symbol: str, uri: str
) -> Instruction:
    """UpdateMetadataAccountV2: new data; update authority and primary sale unchanged."""
    data = (
        bytes([UPDATE_METADATA_ACCOUNT_V2])
        + b"\1"
        + _data_v2(name, symbol, uri)
        + b"\0"  # keep the update authority
        + b"\0"  # keep primary_sale_happened
        + b"\1\1"  # is_mutable: Some(true)
    )
    accounts = [
        AccountMeta(metadata_address(mint), is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
    ]
    return Instruction(METADATA_PROGRAM_ID, data, accounts)


def parse_name_and_symbol(account_data: bytes) -> tuple[str, str]:
    """Name and symbol from a Metadata account (key, update authority, mint, then the data)."""
    offset = 1 + 32 + 32
    fields = []
    for _ in range(2):
        (length,) = struct.unpack_from("<I", account_data, offset)
        offset += 4
        fields.append(account_data[offset : offset + length].decode().rstrip("\0"))
        offset += length
    return fields[0], fields[1]


async def read_metadata(rpc: DevnetRpc, mint: Pubkey) -> tuple[str, str] | None:
    data = await rpc.get_account_data(metadata_address(mint))
    return None if data is None else parse_name_and_symbol(data)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    set_cmd = commands.add_parser("set")
    set_cmd.add_argument("--name", default="Sigil aUSD")
    set_cmd.add_argument("--symbol", default="aUSD")
    set_cmd.add_argument("--uri", default="", help="JSON metadata URL (for a logo); optional")
    args = parser.parse_args()

    seed = load_seed()
    mint = Pubkey.from_string(seed["ausd_mint"])
    rpc = DevnetRpc(rpc_url(), timeout=30)
    try:
        current = await read_metadata(rpc, mint)
        if args.command == "set":
            if len(args.name.encode()) > MAX_NAME or len(args.symbol.encode()) > MAX_SYMBOL:
                sys.exit(f"name ≤ {MAX_NAME} bytes and symbol ≤ {MAX_SYMBOL} bytes")
            if len(args.uri.encode()) > MAX_URI:
                sys.exit(f"uri ≤ {MAX_URI} bytes")
            payer = load_keypair("payer")
            build = create_metadata if current is None else update_metadata
            instruction = build(mint, payer.pubkey(), args.name, args.symbol, args.uri)
            signature = await rpc.send_and_confirm([instruction], payer, [payer])
            print(f"{'created' if current is None else 'updated'}: {explorer(signature)}")
            current = await read_metadata(rpc, mint)
    finally:
        await rpc.aclose()

    print(f"Mint {mint}")
    print(f"  metadata {metadata_address(mint)}")
    if current is None:
        print("  none yet: wallets show it as an unknown token")
    else:
        print(f"  name     {current[0]}")
        print(f"  symbol   {current[1]}")


if __name__ == "__main__":
    asyncio.run(main())
