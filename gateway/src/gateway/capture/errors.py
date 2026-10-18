class AttestationError(Exception):
    """A device attestation or capture signature did not verify. The detail is for logs; the
    device or capture is simply treated as unverified."""
