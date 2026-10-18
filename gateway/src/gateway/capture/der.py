"""A minimal DER reader for the two ASN.1 structures attestation needs: Apple's App Attest
nonce extension and Android's KeyDescription. Strict: anything malformed raises `DerError`.
"""

from dataclasses import dataclass

UNIVERSAL, APPLICATION, CONTEXT, PRIVATE = 0, 1, 2, 3
INTEGER, OCTET_STRING, ENUMERATED, BOOLEAN, SEQUENCE, SET = 2, 4, 10, 1, 16, 17


class DerError(ValueError):
    pass


@dataclass(frozen=True)
class Tlv:
    cls: int
    constructed: bool
    number: int
    value: bytes

    def children(self) -> list["Tlv"]:
        if not self.constructed:
            raise DerError("not a constructed value")
        return read_all(self.value)

    def expect(self, cls: int, number: int) -> "Tlv":
        if (self.cls, self.number) != (cls, number):
            raise DerError(f"expected tag {cls}:{number}, got {self.cls}:{self.number}")
        return self

    def as_int(self) -> int:
        if self.constructed or not self.value:
            raise DerError("bad integer")
        return int.from_bytes(self.value, "big", signed=True)

    def as_bool(self) -> bool:
        if self.constructed or len(self.value) != 1:
            raise DerError("bad boolean")
        return self.value != b"\x00"


def read_one(data: bytes, offset: int = 0) -> tuple[Tlv, int]:
    """Reads one TLV at `offset`; returns it and the offset after it."""
    try:
        first = data[offset]
        offset += 1
        cls, constructed, number = first >> 6, bool(first & 0x20), first & 0x1F
        if number == 0x1F:  # high tag number form (Android uses tags like 704 and 709)
            number = 0
            while True:
                byte = data[offset]
                offset += 1
                number = (number << 7) | (byte & 0x7F)
                if not byte & 0x80:
                    break
        length = data[offset]
        offset += 1
        if length & 0x80:
            count = length & 0x7F
            if count == 0 or count > 4:
                raise DerError("unsupported length")
            length = int.from_bytes(data[offset : offset + count], "big")
            offset += count
    except IndexError as exc:
        raise DerError("truncated") from exc
    end = offset + length
    if end > len(data):
        raise DerError("truncated value")
    return Tlv(cls, constructed, number, data[offset:end]), end


def read_all(data: bytes) -> list[Tlv]:
    items, offset = [], 0
    while offset < len(data):
        item, offset = read_one(data, offset)
        items.append(item)
    return items


def parse(data: bytes) -> Tlv:
    """Exactly one TLV, with nothing after it."""
    item, end = read_one(data)
    if end != len(data):
        raise DerError("trailing data")
    return item
