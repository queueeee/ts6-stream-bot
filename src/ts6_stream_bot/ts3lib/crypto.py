"""TS3 voice protocol crypto primitives.

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/tslib/crypto.ts``.

What's in here:

* AES-CMAC (OMAC1) - via the ``cryptography`` library, which implements the
  same NIST SP 800-38B construction.
* AES-128-EAX authenticated encryption - the construction is built on top
  of CMAC + AES-CTR; we port it directly because Python's standard libs
  don't ship an EAX wrapper.
* SHA-1 / SHA-256 / SHA-512 hash helpers (just thin wrappers over hashlib).
* ECDSA P-256 / SHA-256 sign + verify against SEC1-DER private keys and
  SPKI-DER public keys.
* TS3 per-packet key/nonce derivation (``derive_key_nonce``).
* Constants: ``MAC_LEN``, ``INIT_MAC``, ``DUMMY_KEY``, ``DUMMY_NONCE``,
  ``INIT_VERSION``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

# --- constants -------------------------------------------------------------

MAC_LEN = 8
INIT_MAC = b"TS3INIT1"
# "c:\\windows\\system\\firewall32.cpl" split into key(16) + nonce(16). These
# are the well-known TS3 init-packet placeholders the protocol uses before
# the real session keys are negotiated.
DUMMY_KEY = b"c:\\windows\\syste"
DUMMY_NONCE = b"m\\firewall32.cpl"
INIT_VERSION = 1566914096  # 3.5.0 [Stable]

_BLOCK_SIZE = 16


# --- block primitives ------------------------------------------------------


def _aes_encrypt_block(key: bytes, block: bytes) -> bytes:
    """AES-128-ECB single-block encrypt. Used internally by EAX."""
    if len(key) != 16 or len(block) != _BLOCK_SIZE:
        raise ValueError("AES-128 needs a 16-byte key and a 16-byte block")
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block) + enc.finalize()


def _cmac(key: bytes, data: bytes) -> bytes:
    """AES-CMAC (OMAC1). 16-byte key, 16-byte tag."""
    c = CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def _eax_omac(key: bytes, tag: int, data: bytes) -> bytes:
    """EAX's tag-prefixed OMAC: 16-byte block of zeros with ``tag`` in the
    last byte, prepended to the data, then CMAC'd."""
    tag_block = bytes(_BLOCK_SIZE - 1) + bytes([tag])
    return _cmac(key, tag_block + data)


# --- EAX -------------------------------------------------------------------


@dataclass(slots=True)
class EaxResult:
    ciphertext: bytes
    mac: bytes


def eax_encrypt(
    key: bytes,
    nonce: bytes,
    header: bytes,
    plaintext: bytes,
    mac_len: int = MAC_LEN,
) -> EaxResult:
    """AES-128-EAX encrypt. ``mac_len`` defaults to TS3's truncated 8 bytes."""
    n = _eax_omac(key, 0, nonce)
    h = _eax_omac(key, 1, header)
    enc = Cipher(algorithms.AES(key), modes.CTR(n)).encryptor()
    ciphertext = enc.update(plaintext) + enc.finalize()
    c = _eax_omac(key, 2, ciphertext)
    tag = xor_buffers(xor_buffers(n, h), c)
    return EaxResult(ciphertext=ciphertext, mac=tag[:mac_len])


def eax_decrypt(
    key: bytes,
    nonce: bytes,
    header: bytes,
    ciphertext: bytes,
    mac: bytes,
    mac_len: int = MAC_LEN,
) -> bytes | None:
    """AES-128-EAX decrypt. Returns plaintext on success, or ``None`` if the
    MAC fails (matches the TS reference, which lets callers branch on null)."""
    n = _eax_omac(key, 0, nonce)
    h = _eax_omac(key, 1, header)
    c = _eax_omac(key, 2, ciphertext)
    tag = xor_buffers(xor_buffers(n, h), c)
    if not hmac.compare_digest(tag[:mac_len], mac[:mac_len]):
        return None
    dec = Cipher(algorithms.AES(key), modes.CTR(n)).decryptor()
    return dec.update(ciphertext) + dec.finalize()


# --- hashes ----------------------------------------------------------------


def sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


# --- byte XOR --------------------------------------------------------------


def xor_buffers(a: bytes, b: bytes) -> bytes:
    """XOR ``a`` and ``b``, truncating to the shorter length."""
    n = min(len(a), len(b))
    return bytes(a[i] ^ b[i] for i in range(n))


def xor_into(a: bytearray, b: bytes, length: int) -> None:
    """XOR the first ``length`` bytes of ``b`` into ``a`` in place."""
    for i in range(length):
        a[i] ^= b[i]


# --- TS3 helpers -----------------------------------------------------------


def hash_password(password: str) -> str:
    """TS3 password hash: SHA-1(utf-8 password), then base64."""
    if not password:
        return ""
    return base64.b64encode(sha1(password.encode("utf-8"))).decode("ascii")


@dataclass(slots=True)
class KeyNonce:
    key: bytes
    nonce: bytes


def derive_key_nonce(
    *,
    from_server: bool,
    packet_id: int,
    generation_id: int,
    packet_type: int,
    iv_struct: bytes,
) -> KeyNonce:
    """TS3 per-packet key + nonce derivation.

    The legacy 20-byte ``iv_struct`` and the 64-byte modern one are both
    accepted; the buffer layout matches the TS reference exactly so we
    interoperate with the upstream implementation byte-for-byte.
    """
    tmp_len = 26 if len(iv_struct) == 20 else 70
    tmp = bytearray(tmp_len)
    tmp[0] = 0x30 if from_server else 0x31
    tmp[1] = packet_type
    tmp[2:6] = generation_id.to_bytes(4, "big", signed=False)
    tmp[6 : 6 + len(iv_struct)] = iv_struct

    keynonce = sha256(bytes(tmp))
    key = bytearray(keynonce[:16])
    nonce = bytes(keynonce[16:32])
    key[0] ^= (packet_id >> 8) & 0xFF
    key[1] ^= packet_id & 0xFF
    return KeyNonce(key=bytes(key), nonce=nonce)


# --- ECDSA P-256 / SHA-256 -------------------------------------------------


def ecdsa_sign(private_key_der: bytes, data: bytes) -> bytes:
    """ECDSA P-256 / SHA-256 sign with a SEC1-encoded private key."""
    key = serialization.load_der_private_key(private_key_der, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("not an EC private key")
    return key.sign(data, ec.ECDSA(hashes.SHA256()))


def ecdsa_verify(public_key_der: bytes, data: bytes, signature: bytes) -> bool:
    """ECDSA P-256 / SHA-256 verify with an SPKI-encoded public key.
    Returns False on any verification failure rather than raising."""
    from cryptography.exceptions import InvalidSignature

    key = serialization.load_der_public_key(public_key_der)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("not an EC public key")
    try:
        key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


__all__ = [
    "DUMMY_KEY",
    "DUMMY_NONCE",
    "INIT_MAC",
    "INIT_VERSION",
    "MAC_LEN",
    "EaxResult",
    "KeyNonce",
    "derive_key_nonce",
    "eax_decrypt",
    "eax_encrypt",
    "ecdsa_sign",
    "ecdsa_verify",
    "hash_password",
    "sha1",
    "sha256",
    "sha512",
    "xor_buffers",
    "xor_into",
]
