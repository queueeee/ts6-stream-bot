"""TS3 identity tests.

Vectors come from a Node script (``/tmp/identity-vectors/gen.mjs``) that
inlines the ts6-manager identity implementation; the values it emits are
exactly what the canonical TS code produces, so byte-equality with our
Python port means we're protocol-compatible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ts6_stream_bot.ts3lib import identity
from ts6_stream_bot.ts3lib.identity import (
    OBFUSCATION_KEY,
    Identity,
    _bigint_to_buf32,
    _build_ltc_public_key_der,
    _parse_der_sequence,
    count_leading_zero_bits,
    export_public_key_string,
    from_base64_key,
    generate_identity,
    get_shared_secret,
    restore_identity,
)

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "identity_vectors.json").read_text(encoding="utf-8")
)


def test_obfuscation_key_matches() -> None:
    assert _VECTORS["obfuscation_key"] == OBFUSCATION_KEY.hex()


@pytest.mark.parametrize(
    "v", _VECTORS["count_leading_zero_bits"], ids=lambda v: v["input"] or "empty"
)
def test_count_leading_zero_bits(v: dict) -> None:
    assert v["count"] == count_leading_zero_bits(bytes.fromhex(v["input"]))


@pytest.mark.parametrize("v", _VECTORS["bigint_to_buf32"], ids=lambda v: v["value"][:8])
def test_bigint_to_buf32(v: dict) -> None:
    assert v["hex"] == _bigint_to_buf32(int(v["value"])).hex()


@pytest.mark.parametrize("v", _VECTORS["parse_build_der"])
def test_der_round_trip(v: dict) -> None:
    """Build a libtomcrypt-style DER from known x/y, parse it back, check
    byte-identity with the Node-generated DER and the parsed integers."""
    der = _build_ltc_public_key_der(int(v["x"]), int(v["y"]))
    assert v["der"] == der.hex()
    parsed = _parse_der_sequence(der)
    assert parsed[0] == v["parsed_bit_info"]
    assert parsed[1] == int(v["parsed_int32"])
    assert parsed[2] == int(v["parsed_x"])
    assert parsed[3] == int(v["parsed_y"])


@pytest.mark.parametrize("v", _VECTORS["identity_round_trip"])
def test_identity_round_trip_serialization(v: dict) -> None:
    """Restore from the serialized form and verify the public-key string +
    UID are byte-identical to what Node produced."""
    ident = restore_identity(v)
    assert v["publicKeyString"] == ident.public_key_string
    assert v["uid"] == ident.uid
    assert ident.private_scalar == int(v["privateKeyBigInt"])
    assert ident.key_offset == int(v["keyOffset"])
    # Re-export the public key from the (lazily reconstructed) crypto object;
    # it must produce the same libtomcrypt string we started with.
    assert v["publicKeyString"] == export_public_key_string(ident.public_key)


@pytest.mark.parametrize("v", _VECTORS["identity_round_trip"])
def test_from_base64_key_with_priv(v: dict) -> None:
    """Importing the libtomcrypt 0x80 form (public + private embedded) must
    yield the same identity as the JSON round-trip."""
    ident = from_base64_key(v["base64_key_with_priv"])
    assert ident.private_scalar == int(v["privateKeyBigInt"])
    assert v["publicKeyString"] == ident.public_key_string
    assert v["uid"] == ident.uid


def test_generate_identity_meets_security_level() -> None:
    """Run the actual mining and verify the achieved level >= target.
    Level 8 averages ~256 hashes - milliseconds even in pure Python."""
    ident = generate_identity(security_level=8)
    pub_bytes = ident.public_key_string.encode("ascii")
    from ts6_stream_bot.ts3lib.crypto import sha1

    h = sha1(pub_bytes + str(ident.key_offset).encode("ascii"))
    assert count_leading_zero_bits(h) >= 8


def test_generate_identity_zero_security_level_skips_mining() -> None:
    ident = generate_identity(security_level=0)
    assert ident.key_offset == 0


@pytest.mark.parametrize("v", _VECTORS["improve_security"])
def test_improve_security_reaches_target(v: dict) -> None:
    """Replay the mining: same public key, mine to the same target, and
    confirm the achieved level meets it. We can't expect the SAME offset
    as Node (multiple offsets reach a given level) but the achieved level
    must be >= target.

    NOTE: the Node vector mined to level 12; doing the same Python-side
    averages ~4096 sha1s, still trivially fast.
    """
    pub_bytes = v["publicKeyString"].encode("ascii")
    from ts6_stream_bot.ts3lib.crypto import sha1

    # Mine with the Python implementation starting from offset 0.
    ident = Identity(
        private_scalar=1,  # synthetic; we only mine over the pub-key string
        public_key_string=v["publicKeyString"],
        key_offset=0,
        uid="ignored",
    )
    identity._improve_security(ident, v["target"])
    h = sha1(pub_bytes + str(ident.key_offset).encode("ascii"))
    assert count_leading_zero_bits(h) >= v["target"]


@pytest.mark.parametrize("v", _VECTORS["shared_secret"])
def test_get_shared_secret_matches_node(v: dict) -> None:
    """Alice (private scalar from Node) ECDHs with Bob's libtomcrypt-DER
    public key; the SHA-1 of the x coordinate must match what Node got
    on the other side."""
    from cryptography.hazmat.primitives.asymmetric import ec

    alice_priv = ec.derive_private_key(int(v["alice_private"]), ec.SECP256R1())
    import base64

    bob_pub_der = base64.b64decode(v["bob_public_libtomcrypt_b64"])
    secret = get_shared_secret(alice_priv, bob_pub_der)
    assert v["expected_secret_hex"] == secret.hex()


@pytest.mark.asyncio
async def test_generate_identity_async_returns_valid_identity() -> None:
    """``generate_identity_async`` runs ``generate_identity`` on a worker
    thread; here we just verify the result is well-formed. (Trying to
    prove "the loop wasn't blocked" with timing is flaky - hashlib's
    GIL release means short levels finish faster than a 1ms sleep tick.)
    """
    ident = await identity.generate_identity_async(security_level=8)
    assert ident.key_offset >= 0
    assert len(ident.public_key_string) > 0
    # Round-trip via the serialized form.
    restored = restore_identity(ident.to_dict())
    assert ident.uid == restored.uid
    assert ident.public_key_string == restored.public_key_string
