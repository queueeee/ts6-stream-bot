"""TS3 crypto primitive tests.

Static vectors come from a small Node script
(``/tmp/crypto-vectors/gen.mjs``) that runs the canonical ts6-manager
crypto implementation against fixed inputs. The CMAC vectors are also
cross-checked against RFC 4493 / NIST SP 800-38B fixtures so we know
the underlying primitive is correct, not just self-consistent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ts6_stream_bot.ts3lib import crypto

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "crypto_vectors.json").read_text(encoding="utf-8")
)


def test_init_constants_match() -> None:
    c = _VECTORS["init_constants"]
    assert c["MAC_LEN"] == crypto.MAC_LEN
    assert c["INIT_MAC"] == crypto.INIT_MAC.hex()
    assert c["DUMMY_KEY"] == crypto.DUMMY_KEY.hex()
    assert c["DUMMY_NONCE"] == crypto.DUMMY_NONCE.hex()
    assert c["INIT_VERSION"] == crypto.INIT_VERSION


@pytest.mark.parametrize("v", _VECTORS["hash_password"], ids=lambda v: repr(v["input"])[:30])
def test_hash_password(v: dict) -> None:
    assert crypto.hash_password(v["input"]) == v["output"]


@pytest.mark.parametrize("v", _VECTORS["sha"], ids=lambda v: repr(v["input"])[:20])
def test_sha_helpers(v: dict) -> None:
    data = v["input"].encode("utf-8")
    assert crypto.sha1(data).hex() == v["sha1"]
    assert crypto.sha256(data).hex() == v["sha256"]
    assert crypto.sha512(data).hex() == v["sha512"]


@pytest.mark.parametrize("v", _VECTORS["xor_buffers"], ids=lambda v: f"{v['a']}^{v['b']}")
def test_xor_buffers(v: dict) -> None:
    out = crypto.xor_buffers(bytes.fromhex(v["a"]), bytes.fromhex(v["b"]))
    assert out.hex() == v["out"]


def test_xor_into_in_place() -> None:
    a = bytearray(b"\xaa\xbb\xcc")
    crypto.xor_into(a, b"\xff\x00\xff", 3)
    assert bytes(a) == b"\x55\xbb\x33"


@pytest.mark.parametrize("v", _VECTORS["cmac"], ids=lambda v: f"len{len(v['data']) // 2}")
def test_cmac_vectors(v: dict) -> None:
    """CMAC against NIST SP 800-38B / RFC 4493 fixtures.
    These also match the ts6-manager output byte-for-byte."""
    out = crypto._cmac(bytes.fromhex(v["key"]), bytes.fromhex(v["data"]))
    assert out.hex() == v["mac"]


@pytest.mark.parametrize(
    "v",
    _VECTORS["derive_key_nonce"],
    ids=lambda v: f"iv{len(v['iv_struct']) // 2}_pkt{v['packet_id']}",
)
def test_derive_key_nonce(v: dict) -> None:
    r = crypto.derive_key_nonce(
        from_server=v["from_server"],
        packet_id=v["packet_id"],
        generation_id=v["generation_id"],
        packet_type=v["packet_type"],
        iv_struct=bytes.fromhex(v["iv_struct"]),
    )
    assert r.key.hex() == v["key"]
    assert r.nonce.hex() == v["nonce"]


@pytest.mark.parametrize("v", _VECTORS["eax"], ids=lambda v: f"pt{len(v['plaintext']) // 2}")
def test_eax_round_trip(v: dict) -> None:
    key = bytes.fromhex(v["key"])
    nonce = bytes.fromhex(v["nonce"])
    header = bytes.fromhex(v["header"])
    plaintext = bytes.fromhex(v["plaintext"])

    enc = crypto.eax_encrypt(key, nonce, header, plaintext)
    assert enc.ciphertext.hex() == v["ciphertext"]
    assert enc.mac.hex() == v["mac"]

    out = crypto.eax_decrypt(key, nonce, header, enc.ciphertext, enc.mac)
    assert out == plaintext


def test_eax_decrypt_rejects_tampered_mac() -> None:
    v = _VECTORS["eax_bad_mac"]
    out = crypto.eax_decrypt(
        bytes.fromhex(v["key"]),
        bytes.fromhex(v["nonce"]),
        bytes.fromhex(v["header"]),
        bytes.fromhex(v["ciphertext"]),
        bytes.fromhex(v["mac"]),
    )
    assert out is None


def test_ecdsa_round_trip() -> None:
    """Generate a fresh P-256 key, sign+verify - ECDSA signatures are
    randomized so we can't pin them; round-trip is the right check."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private = ec.generate_private_key(ec.SECP256R1())
    priv_der = private.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )
    pub_der = private.public_key().public_bytes(
        encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo
    )

    msg = b"the bot connects"
    sig = crypto.ecdsa_sign(priv_der, msg)
    assert crypto.ecdsa_verify(pub_der, msg, sig) is True
    # Tampered message must not verify.
    assert crypto.ecdsa_verify(pub_der, msg + b"x", sig) is False
    # Tampered signature must not verify.
    bad = bytearray(sig)
    bad[-1] ^= 0xFF
    assert crypto.ecdsa_verify(pub_der, msg, bytes(bad)) is False
