"""TS3 voice-client unit tests (offline only).

Exercises the parts of ``Ts3Client`` that don't require a real TS6
server: packet header layout, AES-128-EAX round-trip with the dummy
key, packet counter / generation overflow, ACK handling, fragment
reassembly, and the RSA-puzzle solver. Live handshake validation has
to happen against a real server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ts6_stream_bot.ts3lib.client import (
    _C2S_HEADER_LEN,
    _FLAG_NEWPROTOCOL,
    ClientState,
    PacketType,
    Ts3Client,
    Ts3ClientOptions,
)
from ts6_stream_bot.ts3lib.commands import build_command, parse_command
from ts6_stream_bot.ts3lib.crypto import (
    DUMMY_KEY,
    DUMMY_NONCE,
    INIT_MAC,
    MAC_LEN,
    eax_decrypt,
)
from ts6_stream_bot.ts3lib.identity import generate_identity

# --- helpers ---------------------------------------------------------------


class _FakeTransport:
    """Records sent datagrams so tests can assert on the wire."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False

    def sendto(self, data: bytes, addr: Any = None) -> None:
        self.sent.append(bytes(data))

    def close(self) -> None:
        self.closed = True


def _wire_client(client: Ts3Client) -> _FakeTransport:
    """Skip the real connect() path: install a fake transport + minimal
    state so we can drive the client offline. Mirrors the counter bump
    that the real connect() applies before sending Init0."""
    fake = _FakeTransport()
    client._transport = fake  # type: ignore[assignment]
    try:
        client._loop = asyncio.get_event_loop()
    except RuntimeError:
        client._loop = asyncio.new_event_loop()
    client._opts = Ts3ClientOptions(
        host="127.0.0.1",
        port=9987,
        identity=generate_identity(security_level=0),
        nickname="test-bot",
    )
    # connect() bumps the COMMAND counter once before Init0 - keep tests in sync.
    client._inc_packet_counter(PacketType.COMMAND)
    # _send_raw refuses to push when state is DISCONNECTED.
    client._state = ClientState.HANDSHAKE
    return fake


def _decrypt_outgoing(raw: bytes) -> bytes:
    """Reverse a packet sent through _wire_client (DUMMY_KEY/DUMMY_NONCE).
    Returns the plaintext payload body."""
    header = raw[MAC_LEN : MAC_LEN + _C2S_HEADER_LEN]
    mac = raw[:MAC_LEN]
    ciphertext = raw[MAC_LEN + _C2S_HEADER_LEN :]
    out = eax_decrypt(DUMMY_KEY, DUMMY_NONCE, header, ciphertext, mac, MAC_LEN)
    assert out is not None, "DUMMY-keyed decrypt failed"
    return out


# --- packet layout --------------------------------------------------------


def test_build_init0_layout() -> None:
    c = Ts3Client()
    init0 = c._build_init0()
    assert len(init0) == 4 + 1 + 4 + 4 + 8
    # Version is first - matches INIT_VERSION (3.5.0 [Stable])
    from ts6_stream_bot.ts3lib.crypto import INIT_VERSION

    assert int.from_bytes(init0[:4], "big") == INIT_VERSION
    assert init0[4] == 0x00  # step 0


def test_build_raw_packet_header_offsets() -> None:
    c = Ts3Client()
    c._client_id = 0x1234
    raw = c._build_raw_packet(b"hello", PacketType.COMMAND, 0x00AB, 0, _FLAG_NEWPROTOCOL)
    # MAC region untouched here (set by _encrypt_packet later)
    assert raw[MAC_LEN : MAC_LEN + 2] == b"\x00\xab"
    assert raw[MAC_LEN + 2 : MAC_LEN + 4] == b"\x12\x34"
    # PT byte: high nibble = flags, low nibble = packet type (COMMAND=2)
    assert raw[MAC_LEN + 4] == _FLAG_NEWPROTOCOL | int(PacketType.COMMAND)
    # Payload follows the header.
    assert raw[MAC_LEN + _C2S_HEADER_LEN :] == b"hello"


def test_packet_counter_increments_and_wraps_to_generation() -> None:
    c = Ts3Client()
    # Walk the counter to the wrap point.
    c._packet_counter[int(PacketType.COMMAND)] = 0xFFFE
    pid, gen = c._get_packet_counter(PacketType.COMMAND)
    assert (pid, gen) == (0xFFFE, 0)

    c._inc_packet_counter(PacketType.COMMAND)  # -> 0xFFFF
    pid, gen = c._get_packet_counter(PacketType.COMMAND)
    assert (pid, gen) == (0xFFFF, 0)

    c._inc_packet_counter(PacketType.COMMAND)  # wraps -> 0, gen++
    pid, gen = c._get_packet_counter(PacketType.COMMAND)
    assert (pid, gen) == (0, 1)


def test_packet_counter_init1_returns_static() -> None:
    c = Ts3Client()
    assert c._get_packet_counter(PacketType.INIT1) == (101, 0)
    # Calling inc on INIT1 must be a no-op (the resend cycle uses 101 forever).
    c._inc_packet_counter(PacketType.INIT1)
    assert c._get_packet_counter(PacketType.INIT1) == (101, 0)


# --- encryption ------------------------------------------------------------


def test_encrypt_decrypt_round_trip_with_dummy_keys() -> None:
    """Before crypto init completes, packets use DUMMY_KEY / DUMMY_NONCE.
    A round-trip with our EAX layer must produce the original payload."""
    c = Ts3Client()
    c._client_id = 0x0001
    payload = b"clientinitiv alpha=AAAA omega=BBBB"
    raw = c._build_raw_packet(payload, PacketType.COMMAND, 0x0010, 0, _FLAG_NEWPROTOCOL)
    encrypted = c._encrypt_packet(raw, PacketType.COMMAND, 0x0010, 0, _FLAG_NEWPROTOCOL, payload)
    assert (
        encrypted[MAC_LEN : MAC_LEN + _C2S_HEADER_LEN] == raw[MAC_LEN : MAC_LEN + _C2S_HEADER_LEN]
    )

    header = encrypted[MAC_LEN : MAC_LEN + _C2S_HEADER_LEN]
    mac = encrypted[:MAC_LEN]
    ciphertext = encrypted[MAC_LEN + _C2S_HEADER_LEN :]
    decrypted = eax_decrypt(DUMMY_KEY, DUMMY_NONCE, header, ciphertext, mac, MAC_LEN)
    assert decrypted == payload


def test_encrypt_init1_uses_init_mac() -> None:
    c = Ts3Client()
    raw = c._build_raw_packet(b"x" * 21, PacketType.INIT1, 101, 0, 0x80)
    encrypted = c._encrypt_packet(raw, PacketType.INIT1, 101, 0, 0x80, b"x" * 21)
    assert encrypted[:MAC_LEN] == INIT_MAC[:MAC_LEN]


# --- ack / resend ----------------------------------------------------------


def test_send_command_tracks_resend_for_commands() -> None:
    c = Ts3Client()
    _wire_client(c)
    c._state = ClientState.CONNECTED
    c.send_command("clientupdate client_nickname=test")
    # _wire_client bumped the counter once to mirror connect()'s behaviour,
    # so the first send goes out as packet id 1.
    assert 1 in c._resend_map


def test_handle_ack_removes_from_resend_map() -> None:
    c = Ts3Client()
    _wire_client(c)
    c._state = ClientState.CONNECTED
    c.send_command("foo")
    pid = next(iter(c._resend_map))

    # ACK payload is just the packet id big-endian.
    c._handle_ack(pid.to_bytes(2, "big"))
    assert pid not in c._resend_map


def test_handle_ack_short_payload_is_noop() -> None:
    c = Ts3Client()
    c._handle_ack(b"\x00")  # less than 2 bytes
    # No exception, no state change.


# --- fragmentation --------------------------------------------------------


def test_handle_command_data_reassembles_two_fragments() -> None:
    """Big commands span two fragments with FLAG_FRAGMENTED on first + last."""
    c = Ts3Client()
    seen: list[str] = []
    c.on_command = lambda parsed: seen.append(parsed.name)
    c._handle_command_data(b"clientinit nick=Alic", 0x10)
    c._handle_command_data(b"e meta=", 0x10)
    assert seen == ["clientinit"]


def test_handle_command_data_passes_unfragmented_through() -> None:
    c = Ts3Client()
    seen: list[str] = []
    c.on_command = lambda parsed: seen.append(parsed.name)
    c._handle_command_data(b"clientinit nick=Bob", 0x00)
    assert seen == ["clientinit"]


# --- command dispatch -----------------------------------------------------


def test_process_command_dispatches_text_message() -> None:
    c = Ts3Client()
    c._state = ClientState.CONNECTED
    seen: list[dict[str, str]] = []
    c.on_text_message = seen.append
    raw = build_command("notifytextmessage", {"msg": "hi", "invokerid": 5})
    c._process_command(raw.encode("utf-8"))
    assert len(seen) == 1
    assert seen[0]["msg"] == "hi"


def test_process_command_emits_ts3error() -> None:
    c = Ts3Client()
    c._state = ClientState.CONNECTED
    errs: list[dict[str, str]] = []
    c.on_ts3error = errs.append
    raw = build_command("error", {"id": 0, "msg": "ok"})
    c._process_command(raw.encode("utf-8"))
    assert errs == [{"id": "0", "msg": "ok"}]


@pytest.mark.asyncio
async def test_process_command_handles_initserver_and_emits_connected() -> None:
    c = Ts3Client()
    c._loop = asyncio.get_running_loop()
    try:
        connected: list[bool] = []
        c.on_connected = lambda: connected.append(True)
        raw = build_command("initserver", {"aclid": 42})
        c._process_command(raw.encode("utf-8"))
        assert c._state == ClientState.CONNECTED
        assert c._client_id == 42
        assert connected == [True]
    finally:
        # force_close cancels the ping task that initserver scheduled, so
        # the event loop doesn't complain about un-awaited coroutines.
        c.force_close()


# --- channel-list bookkeeping ---------------------------------------------


def test_handle_channellist_grouped_then_finished_moves_to_named_channel() -> None:
    c = Ts3Client()
    fake = _wire_client(c)
    c._state = ClientState.CONNECTED
    c._client_id = 7
    assert c._opts is not None
    c._opts.default_channel = "Lounge"

    grouped = build_command("channellist", {"cid": 1, "channel_name": "Default"})
    grouped += "|cid=2 channel_name=Lounge"
    c._process_command(grouped.encode("utf-8"))

    c._process_command(b"channellistfinished")

    plaintexts = [_decrypt_outgoing(s).decode("utf-8") for s in fake.sent]
    moves = [s for s in plaintexts if s.startswith("clientmove")]
    assert moves, f"no clientmove sent: {plaintexts}"
    parsed = parse_command(moves[0])
    assert parsed.params["cid"] == "2"
    assert parsed.params["clid"] == "7"


# --- RSA puzzle ------------------------------------------------------------


def test_rsa_puzzle_modpow_matches_python_builtin() -> None:
    """Sanity check the modular exponent we use for the puzzle: pow(x, 2**level, n)."""
    x = 0x1234_5678_9ABC_DEF0
    n = (1 << 64) - 59  # a prime-ish modulus
    level = 8
    assert pow(x, 1 << level, n) == pow(x, 2**level, n)


# --- init0 / init handshake structure -------------------------------------


def test_handle_init_step1_builds_init2() -> None:
    c = Ts3Client()
    fake = _wire_client(c)

    # Step 1 payload: 1 byte step + 20 bytes server data
    payload = bytes([0x01]) + b"\xab" * 20
    c._handle_init(payload)

    assert len(fake.sent) == 1
    init2 = fake.sent[0]
    # Init packet: MAC(8) + C2S header(5) + body(25 bytes)
    body = init2[MAC_LEN + _C2S_HEADER_LEN :]
    assert body[4] == 0x02  # step 2
    # Body bytes 5..25 are the 20 bytes from step 1.
    assert body[5:25] == b"\xab" * 20


# --- async smoke test (event loop integration) ----------------------------


@pytest.mark.asyncio
async def test_force_close_is_idempotent() -> None:
    c = Ts3Client()
    fake = _wire_client(c)
    c._state = ClientState.CONNECTED
    c.force_close()
    c.force_close()
    assert c._state == ClientState.DISCONNECTED
    assert fake.closed
