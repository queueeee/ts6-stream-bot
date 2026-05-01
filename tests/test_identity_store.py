"""Tests for the disk-backed identity store.

The point of the store is that two consecutive calls return the SAME
crypto identity (so the TS6 server recognises us across restarts) -
that's the round-trip test below. Failure-mode tests confirm corrupt
or unreadable files don't lock the bot out forever; we always fall
back to generating a fresh identity rather than refusing to start.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from ts6_stream_bot.ts3lib.identity import generate_identity
from ts6_stream_bot.ts3lib.identity_store import load_or_generate_identity


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "identity.json"


async def test_first_call_generates_and_persists(store_path: Path) -> None:
    """Cold start: file missing, helper mines + writes it."""
    assert not store_path.exists()

    ident = await load_or_generate_identity(store_path, security_level=0)

    assert store_path.is_file()
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    assert payload["uid"] == ident.uid
    assert payload["publicKeyString"] == ident.public_key_string
    assert int(payload["privateKeyBigInt"]) == ident.private_scalar


async def test_second_call_returns_same_identity(store_path: Path) -> None:
    """Warm start: file exists, helper loads it byte-identical."""
    first = await load_or_generate_identity(store_path, security_level=0)
    second = await load_or_generate_identity(store_path, security_level=0)

    assert first.uid == second.uid
    assert first.public_key_string == second.public_key_string
    assert first.private_scalar == second.private_scalar


async def test_warm_start_does_not_call_generator(store_path: Path) -> None:
    """If the file already exists, generation must be skipped entirely
    (mining is the expensive part we're trying to avoid on every restart)."""
    seed = generate_identity(security_level=0)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(seed.to_dict()), encoding="utf-8")

    with patch(
        "ts6_stream_bot.ts3lib.identity_store.generate_identity_async",
    ) as gen_mock:
        result = await load_or_generate_identity(store_path, security_level=24)

    gen_mock.assert_not_called()
    assert result.uid == seed.uid


async def test_corrupt_json_falls_back_to_fresh_generation(store_path: Path) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("not valid json {", encoding="utf-8")

    ident = await load_or_generate_identity(store_path, security_level=0)

    # The corrupt file is overwritten with a valid serialization.
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    assert payload["uid"] == ident.uid


async def test_invalid_keypair_data_falls_back(store_path: Path) -> None:
    """Well-formed JSON whose private scalar doesn't match the embedded
    public key should be discarded - rejecting the file leaves the
    bot stuck, regenerating moves us forward."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    bad = {
        "privateKeyBigInt": "12345",
        "keyOffset": "0",
        "publicKeyString": "not-a-real-key",
        "uid": "deadbeef",
    }
    store_path.write_text(json.dumps(bad), encoding="utf-8")

    ident = await load_or_generate_identity(store_path, security_level=0)

    # New identity, not the bogus one.
    assert ident.uid != "deadbeef"
    assert json.loads(store_path.read_text(encoding="utf-8"))["uid"] == ident.uid


async def test_saved_file_has_owner_only_permissions(store_path: Path) -> None:
    """The identity is the bot's TS6 credential - readable by only us."""
    await load_or_generate_identity(store_path, security_level=0)
    mode = stat.S_IMODE(os.stat(store_path).st_mode)
    assert mode == 0o600


async def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    """``IDENTITY_PATH`` defaults to ``/app/state/identity.json``; the
    ``state`` directory may not exist yet on a brand-new volume."""
    nested = tmp_path / "a" / "b" / "c" / "identity.json"
    assert not nested.parent.exists()

    await load_or_generate_identity(nested, security_level=0)

    assert nested.is_file()


async def test_no_tempfile_left_behind_after_save(store_path: Path) -> None:
    """Atomic-rename uses a sibling tempfile; on success it must be gone."""
    await load_or_generate_identity(store_path, security_level=0)
    leftovers = [p for p in store_path.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


async def test_concurrent_first_calls_converge(store_path: Path) -> None:
    """Two coroutines racing on a cold start may both generate, but the
    file ends up readable and the helper is otherwise crash-free."""
    results = await asyncio.gather(
        load_or_generate_identity(store_path, security_level=0),
        load_or_generate_identity(store_path, security_level=0),
    )

    assert all(r.uid for r in results)
    # Whichever write won, the file is parseable afterwards.
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    assert payload["uid"] in {r.uid for r in results}
