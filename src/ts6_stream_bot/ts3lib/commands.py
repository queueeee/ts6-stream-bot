"""TS3 command serialization.

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/tslib/commands.ts``.

Wire format::

    commandname key=value key2=value2|key=value key2=value2

Spaces, pipes, and a fixed set of control characters are backslash-
escaped both inside the command name and inside each value. The pipe
``|`` separates "groups" - multiple rows of the same command, used for
listings (``clientlist``, ``channellist`` etc.).
"""

from __future__ import annotations

from dataclasses import dataclass

# Order matters only for `_ESCAPE_MAP` interaction with iteration: we
# escape character-by-character, so the dict is just a lookup.
_ESCAPE_MAP: dict[str, str] = {
    "\\": "\\\\",
    "/": "\\/",
    " ": "\\s",
    "|": "\\p",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
}

_UNESCAPE_MAP: dict[str, str] = {
    "\\": "\\",
    "/": "/",
    "s": " ",
    "p": "|",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


def ts_escape(s: str) -> str:
    """Escape a string for the TS3 wire format."""
    return "".join(_ESCAPE_MAP.get(ch, ch) for ch in s)


def ts_unescape(s: str) -> str:
    """Reverse ``ts_escape``. Raises ValueError on malformed escapes."""
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            i += 1
            if i >= len(s):
                raise ValueError("invalid escape sequence (truncated)")
            mapped = _UNESCAPE_MAP.get(s[i])
            if mapped is None:
                raise ValueError(f"unknown escape: \\{s[i]}")
            out.append(mapped)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


@dataclass(slots=True)
class ParsedCommand:
    name: str
    params: dict[str, str]
    # Set when the wire string contained pipe-separated groups. Element 0
    # is the same dict as ``params`` (matching the TS reference's behavior).
    groups: list[dict[str, str]] | None = None


def build_command(name: str, params: dict[str, str | int | bool | None]) -> str:
    """Render a command + parameter dict to the TS3 wire format. ``None``
    values are skipped (mirrors the JS ``undefined`` short-circuit).
    Booleans serialize as ``1``/``0``, numbers via ``str()``."""
    parts = [ts_escape(name)]
    for key, value in params.items():
        if value is None:
            continue
        str_val = ("1" if value else "0") if isinstance(value, bool) else str(value)
        parts.append(f"{key}={ts_escape(str_val)}")
    return " ".join(parts)


def _parse_kv_tokens(tokens: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in tokens:
        if not token:
            continue
        eq = token.find("=")
        if eq >= 0:
            out[token[:eq]] = ts_unescape(token[eq + 1 :])
        else:
            out[token] = ""
    return out


def parse_command(raw: str) -> ParsedCommand:
    """Parse an incoming command. Pipe-separated groups land in ``.groups``;
    the first group's params are also exposed as ``.params`` for easy
    access to single-row commands."""
    parts = raw.split("|")
    first_tokens = parts[0].strip().split(" ")
    name = ts_unescape(first_tokens[0])
    params = _parse_kv_tokens(first_tokens[1:])

    groups: list[dict[str, str]] | None = None
    if len(parts) > 1:
        groups = [params]
        for part in parts[1:]:
            groups.append(_parse_kv_tokens(part.strip().split(" ")))

    return ParsedCommand(name=name, params=params, groups=groups)


__all__ = [
    "ParsedCommand",
    "build_command",
    "parse_command",
    "ts_escape",
    "ts_unescape",
]
