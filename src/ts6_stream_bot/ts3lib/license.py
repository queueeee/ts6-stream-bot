"""TS3 server license chain.

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/tslib/license.ts``.

The TS3 server presents a chain of Ed25519-encoded "license blocks"
during the handshake; the client (us) walks the chain by adding
scalar-multiplied points to a known root key, and the resulting
public key is used to seed the modern ``initivexpand2`` shared secret.

We use PyNaCl for the raw Ed25519 point arithmetic - ``cryptography``
exposes Ed25519 sign/verify but not the no-clamp scalar multiplication
or point addition that the TS3 derivation requires.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from nacl.bindings import (
    crypto_core_ed25519_add,
    crypto_scalarmult_ed25519_base_noclamp,
    crypto_scalarmult_ed25519_noclamp,
)

from ts6_stream_bot.ts3lib.crypto import sha512

# Root key of the license chain - hard-coded into every TS3/TS6 client.
LICENSE_ROOT_KEY = bytes(
    [
        0xCD,
        0x0D,
        0xE2,
        0xAE,
        0xD4,
        0x63,
        0x45,
        0x50,
        0x9A,
        0x7E,
        0x3C,
        0xFD,
        0x8F,
        0x68,
        0xB3,
        0xDC,
        0x75,
        0x55,
        0xB2,
        0x9D,
        0xCC,
        0xEC,
        0x73,
        0xCD,
        0x18,
        0x75,
        0x0F,
        0x99,
        0x38,
        0x12,
        0x40,
        0x8A,
    ]
)


# Block-type IDs used by the TS6 license chain.
_BLOCK_INTERMEDIATE = 0
_BLOCK_SERVER = 2
_BLOCK_TS5_SERVER = 8
_BLOCK_EPHEMERAL = 32


@dataclass(slots=True)
class LicenseBlock:
    """One block in a license chain."""

    key: bytes  # 32-byte Ed25519 point
    hash: bytes  # first 32 bytes of SHA-512 over the block content
    type: int


# --- parsing ---------------------------------------------------------------


def _find_null_terminator(data: bytes, start: int) -> int:
    for i in range(start, len(data)):
        if data[i] == 0:
            return i - start
    raise ValueError("non-null-terminated string in license")


def parse_license(data: bytes) -> list[LicenseBlock]:
    """Parse a TS3 license blob into its constituent blocks. Validates
    version, key kind, and per-block-type extra-data length."""
    if len(data) < 1:
        raise ValueError("license too short")
    if data[0] != 1:
        raise ValueError("unsupported license version")

    blocks: list[LicenseBlock] = []
    pos = 1

    while pos < len(data):
        if len(data) - pos < 42:
            raise ValueError("license block too short")
        if data[pos] != 0:
            raise ValueError(f"wrong key kind {data[pos]}")

        key = data[pos + 1 : pos + 33]
        block_type = data[pos + 33]

        if block_type == _BLOCK_INTERMEDIATE:
            extra_len = _find_null_terminator(data, pos + 46) + 5
        elif block_type == _BLOCK_SERVER:
            extra_len = _find_null_terminator(data, pos + 47) + 6
        elif block_type == _BLOCK_TS5_SERVER:
            p = pos + 44
            prop_count = data[pos + 43]
            for _ in range(prop_count):
                prop_len = data[p]
                p += 1 + prop_len
            extra_len = p - (pos + 42)
        elif block_type == _BLOCK_EPHEMERAL:
            extra_len = 0
        else:
            raise ValueError(f"invalid license block type {block_type}")

        block_len = 42 + extra_len
        block_content = data[pos + 1 : pos + block_len]
        block_hash = sha512(block_content)[:32]
        blocks.append(LicenseBlock(key=bytes(key), hash=bytes(block_hash), type=block_type))
        pos += block_len

    return blocks


# --- chain derivation ------------------------------------------------------


def _derive_block_key(block_key: bytes, block_hash: bytes, parent_key: bytes) -> bytes:
    """Derive the next chain key. The block hash becomes a clamped Ed25519
    scalar (TS3-specific clamp: bit-clear high two bits of byte 31, set
    bit 6 of byte 31, clear low three bits of byte 0). Multiply the
    block's public point by that scalar, then add the parent key as a
    point. The result is the new parent for the next block."""
    scalar = bytearray(block_hash)
    scalar[0] &= 0xF8
    scalar[31] &= 0x3F
    scalar[31] |= 0x40

    mul = crypto_scalarmult_ed25519_noclamp(bytes(scalar), block_key)
    return bytes(crypto_core_ed25519_add(mul, parent_key))


def derive_license_key(blocks: list[LicenseBlock]) -> bytes:
    """Walk the chain, returning the final derived public key."""
    parent = LICENSE_ROOT_KEY
    for block in blocks:
        parent = _derive_block_key(block.key, block.hash, parent)
    return parent


# --- handshake helpers (modern initivexpand2) ------------------------------


def get_shared_secret2(server_derived_key: bytes, temp_private_key: bytes) -> bytes:
    """Compute the SHA-512 of (temp_priv * server_derived_key) on Ed25519.
    The high bit of the private scalar is cleared per the TS3 spec."""
    priv = bytearray(temp_private_key)
    priv[31] &= 0x7F
    mul = crypto_scalarmult_ed25519_noclamp(bytes(priv), server_derived_key)
    return sha512(bytes(mul))


@dataclass(slots=True)
class TempKeyPair:
    public_key: bytes
    private_key: bytes


def generate_temporary_key() -> TempKeyPair:
    """Random Ed25519 keypair for the modern protocol handshake. The
    private scalar is clamped to the X25519 form before scalar-mul against
    the basepoint - matches the TS reference byte-for-byte."""
    private_key = bytearray(os.urandom(32))
    private_key[0] &= 248
    private_key[31] &= 127
    private_key[31] |= 64

    public_key = bytes(crypto_scalarmult_ed25519_base_noclamp(bytes(private_key)))
    return TempKeyPair(public_key=public_key, private_key=bytes(private_key))


__all__ = [
    "LICENSE_ROOT_KEY",
    "LicenseBlock",
    "TempKeyPair",
    "derive_license_key",
    "generate_temporary_key",
    "get_shared_secret2",
    "parse_license",
]
