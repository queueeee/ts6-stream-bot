"""TS6 stream-signaling helper.

Ported from `clusterzx/ts6-manager` (MIT) - see
``packages/backend/src/voice/streaming/stream-signaling.ts``.

The TS6 server has a built-in stream feature: clients open an
``RTCPeerConnection`` to each other through the server, with SDP / ICE
relayed over a small set of TS3 commands. This module wires those
commands to the rest of our pipeline:

* Outbound: ``setupstream``, ``respondjoinstreamrequest``,
  ``streamsignaling``, ``stopstream``, ``removeclientfromstream``.
* Inbound notifications: ``notifystreamstarted``, ``notifystreamstopped``,
  ``notifyjoinstreamrequest``, ``notifyrespondjoinstreamrequest``,
  ``notifystreamsignaling``, ``notifystreamclientjoined``,
  ``notifystreamclientleft``, ``notifystreaminfo``.

The module itself doesn't speak WebRTC - it just hands SDP / ICE blobs
to a callback (typically the WebRTC stream publisher in phase 3.3).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from ts6_stream_bot.ts3lib.client import Ts3Client
from ts6_stream_bot.ts3lib.commands import ParsedCommand, build_command

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ActiveStream:
    """Represents a stream the server has acknowledged."""

    id: str
    clid: int
    name: str
    type: int
    access: int
    mode: int
    bitrate: int
    viewer_limit: int
    audio: bool


class SignalingType(StrEnum):
    OFFER = "offer"
    ANSWER = "answer"
    ICE_CANDIDATE = "ice_candidate"
    RECONNECT = "reconnect"
    STREAM_STARTED = "stream_started"
    STREAM_STOPPED = "stream_stopped"
    JOIN_RESPONSE = "join_response"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class SignalingMessage:
    type: SignalingType
    raw: str
    stream_id: str | None = None
    clid: int | None = None
    sdp: str | None = None
    sdp_mid: str | None = None
    sdp_mline_index: int | None = None
    candidate: str | None = None
    is_reconnect: bool = False
    stream: ActiveStream | None = None


class StreamSignaling:
    """Subscribe to a Ts3Client's command stream + send the outbound
    half of the TS6 stream protocol."""

    def __init__(self, client: Ts3Client) -> None:
        self._client = client
        self._active_streams: dict[str, ActiveStream] = {}

        # Public callbacks - assign before the first relevant command.
        self.on_stream_started: Callable[[ActiveStream], None] | None = None
        self.on_stream_stopped: Callable[[str, ActiveStream | None], None] | None = None
        self.on_join_stream_request: Callable[[dict[str, str]], None] | None = None
        self.on_stream_client_joined: Callable[[dict[str, str]], None] | None = None
        self.on_stream_client_left: Callable[[dict[str, str]], None] | None = None
        self.on_signaling_message: Callable[[SignalingMessage], None] | None = None

        # Tap the underlying TS3 client. We chain - prior on_command stays.
        self._prev_command_handler = client.on_command
        client.on_command = self._handle_command

    @property
    def active_streams(self) -> dict[str, ActiveStream]:
        """Snapshot of streams the server has confirmed."""
        return dict(self._active_streams)

    # --- inbound dispatch -------------------------------------------------

    def _handle_command(self, parsed: ParsedCommand) -> None:
        """Tap the TS3 command stream and dispatch the streaming bits."""
        try:
            self._dispatch(parsed)
        except Exception:
            log.exception("stream_signaling.dispatch_failed", name=parsed.name)
        if self._prev_command_handler is not None:
            try:
                self._prev_command_handler(parsed)
            except Exception:
                log.exception("stream_signaling.prev_handler_failed")

    def _dispatch(self, parsed: ParsedCommand) -> None:
        name = parsed.name
        if name == "notifystreamstarted":
            self._handle_stream_started(parsed.params)
        elif name == "notifystreamstopped":
            self._handle_stream_stopped(parsed.params)
        elif name == "notifystreamsignaling":
            self._handle_stream_signaling(parsed.params)
        elif name == "notifyjoinstreamrequest" and self.on_join_stream_request is not None:
            with _guard("on_join_stream_request"):
                self.on_join_stream_request(parsed.params)
        elif name == "notifyrespondjoinstreamrequest":
            self._handle_join_response(parsed.params)
        elif name == "notifystreamclientjoined" and self.on_stream_client_joined is not None:
            with _guard("on_stream_client_joined"):
                self.on_stream_client_joined(parsed.params)
        elif name == "notifystreamclientleft" and self.on_stream_client_left is not None:
            with _guard("on_stream_client_left"):
                self.on_stream_client_left(parsed.params)
        elif name == "notifystreaminfo":
            self._handle_stream_info(parsed.params)

    def _handle_stream_started(self, p: dict[str, str]) -> None:
        stream = self._stream_from_params(p, audio_key="audio")
        self._active_streams[stream.id] = stream
        if self.on_stream_started is not None:
            with _guard("on_stream_started"):
                self.on_stream_started(stream)
        if self.on_signaling_message is not None:
            with _guard("on_signaling_message"):
                self.on_signaling_message(
                    SignalingMessage(
                        type=SignalingType.STREAM_STARTED,
                        stream_id=stream.id,
                        clid=stream.clid,
                        raw=json.dumps(p),
                        stream=stream,
                    )
                )

    def _handle_stream_stopped(self, p: dict[str, str]) -> None:
        stream_id = p.get("id") or p.get("stream_id") or ""
        stream = self._active_streams.pop(stream_id, None)
        if self.on_stream_stopped is not None:
            with _guard("on_stream_stopped"):
                self.on_stream_stopped(stream_id, stream)
        if self.on_signaling_message is not None:
            try:
                clid = int(p.get("clid") or 0) or (stream.clid if stream else None)
            except ValueError:
                clid = stream.clid if stream else None
            with _guard("on_signaling_message"):
                self.on_signaling_message(
                    SignalingMessage(
                        type=SignalingType.STREAM_STOPPED,
                        stream_id=stream_id,
                        clid=clid,
                        raw=json.dumps(p),
                        stream=stream,
                    )
                )

    def _handle_stream_signaling(self, p: dict[str, str]) -> None:
        data_str = p.get("json") or p.get("data") or ""
        if not data_str or self.on_signaling_message is None:
            return

        try:
            payload = json.loads(data_str)
        except json.JSONDecodeError:
            log.warning("stream_signaling.bad_json", payload=data_str[:200])
            return

        cmd = payload.get("cmd")
        args: dict[str, Any] = payload.get("args") or {}
        clid = _maybe_int(p.get("clid"))
        stream_id = p.get("id") or p.get("stream_id")

        if cmd in ("offer", "reconnectOffer"):
            msg = SignalingMessage(
                type=SignalingType.OFFER,
                sdp=args.get("offer") or args.get("sdp"),
                is_reconnect=cmd == "reconnectOffer",
                clid=clid,
                stream_id=stream_id,
                raw=data_str,
            )
        elif cmd == "answer":
            msg = SignalingMessage(
                type=SignalingType.ANSWER,
                sdp=args.get("answer") or args.get("sdp"),
                clid=clid,
                stream_id=stream_id,
                raw=data_str,
            )
        elif cmd == "iceCandidate":
            msg = SignalingMessage(
                type=SignalingType.ICE_CANDIDATE,
                candidate=args.get("sdp") or args.get("candidate"),
                sdp_mid=args.get("mid") or args.get("sdp_mid"),
                sdp_mline_index=args.get("mLine", args.get("sdp_mline_index")),
                clid=clid,
                stream_id=stream_id,
                raw=data_str,
            )
        elif cmd == "reconnect":
            msg = SignalingMessage(
                type=SignalingType.RECONNECT,
                is_reconnect=True,
                clid=clid,
                stream_id=stream_id,
                raw=data_str,
            )
        else:
            return

        with _guard("on_signaling_message"):
            self.on_signaling_message(msg)

    def _handle_join_response(self, p: dict[str, str]) -> None:
        if self.on_signaling_message is None:
            return
        decision = _maybe_int(p.get("decision")) or 0
        clid = _maybe_int(p.get("clid"))
        stream_id = p.get("id") or p.get("stream_id")
        if decision == 1 and p.get("offer"):
            msg = SignalingMessage(
                type=SignalingType.OFFER,
                sdp=p["offer"],
                clid=clid,
                stream_id=stream_id,
                raw=json.dumps(p),
            )
        else:
            msg = SignalingMessage(
                type=SignalingType.JOIN_RESPONSE,
                clid=clid,
                stream_id=stream_id,
                raw=json.dumps(p),
            )
        with _guard("on_signaling_message"):
            self.on_signaling_message(msg)

    def _handle_stream_info(self, p: dict[str, str]) -> None:
        if "id" not in p:
            return
        stream = self._stream_from_params(p, audio_key="audio")
        # ``notifystreaminfo`` uses ``accessibility`` instead of ``access``.
        access_raw = p.get("accessibility") or p.get("access")
        stream.access = _maybe_int(access_raw) or 0
        self._active_streams[stream.id] = stream
        if self.on_stream_started is not None:
            with _guard("on_stream_started"):
                self.on_stream_started(stream)

    @staticmethod
    def _stream_from_params(p: dict[str, str], *, audio_key: str) -> ActiveStream:
        return ActiveStream(
            id=p.get("id", ""),
            clid=_maybe_int(p.get("clid")) or 0,
            name=p.get("name", ""),
            type=_maybe_int(p.get("type")) or 0,
            access=_maybe_int(p.get("access")) or 0,
            mode=_maybe_int(p.get("mode")) or 0,
            bitrate=_maybe_int(p.get("bitrate")) or 0,
            viewer_limit=_maybe_int(p.get("viewer_limit")) or 0,
            audio=p.get(audio_key) == "1",
        )

    # --- outbound commands ------------------------------------------------

    def register_stream_notifications(self) -> None:
        """Subscribe to the server-side notifications we need. Idempotent."""
        for event in ("channel", "server", "textchannel"):
            self._client.send_command(build_command("servernotifyregister", {"event": event}))

    def send_setup_stream(
        self,
        *,
        name: str = "Bot Stream",
        type: int = 3,
        bitrate: int = 4608,
        accessibility: int = 1,
        mode: int = 1,
        viewer_limit: int = 0,
        audio: bool = True,
    ) -> None:
        """Ask the server to allocate a stream for us. Server replies with
        ``notifystreamstarted`` once the allocation succeeds."""
        params: dict[str, str | int | bool | None] = {
            "name": name,
            "type": type,
            "bitrate": bitrate,
            "accessibility": accessibility,
            "mode": mode,
            "viewer_limit": viewer_limit,
            "audio": audio,
        }
        self._client.send_command(build_command("setupstream", params))

    def send_join_response(
        self, *, viewer_clid: int, stream_id: str, accept: bool, offer_sdp: str = ""
    ) -> None:
        """Respond to ``notifyjoinstreamrequest``: accept (with our offer
        SDP for the new viewer) or reject (decision = 0)."""
        params: dict[str, str | int | bool | None] = {
            "id": stream_id,
            "clid": viewer_clid,
            "msg": "",
            "offer": offer_sdp,
            "decision": 1 if accept else 0,
        }
        self._client.send_command(build_command("respondjoinstreamrequest", params))

    def send_signaling(
        self,
        *,
        target_clid: int,
        cmd: str,
        args: dict[str, Any],
        stream_id: str = "",
    ) -> None:
        """Send an SDP/ICE signaling message to a specific viewer. The wire
        format is JSON inside the ``json`` parameter of ``streamsignaling``."""
        payload = json.dumps({"cmd": cmd, "args": args})
        params: dict[str, str | int | bool | None] = {
            "id": stream_id,
            "clid": target_clid,
            "json": payload,
        }
        self._client.send_command(build_command("streamsignaling", params))

    def send_stream_stop(self, stream_id: str, *, reason: int = 1) -> None:
        self._client.send_command(build_command("stopstream", {"id": stream_id, "reason": reason}))

    def send_remove_client(self, *, viewer_clid: int, stream_id: str) -> None:
        self._client.send_command(
            build_command("removeclientfromstream", {"id": stream_id, "clid": viewer_clid})
        )


# --- helpers ---------------------------------------------------------------


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _guard:
    """Swallow + log callback exceptions, with the callback name in the log."""

    def __init__(self, callback_name: str) -> None:
        self._name = callback_name

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        if exc is not None:
            log.exception("stream_signaling.callback_failed", callback=self._name, error=str(exc))
        return True


__all__ = [
    "ActiveStream",
    "SignalingMessage",
    "SignalingType",
    "StreamSignaling",
]
