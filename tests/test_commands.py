"""TS3 command-format tests.

Vectors come from ``/tmp/commands-vectors/gen.mjs`` which inlines the
canonical ts6-manager implementation; the values it emits are exactly
what the JS code produces, so character-equality with our Python port
means we're protocol-compatible on the wire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ts6_stream_bot.ts3lib.commands import (
    build_command,
    parse_command,
    ts_escape,
    ts_unescape,
)

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "commands_vectors.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("v", _VECTORS["escape"], ids=lambda v: repr(v["input"])[:25])
def test_ts_escape(v: dict) -> None:
    assert v["output"] == ts_escape(v["input"])


@pytest.mark.parametrize("v", _VECTORS["unescape"], ids=lambda v: repr(v["input"])[:25])
def test_ts_unescape(v: dict) -> None:
    assert v["output"] == ts_unescape(v["input"])


def test_unescape_truncated_raises() -> None:
    with pytest.raises(ValueError, match="invalid escape sequence"):
        ts_unescape("trailing\\")


def test_unescape_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown escape"):
        ts_unescape("bad\\x")


@pytest.mark.parametrize("v", _VECTORS["build"], ids=lambda v: v["name"])
def test_build_command(v: dict) -> None:
    # Pydantic / json deserialization left undefined as None - rebuild the
    # original mapping with None for the dropped key.
    params: dict[str, str | int | bool | None] = {}
    for k, val in v["params"].items():
        params[k] = None if val is None else val
    assert v["output"] == build_command(v["name"], params)


def test_build_command_skips_none_values() -> None:
    out = build_command("cmd", {"keep": "x", "drop": None, "also": "y"})
    assert out == "cmd keep=x also=y"


def test_build_command_booleans_become_one_zero() -> None:
    assert build_command("flag", {"on": True, "off": False}) == "flag on=1 off=0"


@pytest.mark.parametrize("v", _VECTORS["parse_simple"], ids=lambda v: v["name"])
def test_parse_simple(v: dict) -> None:
    p = parse_command(v["raw"])
    assert p.name == v["name"]
    assert p.params == v["params"]
    assert p.groups is None


@pytest.mark.parametrize("v", _VECTORS["parse_grouped"], ids=lambda v: v["raw"][:30])
def test_parse_grouped(v: dict) -> None:
    p = parse_command(v["raw"])
    assert p.groups is not None
    assert len(p.groups) == len(v["expected_groups"])
    for got, want in zip(p.groups, v["expected_groups"], strict=True):
        assert got == want


@pytest.mark.parametrize("v", _VECTORS["round_trips"], ids=lambda v: repr(v["input"])[:25])
def test_escape_unescape_round_trip(v: dict) -> None:
    """The Node-side round-trip and our Python round-trip should both
    return the original string AND produce the same intermediate."""
    assert v["restored"] == v["input"]
    assert v["escaped"] == ts_escape(v["input"])
    assert v["input"] == ts_unescape(ts_escape(v["input"]))


def test_build_then_parse_round_trip() -> None:
    """Higher-level: a command we build should parse back to itself."""
    raw = build_command("clientupdate", {"client_nickname": "Bot With Spaces"})
    parsed = parse_command(raw)
    assert parsed.name == "clientupdate"
    assert parsed.params["client_nickname"] == "Bot With Spaces"
