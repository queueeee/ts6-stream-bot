"""TS3 voice client (UDP).

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/tslib/client.ts``.

Talks the TS3 voice protocol directly over UDP:

* Init handshake (steps 0/2/4 → server replies 1/3/5) including the RSA
  client puzzle (we solve ``y = x^(2^level) mod n``).
* Crypto handshake. Old protocol is ``initivexpand`` (ECDH P-256 with
  the server's omega). New protocol is ``initivexpand2`` (Ed25519
  license chain + temporary keypair, signed proof). The session
  ``ivStruct`` is XOR-built and seeds per-packet AES-128-EAX keys.
* Command sending with backpressure-aware fragmentation, packet IDs +
  generation counters per packet type, and an ACK-driven resend loop
  for reliable command delivery.
* Voice frame sending (Opus, type Music = 5) at the consumer's pacing.
* Channel-list reception so the bot can auto-join a named channel after
  ``initserver``.
* Event callbacks: ``on_connected``, ``on_disconnected``, ``on_error``,
  ``on_command``, ``on_voice``, ``on_text_message``, ``on_ts3error``.

Real-server validation has to happen on a live TS6 instance - the
unit-testable surface here is packet building / encryption /
fragmentation, which the test module exercises against the same
ts6-manager-derived vectors used elsewhere.
"""

from __future__ import annotations

import asyncio
import enum
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ts6_stream_bot.ts3lib.commands import (
    ParsedCommand,
    build_command,
    parse_command,
)
from ts6_stream_bot.ts3lib.crypto import (
    DUMMY_KEY,
    DUMMY_NONCE,
    INIT_MAC,
    INIT_VERSION,
    MAC_LEN,
    derive_key_nonce,
    eax_decrypt,
    eax_encrypt,
    hash_password,
    sha1,
    xor_into,
)
from ts6_stream_bot.ts3lib.identity import Identity, get_shared_secret
from ts6_stream_bot.ts3lib.license import (
    derive_license_key,
    generate_temporary_key,
    get_shared_secret2,
    parse_license,
)
from ts6_stream_bot.ts3lib.quicklz import qlz_decompress

log = structlog.get_logger(__name__)


# --- protocol constants ----------------------------------------------------


class PacketType(enum.IntEnum):
    VOICE = 0
    VOICE_WHISPER = 1
    COMMAND = 2
    COMMAND_LOW = 3
    PING = 4
    PONG = 5
    ACK = 6
    ACK_LOW = 7
    INIT1 = 8


_FLAG_FRAGMENTED = 0x10
_FLAG_NEWPROTOCOL = 0x20
_FLAG_COMPRESSED = 0x40
_FLAG_UNENCRYPTED = 0x80

_C2S_HEADER_LEN = 5  # PId(2) + CId(2) + PT(1)
_S2C_HEADER_LEN = 3  # PId(2) + PT(1)
_MAX_PACKET_SIZE = 500
_MAX_OUT_CONTENT = _MAX_PACKET_SIZE - MAC_LEN - _C2S_HEADER_LEN

# TS3AudioBot's far-future Linux build sign - lets us look like a current client.
_VERSION_PLATFORM = "Linux"
_VERSION_STRING = "3.?.? [Build: 5680278000]"
_VERSION_SIGN = (
    "Hjd+N58Gv3ENhoKmGYy2bNRBsNNgm5kpiaQWxOj5HN2DXttG6REjymSwJtpJ8muC2gSwRuZi0R+8Laan5ts5CQ=="
)

# Fatal TS3 error IDs that should reject the connect promise instead of
# living through resend retries: 2568 = invalid password, 3329 = banned,
# 1796 = max clients reached.
_FATAL_TS3_ERRORS = frozenset({2568, 3329, 1796})


class ClientState(enum.StrEnum):
    DISCONNECTED = "disconnected"
    INIT = "init"
    HANDSHAKE = "handshake"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"


@dataclass(slots=True)
class Ts3ClientOptions:
    host: str
    port: int
    identity: Identity
    nickname: str
    server_password: str = ""
    default_channel: str = ""
    channel_password: str = ""


@dataclass(slots=True)
class _ResendPacket:
    raw: bytes
    packet_id: int
    first_send: float
    last_send: float


# --- main client -----------------------------------------------------------


class Ts3Client:
    """One TS3 voice connection over UDP. Use ``await connect(opts)`` to
    establish, ``send_voice`` / ``send_command`` to push, and ``disconnect``
    or ``force_close`` to tear down."""

    def __init__(self) -> None:
        self._state = ClientState.DISCONNECTED
        self._opts: Ts3ClientOptions | None = None

        self._transport: asyncio.DatagramTransport | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Per-packet-type counters. Index by PacketType; uses .value so the
        # array doesn't depend on enum ordering accidents.
        self._packet_counter = [0] * 9
        self._generation_counter = [0] * 9
        self._in_generation_counter = [0] * 9

        self._resend_map: dict[int, _ResendPacket] = {}
        self._init_resend: _ResendPacket | None = None
        self._resend_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._last_message_time = time.monotonic()

        self._crypto_init_complete = False
        self._iv_struct: bytes | None = None
        self._fake_signature = bytearray(MAC_LEN)
        self._alpha_tmp: bytes | None = None

        self._client_id = 0

        self._fragment_buffer: list[bytes] = []
        self._fragmenting = False
        self._fragment_flags = 0

        self._channel_map: dict[str, int] = {}

        self._connected_event: asyncio.Event = asyncio.Event()
        self._connect_error: BaseException | None = None

        # Public callbacks - assign before connect() to receive events.
        self.on_connected: Callable[[], None] | None = None
        self.on_disconnected: Callable[[], None] | None = None
        self.on_error: Callable[[BaseException], None] | None = None
        self.on_command: Callable[[ParsedCommand], None] | None = None
        self.on_voice: Callable[[bytes], None] | None = None
        self.on_text_message: Callable[[dict[str, str]], None] | None = None
        self.on_ts3error: Callable[[dict[str, str]], None] | None = None

    # --- public API --------------------------------------------------------

    @property
    def state(self) -> ClientState:
        return self._state

    @property
    def client_id(self) -> int:
        return self._client_id

    async def connect(self, opts: Ts3ClientOptions, *, timeout: float = 15.0) -> None:
        """Open the UDP socket and run the init + crypto handshake. Returns
        once the server sends ``initserver`` (state → CONNECTED). Raises
        the underlying error if anything fails before that."""
        self._opts = opts
        self._reset_state()
        self._loop = asyncio.get_running_loop()
        self._connected_event.clear()
        self._connect_error = None

        self._transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _Ts3DatagramProtocol(self),
            remote_addr=(opts.host, opts.port),
        )

        self._last_message_time = time.monotonic()
        self._resend_task = self._loop.create_task(self._resend_loop())

        # Match the JS quirk: the command counter starts at 1, not 0.
        self._inc_packet_counter(PacketType.COMMAND)

        self._send_init_packet(self._build_init0())

        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except TimeoutError:
            self.force_close()
            raise ConnectionError("TS3 connect timeout") from None
        if self._connect_error is not None:
            self.force_close()
            raise self._connect_error

    def force_close(self) -> None:
        """Close the socket immediately without sending a clean disconnect."""
        if self._state == ClientState.DISCONNECTED:
            return
        self._state = ClientState.DISCONNECTED
        self._cancel_tasks()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def disconnect(self) -> None:
        """Send a polite ``clientdisconnect`` then close the socket."""
        if self._state == ClientState.CONNECTED:
            self._state = ClientState.DISCONNECTING
            self.send_command(
                build_command("clientdisconnect", {"reasonid": 8, "reasonmsg": "leaving"})
            )
        if self._loop is not None:
            self._loop.call_later(0.5, self._cleanup)

    def send_voice(self, opus_data: bytes) -> None:
        """Send one Opus frame as a Voice packet (``codec = Music = 5``)."""
        if self._state != ClientState.CONNECTED:
            return
        buf = bytearray(3 + len(opus_data))
        # bytes 0-1 are the voice packet ID, written by _send_outgoing.
        buf[2] = 5
        buf[3:] = opus_data
        self._send_outgoing(bytes(buf), PacketType.VOICE)

    def send_voice_stop(self) -> None:
        """End-of-voice marker (empty payload after the codec byte)."""
        if self._state != ClientState.CONNECTED:
            return
        buf = bytearray(3)
        buf[2] = 5
        self._send_outgoing(bytes(buf), PacketType.VOICE)

    def send_command(self, cmd: str) -> None:
        """Send a TS3 command. Auto-fragments if it exceeds the wire MTU."""
        data = cmd.encode("utf-8")
        if len(data) <= _MAX_OUT_CONTENT:
            self._send_outgoing(data, PacketType.COMMAND)
            return

        chunks = [data[i : i + _MAX_OUT_CONTENT] for i in range(0, len(data), _MAX_OUT_CONTENT)]
        cmd_name = cmd.split(" ", 1)[0]
        log.info(
            "ts3.command_fragmented", cmd=cmd_name, total_bytes=len(data), fragments=len(chunks)
        )

        # TS3 fragmentation flag: set on the FIRST and LAST fragment only.
        for i, chunk in enumerate(chunks):
            extra = _FLAG_FRAGMENTED if i in (0, len(chunks) - 1) else 0
            self._send_outgoing(chunk, PacketType.COMMAND, extra)

    # --- transport plumbing ------------------------------------------------

    def _emit_error(self, exc: BaseException) -> None:
        if self.on_error is not None:
            with self._guard():
                self.on_error(exc)
        if not self._connected_event.is_set():
            self._connect_error = exc
            self._connected_event.set()

    def _on_datagram(self, data: bytes) -> None:
        try:
            self._handle_incoming_packet(data)
        except Exception as exc:
            log.exception("ts3.handle_packet_failed", error=str(exc))
            self._emit_error(exc)

    def _on_transport_error(self, exc: BaseException) -> None:
        self._emit_error(exc)

    # --- packet building / sending ----------------------------------------

    def _build_init0(self) -> bytes:
        # version(4 BE) + step(1) + timestamp_be(4) + random(4) + reserved(8)
        buf = bytearray(4 + 1 + 4 + 4 + 8)
        buf[0:4] = INIT_VERSION.to_bytes(4, "big", signed=False)
        buf[4] = 0x00
        buf[5:9] = int(time.time()).to_bytes(4, "big", signed=False)
        buf[9:13] = os.urandom(4)
        # bytes 13..20 are reserved zero
        return bytes(buf)

    def _send_init_packet(self, data: bytes) -> None:
        raw = self._build_raw_packet(data, PacketType.INIT1, 101, 0, _FLAG_UNENCRYPTED)
        # Init packets carry the literal INIT_MAC instead of a real MAC.
        raw_buf = bytearray(raw)
        raw_buf[:MAC_LEN] = INIT_MAC[:MAC_LEN]
        raw_bytes = bytes(raw_buf)
        now = time.monotonic()
        self._init_resend = _ResendPacket(
            raw=raw_bytes, packet_id=101, first_send=now, last_send=now
        )
        self._send_raw(raw_bytes)

    def _send_outgoing(self, data: bytes, packet_type: PacketType, extra_flags: int = 0) -> None:
        pid, gen = self._get_packet_counter(packet_type)
        self._inc_packet_counter(packet_type)

        flags = extra_flags
        payload = bytearray(data)

        if packet_type in (PacketType.VOICE, PacketType.VOICE_WHISPER):
            payload[0:2] = pid.to_bytes(2, "big", signed=False)
        elif packet_type in (PacketType.COMMAND, PacketType.COMMAND_LOW):
            flags |= _FLAG_NEWPROTOCOL
        elif packet_type in (PacketType.PING, PacketType.PONG):
            flags |= _FLAG_UNENCRYPTED

        raw = self._build_raw_packet(bytes(payload), packet_type, pid, gen, flags)
        raw = self._encrypt_packet(raw, packet_type, pid, gen, flags, bytes(payload))

        if packet_type in (PacketType.COMMAND, PacketType.COMMAND_LOW):
            now = time.monotonic()
            self._resend_map[pid] = _ResendPacket(
                raw=raw, packet_id=pid, first_send=now, last_send=now
            )

        self._send_raw(raw)

    def _build_raw_packet(
        self,
        data: bytes,
        packet_type: PacketType,
        packet_id: int,
        _gen: int,
        flags: int,
    ) -> bytes:
        buf = bytearray(MAC_LEN + _C2S_HEADER_LEN + len(data))
        buf[MAC_LEN : MAC_LEN + 2] = packet_id.to_bytes(2, "big", signed=False)
        buf[MAC_LEN + 2 : MAC_LEN + 4] = self._client_id.to_bytes(2, "big", signed=False)
        buf[MAC_LEN + 4] = (flags & 0xF0) | (int(packet_type) & 0x0F)
        buf[MAC_LEN + _C2S_HEADER_LEN :] = data
        return bytes(buf)

    def _encrypt_packet(
        self,
        raw: bytes,
        packet_type: PacketType,
        packet_id: int,
        gen: int,
        flags: int,
        data: bytes,
    ) -> bytes:
        out = bytearray(raw)

        if packet_type == PacketType.INIT1:
            out[:MAC_LEN] = INIT_MAC[:MAC_LEN]
            return bytes(out)

        if flags & _FLAG_UNENCRYPTED:
            out[:MAC_LEN] = self._fake_signature[:MAC_LEN]
            return bytes(out)

        use_dummy = not self._crypto_init_complete
        if use_dummy:
            key, nonce = DUMMY_KEY, DUMMY_NONCE
        else:
            kn = self._get_key_nonce(False, packet_id, gen, packet_type)
            key, nonce = kn

        header = bytes(out[MAC_LEN : MAC_LEN + _C2S_HEADER_LEN])
        result = eax_encrypt(key, nonce, header, data, MAC_LEN)
        out[:MAC_LEN] = result.mac
        out[MAC_LEN + _C2S_HEADER_LEN :] = result.ciphertext
        return bytes(out)

    def _decrypt_packet(
        self,
        raw: bytes,
        packet_type: PacketType,
        packet_id: int,
        gen: int,
        flags: int,
    ) -> bytes | None:
        if packet_type == PacketType.INIT1:
            if raw[:MAC_LEN] != INIT_MAC[:MAC_LEN]:
                return None
            return raw[MAC_LEN + _S2C_HEADER_LEN :]

        if flags & _FLAG_UNENCRYPTED:
            if self._crypto_init_complete and raw[:MAC_LEN] != bytes(self._fake_signature):
                return None
            return raw[MAC_LEN + _S2C_HEADER_LEN :]

        # Try the current key mode first, then fall back to the other one.
        result = self._try_decrypt(raw, packet_type, packet_id, gen, force_dummy=False)
        if result is not None:
            return result
        return self._try_decrypt(raw, packet_type, packet_id, gen, force_dummy=True)

    def _try_decrypt(
        self,
        raw: bytes,
        packet_type: PacketType,
        packet_id: int,
        gen: int,
        *,
        force_dummy: bool,
    ) -> bytes | None:
        use_dummy = force_dummy or not self._crypto_init_complete
        if use_dummy:
            key, nonce = DUMMY_KEY, DUMMY_NONCE
        else:
            kn = self._get_key_nonce(True, packet_id, gen, packet_type)
            key, nonce = kn

        header = raw[MAC_LEN : MAC_LEN + _S2C_HEADER_LEN]
        mac = raw[:MAC_LEN]
        ciphertext = raw[MAC_LEN + _S2C_HEADER_LEN :]
        return eax_decrypt(key, nonce, header, ciphertext, mac, MAC_LEN)

    def _get_key_nonce(
        self, from_server: bool, packet_id: int, gen: int, packet_type: PacketType
    ) -> tuple[bytes, bytes]:
        if self._iv_struct is None:
            return DUMMY_KEY, DUMMY_NONCE
        kn = derive_key_nonce(
            from_server=from_server,
            packet_id=packet_id,
            generation_id=gen,
            packet_type=int(packet_type),
            iv_struct=self._iv_struct,
        )
        return kn.key, kn.nonce

    def _get_packet_counter(self, packet_type: PacketType) -> tuple[int, int]:
        if packet_type == PacketType.INIT1:
            return 101, 0
        idx = int(packet_type)
        return self._packet_counter[idx], self._generation_counter[idx]

    def _inc_packet_counter(self, packet_type: PacketType) -> None:
        if packet_type == PacketType.INIT1:
            return
        idx = int(packet_type)
        self._packet_counter[idx] = (self._packet_counter[idx] + 1) & 0xFFFF
        if self._packet_counter[idx] == 0:
            self._generation_counter[idx] = (self._generation_counter[idx] + 1) & 0xFFFFFFFF

    def _send_raw(self, raw: bytes) -> None:
        if self._transport is None or self._state == ClientState.DISCONNECTED:
            return
        self._transport.sendto(raw)

    # --- incoming ----------------------------------------------------------

    def _handle_incoming_packet(self, raw: bytes) -> None:
        if len(raw) < MAC_LEN + _S2C_HEADER_LEN:
            return

        packet_id = int.from_bytes(raw[MAC_LEN : MAC_LEN + 2], "big")
        pt_byte = raw[MAC_LEN + 2]
        try:
            packet_type = PacketType(pt_byte & 0x0F)
        except ValueError:
            return
        flags = pt_byte & 0xF0

        self._last_message_time = time.monotonic()

        gen = self._in_generation_counter[int(packet_type)]
        data = self._decrypt_packet(raw, packet_type, packet_id, gen, flags)
        if data is None:
            return

        if packet_type == PacketType.INIT1:
            self._handle_init(data)
        elif packet_type in (PacketType.COMMAND, PacketType.COMMAND_LOW):
            ack_type = PacketType.ACK if packet_type == PacketType.COMMAND else PacketType.ACK_LOW
            self._send_ack(packet_id, ack_type)
            self._handle_command_data(data, flags)
        elif packet_type == PacketType.ACK:
            self._handle_ack(data)
        elif packet_type == PacketType.PING:
            self._handle_ping(packet_id)
        elif (
            packet_type in (PacketType.VOICE, PacketType.VOICE_WHISPER)
            and self.on_voice is not None
        ):
            with self._guard():
                self.on_voice(data)

    def _send_ack(self, ack_id: int, ack_type: PacketType) -> None:
        self._send_outgoing(ack_id.to_bytes(2, "big"), ack_type)

    def _handle_ping(self, packet_id: int) -> None:
        self._send_outgoing(packet_id.to_bytes(2, "big"), PacketType.PONG)

    def _handle_ack(self, data: bytes) -> None:
        if len(data) < 2:
            return
        acked_id = int.from_bytes(data[:2], "big")
        self._resend_map.pop(acked_id, None)

    # --- init handshake ----------------------------------------------------

    def _handle_init(self, data: bytes) -> None:
        if len(data) < 1:
            return
        step = data[0]

        if step == 1:
            if len(data) < 21:
                return
            init2 = bytearray(4 + 1 + 16 + 4)
            init2[0:4] = INIT_VERSION.to_bytes(4, "big", signed=False)
            init2[4] = 0x02
            init2[5:25] = data[1:21]
            self._init_resend = None
            self._send_init_packet(bytes(init2))

        elif step == 3:
            if len(data) < 1 + 64 + 64 + 4 + 100:
                return
            self._state = ClientState.HANDSHAKE
            self._init_resend = None

            x = data[1:65]
            n = data[65:129]
            level = int.from_bytes(data[129:133], "big", signed=True)
            server_data = data[133:233]

            log.info("ts3.rsa_puzzle", level=level)
            x_big = int.from_bytes(x, "big")
            n_big = int.from_bytes(n, "big")
            y_big = pow(x_big, 1 << level, n_big)
            y = y_big.to_bytes(64, "big")

            self._alpha_tmp = os.urandom(10)
            import base64

            alpha = base64.b64encode(self._alpha_tmp).decode("ascii")
            assert self._opts is not None
            omega = self._opts.identity.public_key_string

            init_add = build_command(
                "clientinitiv", {"alpha": alpha, "omega": omega, "ot": 1, "ip": ""}
            )
            text_bytes = init_add.encode("utf-8")

            init4 = bytearray(4 + 1 + 64 + 64 + 4 + 100 + 64 + len(text_bytes))
            init4[0:4] = INIT_VERSION.to_bytes(4, "big", signed=False)
            init4[4] = 0x04
            init4[5:69] = x
            init4[69:133] = n
            init4[133:137] = level.to_bytes(4, "big", signed=True)
            init4[137:237] = server_data
            init4[237:301] = y
            init4[301 : 301 + len(text_bytes)] = text_bytes

            self._send_init_packet(bytes(init4))

        elif step == 0x7F:
            # Server requests restart from step 0.
            self._init_resend = None
            self._send_init_packet(self._build_init0())

    # --- command handling -------------------------------------------------

    def _handle_command_data(self, data: bytes, flags: int) -> None:
        if flags & _FLAG_FRAGMENTED:
            if not self._fragmenting:
                self._fragmenting = True
                self._fragment_flags = flags
                self._fragment_buffer = [bytes(data)]
            else:
                self._fragment_buffer.append(bytes(data))
                merged = b"".join(self._fragment_buffer)
                saved_flags = self._fragment_flags
                self._fragmenting = False
                self._fragment_flags = 0
                self._fragment_buffer = []
                self._process_command(merged, saved_flags)
            return

        if self._fragmenting:
            self._fragment_buffer.append(bytes(data))
            return

        self._process_command(data, flags)

    def _process_command(self, data: bytes, flags: int = 0) -> None:
        if flags & _FLAG_COMPRESSED:
            try:
                data = qlz_decompress(data)
            except Exception as exc:
                log.warning("ts3.decompress_failed", error=str(exc))
                return

        cmd_str = data.decode("utf-8", errors="replace")
        parsed = parse_command(cmd_str)

        if self.on_command is not None:
            with self._guard():
                self.on_command(parsed)

        name = parsed.name
        if name == "initivexpand":
            self._handle_initivexpand(parsed.params)
        elif name == "initivexpand2":
            assert self._loop is not None
            self._loop.create_task(self._handle_initivexpand2(parsed.params))
        elif name == "initserver":
            self._handle_initserver(parsed.params)
        elif name == "channellist":
            self._handle_channellist(parsed)
        elif name == "channellistfinished":
            self._handle_channellist_finished()
        elif name == "notifyclientleftview":
            clid = parsed.params.get("clid")
            if clid and int(clid) == self._client_id:
                self._cleanup()
        elif name == "notifytextmessage":
            if self.on_text_message is not None:
                with self._guard():
                    self.on_text_message(parsed.params)
        elif name == "error":
            if self.on_ts3error is not None:
                with self._guard():
                    self.on_ts3error(parsed.params)
            err_id = int(parsed.params.get("id") or "0")
            if err_id in _FATAL_TS3_ERRORS:
                msg = parsed.params.get("msg", "unknown error")
                self._emit_error(ConnectionError(f"TS3 error {err_id}: {msg}"))
                self.disconnect()

    # --- crypto handshake -------------------------------------------------

    def _handle_initivexpand(self, params: dict[str, str]) -> None:
        """Old crypto init: ECDH P-256 with the server's omega."""
        self._init_resend = None
        alpha_b64 = params.get("alpha")
        beta_b64 = params.get("beta")
        omega_b64 = params.get("omega")
        if not alpha_b64 or not beta_b64 or not omega_b64:
            self._emit_error(ValueError("missing initivexpand parameters"))
            return

        import base64

        beta_bytes = base64.b64decode(beta_b64)
        omega_bytes = base64.b64decode(omega_b64)

        assert self._opts is not None
        assert self._alpha_tmp is not None
        shared_key = get_shared_secret(self._opts.identity.private_key, omega_bytes)

        iv = bytearray(10 + len(beta_bytes))
        xor_into(iv, shared_key, 10)
        xor_into(iv, self._alpha_tmp, 10)
        for i in range(len(beta_bytes)):
            iv[10 + i] = shared_key[10 + i] ^ beta_bytes[i]
        self._iv_struct = bytes(iv)

        sig = sha1(self._iv_struct)
        self._fake_signature[:MAC_LEN] = sig[:MAC_LEN]
        self._crypto_init_complete = True
        self._alpha_tmp = None

        self._send_clientinit()

    async def _handle_initivexpand2(self, params: dict[str, str]) -> None:
        """New crypto init: Ed25519 license chain + temporary keypair."""
        if self._crypto_init_complete or self._alpha_tmp is None:
            return
        self._init_resend = None

        license_b64 = params.get("l")
        beta_b64 = params.get("beta")
        omega_b64 = params.get("omega")
        if not license_b64 or not beta_b64 or not omega_b64:
            self._emit_error(ValueError("missing initivexpand2 parameters"))
            return

        import base64

        try:
            beta_bytes = base64.b64decode(beta_b64)
            license_bytes = base64.b64decode(license_b64)

            blocks = parse_license(license_bytes)
            server_key = derive_license_key(blocks)

            temp = generate_temporary_key()
            ek_b64 = base64.b64encode(temp.public_key).decode("ascii")

            assert self._opts is not None
            to_sign = temp.public_key + beta_bytes
            priv = self._opts.identity.private_key
            sign_buf = priv.sign(to_sign, ec.ECDSA(hashes.SHA256()))
            proof_b64 = base64.b64encode(sign_buf).decode("ascii")

            client_ek_cmd = build_command("clientek", {"ek": ek_b64, "proof": proof_b64})
            self.send_command(client_ek_cmd)

            shared_data = get_shared_secret2(server_key, temp.private_key)
            iv = bytearray(shared_data)
            assert self._alpha_tmp is not None
            xor_into(iv, self._alpha_tmp, 10)
            # The TS reference XORs beta into iv[10:] in place; we mirror that.
            for i in range(len(beta_bytes)):
                iv[10 + i] ^= beta_bytes[i]
            self._iv_struct = bytes(iv)

            sig = sha1(self._iv_struct)
            self._fake_signature[:MAC_LEN] = sig[:MAC_LEN]
            self._crypto_init_complete = True
            self._alpha_tmp = None

            self._send_clientinit()
        except Exception as exc:
            self._emit_error(exc)

    # --- post-handshake ---------------------------------------------------

    def _send_clientinit(self) -> None:
        assert self._opts is not None
        ident = self._opts.identity
        params: dict[str, str | int | bool | None] = {
            "client_nickname": self._opts.nickname,
            "client_version": _VERSION_STRING,
            "client_platform": _VERSION_PLATFORM,
            "client_input_hardware": 1,
            "client_output_hardware": 1,
            "client_default_channel": self._opts.default_channel,
            "client_default_channel_password": (
                hash_password(self._opts.channel_password) if self._opts.channel_password else ""
            ),
            "client_server_password": (
                hash_password(self._opts.server_password) if self._opts.server_password else ""
            ),
            "client_meta_data": "",
            "client_version_sign": _VERSION_SIGN,
            "client_key_offset": str(ident.key_offset),
            "client_nickname_phonetic": "",
            "client_default_token": "",
            "hwid": "",
        }
        self.send_command(build_command("clientinit", params))

    def _handle_initserver(self, params: dict[str, str]) -> None:
        self._client_id = int(params.get("aclid") or 0)
        self._state = ClientState.CONNECTED
        log.info("ts3.connected", client_id=self._client_id)

        assert self._loop is not None
        self._ping_task = self._loop.create_task(self._ping_loop())

        self._connected_event.set()
        if self.on_connected is not None:
            with self._guard():
                self.on_connected()

    def _handle_channellist(self, parsed: ParsedCommand) -> None:
        entries = parsed.groups if parsed.groups is not None else [parsed.params]
        for entry in entries:
            try:
                cid = int(entry.get("cid") or 0)
            except ValueError:
                continue
            name = entry.get("channel_name")
            if cid and name:
                self._channel_map[name] = cid

    def _handle_channellist_finished(self) -> None:
        assert self._opts is not None
        target = self._opts.default_channel
        if not target or not self._client_id:
            return

        cid: int | None
        try:
            as_num = int(target)
            cid = as_num if str(as_num) == target else self._channel_map.get(target)
        except ValueError:
            cid = self._channel_map.get(target)

        if not cid:
            log.info("ts3.default_channel_not_found", target=target)
            return

        params: dict[str, str | int | bool | None] = {
            "cid": cid,
            "clid": self._client_id,
            "cpw": self._opts.channel_password,
        }
        log.info("ts3.moving_to_channel", target=target, cid=cid)
        self.send_command(build_command("clientmove", params))

    # --- timers -----------------------------------------------------------

    async def _resend_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                self._resend_tick()
        except asyncio.CancelledError:
            pass

    def _resend_tick(self) -> None:
        now = time.monotonic()

        if self._init_resend is not None:
            if now - self._init_resend.first_send > 30.0:
                self._emit_error(ConnectionError("TS3 init timeout"))
                self._cleanup()
                return
            if now - self._init_resend.last_send > 1.0:
                self._init_resend.last_send = now
                self._send_raw(self._init_resend.raw)

        for pkt in list(self._resend_map.values()):
            if now - pkt.first_send > 30.0:
                self._emit_error(ConnectionError(f"TS3 packet {pkt.packet_id} timeout"))
                self._cleanup()
                return
            if now - pkt.last_send > 1.0:
                pkt.last_send = now
                self._send_raw(pkt.raw)

        if now - self._last_message_time > 30.0:
            self._emit_error(ConnectionError("TS3 connection timeout - no response"))
            self._cleanup()

    async def _ping_loop(self) -> None:
        try:
            while self._state == ClientState.CONNECTED:
                await asyncio.sleep(1.0)
                if self._state == ClientState.CONNECTED:
                    self._send_outgoing(b"", PacketType.PING)
        except asyncio.CancelledError:
            pass

    # --- bookkeeping ------------------------------------------------------

    def _reset_state(self) -> None:
        self._state = ClientState.INIT
        self._crypto_init_complete = False
        self._iv_struct = None
        self._fake_signature = bytearray(MAC_LEN)
        self._resend_map.clear()
        self._init_resend = None
        for i in range(9):
            self._packet_counter[i] = 0
            self._generation_counter[i] = 0
            self._in_generation_counter[i] = 0
        self._fragment_buffer = []
        self._fragmenting = False
        self._fragment_flags = 0
        self._client_id = 0
        self._channel_map.clear()
        self._alpha_tmp = None

    def _cancel_tasks(self) -> None:
        if self._resend_task is not None:
            self._resend_task.cancel()
            self._resend_task = None
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None

    def _cleanup(self) -> None:
        self._state = ClientState.DISCONNECTED
        self._cancel_tasks()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self.on_disconnected is not None:
            with self._guard():
                self.on_disconnected()

    class _Suppressor:
        """Context manager that swallows + logs callback exceptions so a
        misbehaving consumer can't kill the client. Used for every
        callback dispatch."""

        def __enter__(self) -> None:
            return None

        def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
            if exc is not None:
                log.exception("ts3.callback_failed", error=str(exc))
            return True

    def _guard(self) -> Ts3Client._Suppressor:
        return self._Suppressor()


# --- helpers ---------------------------------------------------------------


class _Ts3DatagramProtocol(asyncio.DatagramProtocol):
    """Thin asyncio glue that pumps datagrams into the parent client."""

    def __init__(self, client: Ts3Client) -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        self._client._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        self._client._on_transport_error(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            self._client._on_transport_error(exc)


def load_identity_private_key(identity: Identity) -> ec.EllipticCurvePrivateKey:
    """Convenience: rebuild the cryptography private-key object from an
    Identity. Useful for callers that want to sign things outside the
    client (e.g., the WebRTC stream signaling)."""
    der = identity.private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return ec.derive_private_key(int.from_bytes(der, "big") % (1 << 256), ec.SECP256R1())


__all__ = [
    "ClientState",
    "PacketType",
    "Ts3Client",
    "Ts3ClientOptions",
]
