"""TS3 client identity: P-256 ECDSA keypair + hashcash security level.

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/tslib/identity.ts`` and the worker file
``identity-worker.ts``.

What's in here:

* ``Identity`` - canonical state: private scalar, public-key string in
  libtomcrypt format (base64-encoded DER), the hashcash key offset, and
  the derived UID. Crypto key objects are reconstructed lazily.
* ``generate_identity`` / ``generate_identity_async`` - fresh P-256
  keypair plus optional hashcash mining to a target security level. The
  async variant runs the mining in a worker thread so the asyncio loop
  isn't blocked.
* ``from_ts_identity`` - import a TS3 client identity export string
  (``"<offset>V<base64data>"``), with the standard XOR deobfuscation.
* ``from_base64_key`` / ``restore_identity`` - other entry points.
* ``export_public_key_string`` - render a public key into the
  libtomcrypt ASN.1 base64 form the TS3 protocol uses (``omega``).
* ``get_shared_secret`` - ECDH P-256 with the server's public key,
  returning the SHA-1 hash of the x coordinate (the format TS3 uses
  to seed its session keys).

Hashcash level guidance: TS6 typically wants level >= 8 (cheap, ~msec).
Levels >= 20 take seconds; >= 24 takes minutes. Threading lifts this
off the event loop but doesn't make a single search faster.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric import ec

from ts6_stream_bot.ts3lib.crypto import sha1, xor_into

# 64-byte XOR mask used by the TS3 client for identity-export obfuscation.
OBFUSCATION_KEY = bytes.fromhex(
    "b9dfaa7bee6ac57ac7b65f1094a1c155"
    "e747327bc2fe5d51c512023fe54a2802"
    "01004e90ad1daaae1075d53b7d571c30"
    "e063b5a62a4a017bb394833aa0983e6e"
)

_CURVE = ec.SECP256R1()


# --- helpers ---------------------------------------------------------------


def _bigint_to_buf32(n: int) -> bytes:
    """Convert ``n`` to 32 bytes big-endian, mirroring TS bigintToBuffer32:
    take absolute value, pad on the left to 32 bytes, truncate from the
    high end if it's longer than 32."""
    n = abs(n) & ((1 << 256) - 1)
    return n.to_bytes(32, "big")


def _buf_to_bigint(buf: bytes) -> int:
    if not buf:
        return 0
    return int.from_bytes(buf, "big", signed=False)


def count_leading_zero_bits(data: bytes) -> int:
    """TS3-protocol "leading zero bits": traverses the buffer byte by byte
    and counts trailing-zero bits within each byte (LSB first). Matches the
    TS reference exactly - this is not the conventional MSB count.
    """
    count = 0
    for byte in data:
        if byte == 0:
            count += 8
            continue
        for bit in range(8):
            if byte & (1 << bit):
                return count
            count += 1
    return count


# --- libtomcrypt-style DER -------------------------------------------------


def _read_der_length(data: bytes, pos: int) -> tuple[int, int]:
    if data[pos] < 0x80:
        return data[pos], 1
    num_bytes = data[pos] & 0x7F
    length = 0
    for i in range(num_bytes):
        length = (length << 8) | data[pos + 1 + i]
    return length, 1 + num_bytes


def _parse_der_sequence(data: bytes) -> list[int]:
    """Tiny DER parser specialized for libtomcrypt's identity format:
    a SEQUENCE containing one BIT STRING (returned as the value byte)
    followed by INTEGERs (returned as Python ints). Anything else inside
    is silently skipped."""
    if not data or data[0] != 0x30:
        raise ValueError("expected SEQUENCE tag")
    pos = 1
    seq_len, n = _read_der_length(data, pos)
    pos += n
    seq_end = pos + seq_len

    out: list[int] = []
    while pos < seq_end:
        tag = data[pos]
        pos += 1
        length, n = _read_der_length(data, pos)
        pos += n
        if tag == 0x03:  # BIT STRING - one unused-bits byte then value byte
            out.append(data[pos + 1])
        elif tag == 0x02:  # INTEGER - signed big-endian, but TS keys are unsigned
            out.append(_buf_to_bigint(data[pos : pos + length]))
        pos += length
    return out


def _build_der_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    if length < 0x100:
        return bytes([0x81, length])
    return bytes([0x82, (length >> 8) & 0xFF, length & 0xFF])


def _build_der_integer(value: int) -> bytes:
    hex_str = format(abs(value), "x")
    if len(hex_str) % 2:
        hex_str = "0" + hex_str
    buf = bytes.fromhex(hex_str)
    if buf and buf[0] & 0x80:
        buf = b"\x00" + buf
    return b"\x02" + _build_der_length(len(buf)) + buf


def _build_ltc_public_key_der(x: int, y: int) -> bytes:
    """libtomcrypt-style public-key DER: SEQUENCE { BIT STRING(0x07,0x00),
    INTEGER(32), INTEGER(x), INTEGER(y) }. The bit string encodes 7 unused
    bits with value 0 - this is the public-only marker (0x00 bitInfo)."""
    bit_str = bytes([0x03, 0x02, 0x07, 0x00])
    int32 = _build_der_integer(32)
    int_x = _build_der_integer(x)
    int_y = _build_der_integer(y)
    content = bit_str + int32 + int_x + int_y
    return b"\x30" + _build_der_length(len(content)) + content


# --- public API: Identity --------------------------------------------------


@dataclass(slots=True)
class Identity:
    """TS3 client identity. ``private_scalar``, ``public_key_string``,
    ``key_offset`` and ``uid`` are the canonical fields (everything else is
    derived). All four are cheap to serialize/deserialize."""

    private_scalar: int
    public_key_string: str
    key_offset: int
    uid: str

    # Lazily-built crypto key objects.
    _priv: ec.EllipticCurvePrivateKey | None = field(default=None, init=False, repr=False)
    _pub: ec.EllipticCurvePublicKey | None = field(default=None, init=False, repr=False)

    @property
    def private_key(self) -> ec.EllipticCurvePrivateKey:
        if self._priv is None:
            self._priv = ec.derive_private_key(self.private_scalar, _CURVE)
        return self._priv

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        if self._pub is None:
            self._pub = self.private_key.public_key()
        return self._pub

    def to_dict(self) -> dict[str, str]:
        """Serializable form. Big ints become decimal strings."""
        return {
            "privateKeyBigInt": str(self.private_scalar),
            "keyOffset": str(self.key_offset),
            "publicKeyString": self.public_key_string,
            "uid": self.uid,
        }


def restore_identity(data: dict[str, str | int]) -> Identity:
    """Rebuild an Identity from its serialized form."""
    return Identity(
        private_scalar=int(data["privateKeyBigInt"]),
        key_offset=int(data["keyOffset"]),
        public_key_string=str(data["publicKeyString"]),
        uid=str(data["uid"]),
    )


# --- export / import -------------------------------------------------------


def export_public_key_string(public_key: ec.EllipticCurvePublicKey) -> str:
    """Render the public key in the libtomcrypt ASN.1 form TS3 uses for
    the ``omega`` parameter: base64-encoded SEQUENCE { BIT STRING, INTEGER,
    INTEGER, INTEGER }."""
    nums = public_key.public_numbers()
    der = _build_ltc_public_key_der(nums.x, nums.y)
    return base64.b64encode(der).decode("ascii")


def _identity_from_components(
    *,
    private_scalar: int,
    pub_x: int,
    pub_y: int,
    key_offset: int,
) -> Identity:
    private_key = ec.derive_private_key(private_scalar, _CURVE)
    derived = private_key.public_key().public_numbers()

    # If pub_x/pub_y were absent in the source (the 0xC0 case), derive them.
    if pub_x == 0 and pub_y == 0:
        pub_x = derived.x
        pub_y = derived.y

    if pub_x != derived.x or pub_y != derived.y:
        raise ValueError("public key in identity does not match private scalar")

    pub_key_string = export_public_key_string(private_key.public_key())
    uid = base64.b64encode(sha1(pub_key_string.encode("ascii"))).decode("ascii")

    ident = Identity(
        private_scalar=private_scalar,
        public_key_string=pub_key_string,
        key_offset=key_offset,
        uid=uid,
    )
    ident._priv = private_key
    return ident


def _import_key_from_asn(asn_data: bytes, key_offset: int) -> Identity:
    parsed = _parse_der_sequence(asn_data)
    if len(parsed) < 3:
        raise ValueError("invalid ASN.1 key data")

    bit_info = parsed[0]
    private_scalar: int | None = None
    pub_x = 0
    pub_y = 0

    if bit_info in (0x00, 0x80):
        pub_x = parsed[2]
        pub_y = parsed[3]
        if bit_info == 0x80 and len(parsed) >= 5:
            private_scalar = parsed[4]
    elif bit_info == 0xC0:
        private_scalar = parsed[2]
    else:
        raise ValueError(f"unknown key bitInfo: 0x{bit_info:02x}")

    if private_scalar is None:
        raise ValueError("key does not contain a private scalar")

    return _identity_from_components(
        private_scalar=private_scalar,
        pub_x=pub_x,
        pub_y=pub_y,
        key_offset=key_offset,
    )


def from_base64_key(base64_key: str, key_offset: int = 0) -> Identity:
    """Import from a raw libtomcrypt base64 ASN.1 key."""
    return _import_key_from_asn(base64.b64decode(base64_key), key_offset)


_TS_IDENTITY_RE = re.compile(r"^(\d+)V([\w/+=]+)$")


def from_ts_identity(identity_str: str) -> Identity:
    """Import a TS3 client identity export string of the form
    ``"<offset>V<base64data>"``. The base64 data is XOR-deobfuscated
    against ``sha1(<inner bytes>)`` for the first 20 bytes and against
    OBFUSCATION_KEY for up to 100 bytes total."""
    m = _TS_IDENTITY_RE.match(identity_str)
    if not m:
        raise ValueError("invalid TS3 identity format")

    key_offset = int(m.group(1))
    ident = bytearray(base64.b64decode(m.group(2)))
    if len(ident) < 20:
        raise ValueError("identity payload too short")

    null_idx = -1
    for i in range(20, len(ident)):
        if ident[i] == 0:
            null_idx = i - 20
            break
    hash_len = (len(ident) - 20) if null_idx < 0 else null_idx
    h = sha1(bytes(ident[20 : 20 + hash_len]))

    xor_into(ident, h, 20)
    xor_into(ident, OBFUSCATION_KEY, min(100, len(ident)))

    inner = bytes(ident).decode("utf-8", errors="ignore").split("\x00", 1)[0]
    return _import_key_from_asn(base64.b64decode(inner), key_offset)


# --- generation + hashcash -------------------------------------------------


def _security_level_at(pub_key_bytes: bytes, offset: int) -> int:
    return count_leading_zero_bits(sha1(pub_key_bytes + str(offset).encode("ascii")))


def _improve_security(identity: Identity, to_level: int) -> None:
    """Hashcash search: find the smallest offset (>= the current one) whose
    sha1(pub_key_string || str(offset)) has at least ``to_level`` LSB-first
    zero bits. Mutates ``identity.key_offset`` in place."""
    pub_bytes = identity.public_key_string.encode("ascii")
    offset = identity.key_offset
    best = _security_level_at(pub_bytes, offset)

    checked = offset
    while True:
        if best >= to_level:
            identity.key_offset = offset
            return
        curr = _security_level_at(pub_bytes, checked)
        if curr > best:
            offset = checked
            best = curr
        checked += 1


def generate_identity(security_level: int = 8) -> Identity:
    """Generate a fresh P-256 keypair and (optionally) mine a hashcash
    offset that meets ``security_level``. Blocks the calling thread - use
    ``generate_identity_async`` from inside an event loop."""
    private_key = ec.generate_private_key(_CURVE)
    private_scalar = private_key.private_numbers().private_value
    pub_key_string = export_public_key_string(private_key.public_key())
    uid = base64.b64encode(sha1(pub_key_string.encode("ascii"))).decode("ascii")

    ident = Identity(
        private_scalar=private_scalar,
        public_key_string=pub_key_string,
        key_offset=0,
        uid=uid,
    )
    ident._priv = private_key

    if security_level > 0:
        _improve_security(ident, security_level)

    return ident


async def generate_identity_async(security_level: int = 8) -> Identity:
    """Same as ``generate_identity`` but runs in a worker thread so the
    asyncio event loop stays responsive during the hashcash mining. SHA-1
    via hashlib releases the GIL so this gets real parallelism."""
    return await asyncio.to_thread(generate_identity, security_level)


# --- ECDH ------------------------------------------------------------------


def get_shared_secret(
    private_key: ec.EllipticCurvePrivateKey, server_public_key_der: bytes
) -> bytes:
    """ECDH(P-256) shared secret with the server's libtomcrypt-style public
    key. Returns SHA-1 of the (left-padded) x coordinate, which is what the
    TS3 protocol uses to seed session keys."""
    parsed = _parse_der_sequence(server_public_key_der)
    if len(parsed) < 4:
        raise ValueError("invalid server public key DER")
    pub_x = parsed[2]
    pub_y = parsed[3]

    server_pub = ec.EllipticCurvePublicNumbers(pub_x, pub_y, _CURVE).public_key()
    shared = private_key.exchange(ec.ECDH(), server_pub)

    # Normalize to exactly 32 bytes left-padded with zeros, then SHA-1.
    if len(shared) > 32:
        shared = shared[-32:]
    elif len(shared) < 32:
        shared = b"\x00" * (32 - len(shared)) + shared
    return sha1(shared)


__all__ = [
    "OBFUSCATION_KEY",
    "Identity",
    "count_leading_zero_bits",
    "export_public_key_string",
    "from_base64_key",
    "from_ts_identity",
    "generate_identity",
    "generate_identity_async",
    "get_shared_secret",
    "restore_identity",
]
