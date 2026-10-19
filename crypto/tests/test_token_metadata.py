import struct

from solders.keypair import Keypair

from scripts.token_metadata import (
    METADATA_PROGRAM_ID,
    create_metadata,
    metadata_address,
    parse_name_and_symbol,
    update_metadata,
)

MINT = Keypair().pubkey()
AUTHORITY = Keypair().pubkey()


def borsh_string(value: str) -> bytes:
    return struct.pack("<I", len(value.encode())) + value.encode()


DATA_V2 = borsh_string("Sigil aUSD") + borsh_string("aUSD") + borsh_string("") + b"\0\0" + b"\0\0\0"


def test_metadata_address_is_the_program_pda() -> None:
    address = metadata_address(MINT)
    assert address.is_on_curve() is False
    assert address == metadata_address(MINT)


def test_create_instruction_layout() -> None:
    ix = create_metadata(MINT, AUTHORITY, "Sigil aUSD", "aUSD", "")
    assert ix.program_id == METADATA_PROGRAM_ID
    assert bytes(ix.data) == bytes([33]) + DATA_V2 + b"\1" + b"\0"
    metas = [(m.pubkey, m.is_signer, m.is_writable) for m in ix.accounts]
    assert metas[:5] == [
        (metadata_address(MINT), False, True),
        (MINT, False, False),
        (AUTHORITY, True, False),  # mint authority
        (AUTHORITY, True, True),  # payer
        (AUTHORITY, True, False),  # update authority
    ]
    assert len(metas) == 7  # + system program, rent


def test_update_instruction_layout() -> None:
    ix = update_metadata(MINT, AUTHORITY, "Sigil aUSD", "aUSD", "")
    assert bytes(ix.data) == bytes([15]) + b"\1" + DATA_V2 + b"\0" + b"\0" + b"\1\1"
    assert [(m.pubkey, m.is_signer, m.is_writable) for m in ix.accounts] == [
        (metadata_address(MINT), False, True),
        (AUTHORITY, True, False),
    ]


def test_parse_name_and_symbol_strips_padding() -> None:
    # On chain the program pads name to 32 bytes and symbol to 10 with NULs.
    account = (
        b"\4"
        + bytes(AUTHORITY)
        + bytes(MINT)
        + borsh_string("Sigil aUSD".ljust(32, "\0"))
        + borsh_string("aUSD".ljust(10, "\0"))
        + borsh_string("")
    )
    assert parse_name_and_symbol(account) == ("Sigil aUSD", "aUSD")
