"""Disk-backed persistence for the TS3 client identity.

A fresh hashcash-mined identity is cheap (level 8 finishes in milliseconds)
but it is the *cryptographic identifier* the TS6 server uses to recognise
the bot across reconnects. If we generate a new one on every container
start, every previous session looks like an unrelated client to the
server: stream slots from those past sessions never get cleaned up,
they pile up as zombies that the TS6 client UI still offers for
"Join", and clicking a zombie sends the request into the void (no log
on the live bot, UI hangs at "connecting").

Persisting the identity to a volume restores the expected lifecycle:
the bot reconnects with the same key, the server cleans up the old
session via its normal client-disconnect path, and the new
``setupstream`` is the only stream slot the channel exposes.

The on-disk format is the JSON dict produced by ``Identity.to_dict``
(``privateKeyBigInt`` / ``keyOffset`` / ``publicKeyString`` / ``uid``).
We write to a tempfile in the same directory and ``os.replace`` over
the target so a crash mid-write can't leave a half-written file.
File mode is 0600 so the host-side volume only exposes the key to
whoever already has filesystem access to that directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog

from ts6_stream_bot.ts3lib.identity import (
    Identity,
    export_public_key_string,
    generate_identity_async,
    restore_identity,
)

log = structlog.get_logger(__name__)


async def load_or_generate_identity(path: Path, *, security_level: int) -> Identity:
    """Return the identity stored at ``path``; generate + save one if the
    file is missing. ``security_level`` is only consulted for the
    generation branch - existing files are trusted as-is so that the
    server-side recognition remains stable even if the operator bumps
    the level later (see module docstring)."""
    existing = _try_load(path)
    if existing is not None:
        log.info("identity_store.loaded", path=str(path), uid=existing.uid)
        return existing

    log.info("identity_store.generating", path=str(path), security_level=security_level)
    identity = await generate_identity_async(security_level=security_level)
    _save(path, identity)
    log.info("identity_store.saved", path=str(path), uid=identity.uid)
    return identity


def _try_load(path: Path) -> Identity | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("identity_store.load_failed", path=str(path), error=str(exc))
        return None
    try:
        identity = restore_identity(data)
        # ``restore_identity`` is permissive; force the lazy private-key
        # derivation and cross-check that the stored public-key string
        # matches what we derive from the private scalar. A corrupt or
        # tampered file gets rejected here rather than later inside the
        # signing loop, where the failure is harder to diagnose.
        derived = export_public_key_string(identity.public_key)
        if derived != identity.public_key_string:
            raise ValueError("public key in file does not match private scalar")
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("identity_store.restore_failed", path=str(path), error=str(exc))
        return None
    return identity


def _save(path: Path, identity: Identity) -> None:
    """Atomic write: tempfile in the same directory + ``os.replace``. The
    same-dir constraint matters because ``replace`` is only atomic across
    a single filesystem, and the parent directory is the only spot we
    know shares the target's filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(identity.to_dict(), indent=2, sort_keys=True)

    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


__all__ = ["load_or_generate_identity"]
