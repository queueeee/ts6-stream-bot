"""QuickLZ level-1 decoder tests.

Vectors in ``tests/fixtures/quicklz_vectors.json`` are produced by a small
Node encoder (``/tmp/qlz-vectors/encode.mjs``) which round-trip-validates
each one against the canonical ts6-manager decoder before emitting it.
The fixture covers stored-verbatim (short + long header) and a
literals-only compressed encoding that exercises the control-word loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ts6_stream_bot.ts3lib.quicklz import (
    qlz_decompress,
    qlz_get_compressed_size,
    qlz_get_decompressed_size,
)

_VECTORS_PATH = Path(__file__).parent / "fixtures" / "quicklz_vectors.json"


def _vectors() -> list[tuple[str, bytes, bytes]]:
    raw = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
    return [(v["name"], bytes.fromhex(v["input"]), bytes.fromhex(v["encoded"])) for v in raw]


_VECTORS = _vectors()


@pytest.mark.parametrize(("name", "expected", "encoded"), _VECTORS, ids=[v[0] for v in _VECTORS])
def test_roundtrip(name: str, expected: bytes, encoded: bytes) -> None:
    assert qlz_decompress(encoded) == expected


def test_size_helpers_short_header() -> None:
    # "hello" stored short: flags=04 csize=08 dsize=05 + payload
    encoded = bytes.fromhex("04080568656c6c6f")
    assert qlz_get_compressed_size(encoded) == 8
    assert qlz_get_decompressed_size(encoded) == 5


def test_size_helpers_long_header() -> None:
    # "hello" stored long: flags=06 csize_le32=0e000000 dsize_le32=05000000 + payload
    encoded = bytes.fromhex("060e0000000500000068656c6c6f")
    assert qlz_get_compressed_size(encoded) == 14
    assert qlz_get_decompressed_size(encoded) == 5


def test_rejects_unsupported_level() -> None:
    # bits 2-3 = 0b10 (level 2). Level != 1 must raise.
    bad = bytes.fromhex("080403") + b"\x00\x00\x00"
    with pytest.raises(ValueError, match="level"):
        qlz_decompress(bad)


def test_rejects_oversized_decompressed_size() -> None:
    # Long header claiming a 2 MiB decompressed size (over the 1 MiB cap).
    bad = b"\x06" + (15).to_bytes(4, "little") + (2 * 1024 * 1024).to_bytes(4, "little")
    with pytest.raises(ValueError, match="exceeds max"):
        qlz_decompress(bad)
