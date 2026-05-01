"""TS3 voice protocol library.

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/tslib/`` in that project. This package is
the Python equivalent: crypto, identity, license, quicklz, command
serialization, and the UDP voice client itself.

Phase 1 builds this up module by module. Until the client is wired into
``StreamController`` (phase 4), nothing in here produces output - the
modules are unit-tested standalone.
"""

from __future__ import annotations

from ts6_stream_bot.ts3lib.quicklz import (
    qlz_decompress,
    qlz_get_compressed_size,
    qlz_get_decompressed_size,
)

__all__ = [
    "qlz_decompress",
    "qlz_get_compressed_size",
    "qlz_get_decompressed_size",
]
