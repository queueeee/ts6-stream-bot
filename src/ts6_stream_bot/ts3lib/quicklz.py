"""QuickLZ level 1 decompression.

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/tslib/quicklz.ts``. The TS implementation
itself is a port of TSLib's ``QuickerLz.cs``. We only ever decompress
incoming server frames; compression is not needed by the bot.

Format quick reference
----------------------
First byte ``flags``::

    bit 0       : 1 = compressed, 0 = stored verbatim
    bits 2-3    : compression level (only level 1 is supported)
    bit 1       : long-header flag - 9-byte header vs. 3-byte header

For a short header (3 bytes total): ``flags | csize | dsize`` as three
bytes. For a long header (9 bytes): ``flags | csize_le32 | dsize_le32``.
"""

from __future__ import annotations

_TABLE_SIZE = 4096
_MAX_DECOMPRESSED_SIZE = 1024 * 1024


def qlz_get_decompressed_size(data: bytes) -> int:
    if data[0] & 0x02:
        return int.from_bytes(data[5:9], "little", signed=True)
    return data[2]


def qlz_get_compressed_size(data: bytes) -> int:
    if data[0] & 0x02:
        return int.from_bytes(data[1:5], "little", signed=True)
    return data[1]


def _read24(buf: bytes | bytearray, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16)


def _hash(value: int) -> int:
    return ((value >> 12) ^ value) & 0xFFF


def qlz_decompress(data: bytes) -> bytes:
    """Decompress a QuickLZ level-1 frame.

    Raises ValueError on malformed/oversized input or unsupported levels.
    """
    flags = data[0]
    level = (flags >> 2) & 0x03
    if level != 1:
        raise ValueError(f"QuickLZ level {level} not supported")

    header_len = 9 if (flags & 0x02) else 3
    decompressed_size = qlz_get_decompressed_size(data)
    if decompressed_size > _MAX_DECOMPRESSED_SIZE:
        raise ValueError(f"Decompressed size {decompressed_size} exceeds max")
    if decompressed_size < 0:
        raise ValueError(f"Negative decompressed size {decompressed_size}")

    dest = bytearray(decompressed_size)

    # Stored uncompressed.
    if (flags & 0x01) == 0:
        dest[:decompressed_size] = data[header_len : header_len + decompressed_size]
        return bytes(dest)

    hashtable = [0] * _TABLE_SIZE
    control = 1
    source_pos = header_len
    dest_pos = 0
    next_hashed = 0

    while True:
        if control == 1:
            control = int.from_bytes(data[source_pos : source_pos + 4], "little")
            source_pos += 4

        if control & 1:
            # Back-reference
            control >>= 1
            nxt = data[source_pos]
            source_pos += 1
            hash_idx = (nxt >> 4) | (data[source_pos] << 4)
            source_pos += 1

            match_len = nxt & 0x0F
            if match_len != 0:
                match_len += 2
            else:
                match_len = data[source_pos]
                source_pos += 1

            offset = hashtable[hash_idx]

            # Byte-wise copy, may overlap.
            dest[dest_pos] = dest[offset]
            dest[dest_pos + 1] = dest[offset + 1]
            dest[dest_pos + 2] = dest[offset + 2]
            for i in range(3, match_len):
                dest[dest_pos + i] = dest[offset + i]
            dest_pos += match_len

            end = dest_pos + 1 - match_len
            if next_hashed < end:
                rolling = _read24(dest, next_hashed)
                hashtable[_hash(rolling)] = next_hashed
                for i in range(next_hashed + 1, end):
                    rolling = (rolling >> 8) | (dest[i + 2] << 16)
                    hashtable[_hash(rolling)] = i
            next_hashed = dest_pos
        elif dest_pos >= max(decompressed_size, 10) - 10:
            # Near the end: copy remaining bytes verbatim. The original
            # encoder writes tail literals without reusing the control
            # word slot, so we don't reload control here either.
            while dest_pos < decompressed_size:
                if control == 1:
                    source_pos += 4
                control >>= 1
                dest[dest_pos] = data[source_pos]
                dest_pos += 1
                source_pos += 1
            break
        else:
            # Literal
            dest[dest_pos] = data[source_pos]
            dest_pos += 1
            source_pos += 1
            control >>= 1

            end = max(dest_pos - 2, 0)
            if next_hashed < end:
                rolling = _read24(dest, next_hashed)
                hashtable[_hash(rolling)] = next_hashed
                for i in range(next_hashed + 1, end):
                    rolling = (rolling >> 8) | (dest[i + 2] << 16)
                    hashtable[_hash(rolling)] = i
            if next_hashed < end:
                next_hashed = end

    return bytes(dest)


__all__ = [
    "qlz_decompress",
    "qlz_get_compressed_size",
    "qlz_get_decompressed_size",
]
