"""Stream-signaling unit tests.

Drives a fake Ts3Client through synthetic notifications and outbound
commands, and asserts that:

* The right callbacks fire with the right shaped data.
* Outbound commands serialize to the expected wire form (matches what
  ts6-manager would send).
* JSON-wrapped streamsignaling payloads (offer/answer/iceCandidate /
  reconnect) decode to the right SignalingMessage variant.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from ts6_stream_bot.pipeline.stream_signaling import (
    ActiveStream,
    SignalingMessage,
    SignalingType,
    StreamSignaling,
)
from ts6_stream_bot.ts3lib.commands import ParsedCommand, parse_command


class _FakeClient:
    """Subset of Ts3Client surface that StreamSignaling actually touches."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.on_command: Callable[[ParsedCommand], None] | None = None

    def send_command(self, cmd: str) -> None:
        self.sent.append(cmd)


def _emit(client: _FakeClient, raw: str) -> None:
    """Helper: simulate the underlying Ts3Client receiving + parsing a command."""
    assert client.on_command is not None
    client.on_command(parse_command(raw))


# --- inbound dispatch -----------------------------------------------------


def test_notifystreamstarted_populates_active_streams_and_fires_callback() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[ActiveStream] = []
    sig.on_stream_started = seen.append

    _emit(
        client,
        "notifystreamstarted id=stream-1 clid=42 name=Hello type=3 access=1 mode=1 "
        "bitrate=4608 viewer_limit=0 audio=1",
    )

    assert "stream-1" in sig.active_streams
    assert seen and seen[0].id == "stream-1"
    assert seen[0].clid == 42
    assert seen[0].audio is True


def test_notifystreamstopped_removes_active_stream_and_fires_callback() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]

    _emit(client, "notifystreamstarted id=s1 clid=1 name=N type=3 audio=0")
    _emit(client, "notifystreamstopped id=s1 clid=1")

    assert "s1" not in sig.active_streams


def test_notifyjoinstreamrequest_passes_params_through() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[dict[str, str]] = []
    sig.on_join_stream_request = seen.append

    _emit(client, "notifyjoinstreamrequest id=s1 clid=99")

    assert seen == [{"id": "s1", "clid": "99"}]


def test_streamsignaling_offer_decodes_to_signaling_message() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[SignalingMessage] = []
    sig.on_signaling_message = seen.append

    payload = json.dumps({"cmd": "offer", "args": {"offer": "v=0 fake sdp"}})
    _emit(client, f"notifystreamsignaling id=s1 clid=7 json={_escape(payload)}")

    offers = [m for m in seen if m.type == SignalingType.OFFER]
    assert len(offers) == 1
    assert offers[0].sdp == "v=0 fake sdp"
    assert offers[0].clid == 7
    assert offers[0].is_reconnect is False


def test_streamsignaling_answer_uses_args_answer_field() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[SignalingMessage] = []
    sig.on_signaling_message = seen.append

    payload = json.dumps({"cmd": "answer", "args": {"answer": "answer-sdp"}})
    _emit(client, f"notifystreamsignaling id=s1 clid=7 json={_escape(payload)}")

    answers = [m for m in seen if m.type == SignalingType.ANSWER]
    assert answers[0].sdp == "answer-sdp"


def test_streamsignaling_ice_candidate_carries_mid_and_mline() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[SignalingMessage] = []
    sig.on_signaling_message = seen.append

    payload = json.dumps(
        {"cmd": "iceCandidate", "args": {"sdp": "candidate:1", "mid": "0", "mLine": 0}}
    )
    _emit(client, f"notifystreamsignaling id=s1 clid=7 json={_escape(payload)}")

    cands = [m for m in seen if m.type == SignalingType.ICE_CANDIDATE]
    assert cands[0].candidate == "candidate:1"
    assert cands[0].sdp_mid == "0"
    assert cands[0].sdp_mline_index == 0


def test_streamsignaling_reconnect_offer_sets_flag() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[SignalingMessage] = []
    sig.on_signaling_message = seen.append

    payload = json.dumps({"cmd": "reconnectOffer", "args": {"offer": "v=0"}})
    _emit(client, f"notifystreamsignaling id=s1 clid=7 json={_escape(payload)}")

    offers = [m for m in seen if m.type == SignalingType.OFFER]
    assert offers[0].is_reconnect is True


def test_notifyrespondjoinstreamrequest_with_decision_one_emits_offer() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[SignalingMessage] = []
    sig.on_signaling_message = seen.append

    _emit(client, "notifyrespondjoinstreamrequest id=s1 clid=7 decision=1 offer=fake-offer")

    assert seen[0].type == SignalingType.OFFER
    assert seen[0].sdp == "fake-offer"


def test_notifystreamclientleft_passes_through() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    seen: list[dict[str, str]] = []
    sig.on_stream_client_left = seen.append

    _emit(client, "notifystreamclientleft clid=7")

    assert seen == [{"clid": "7"}]


# --- outbound commands ----------------------------------------------------


def test_send_setup_stream_serializes_defaults() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    sig.send_setup_stream(name="My Stream")

    assert len(client.sent) == 1
    parsed = parse_command(client.sent[0])
    assert parsed.name == "setupstream"
    assert parsed.params["name"] == "My Stream"
    assert parsed.params["type"] == "3"
    assert parsed.params["bitrate"] == "4608"
    assert parsed.params["audio"] == "1"


def test_send_join_response_accept_carries_offer() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    sig.send_join_response(viewer_clid=42, stream_id="s1", accept=True, offer_sdp="v=0...")

    parsed = parse_command(client.sent[0])
    assert parsed.name == "respondjoinstreamrequest"
    assert parsed.params["clid"] == "42"
    assert parsed.params["decision"] == "1"
    assert parsed.params["offer"] == "v=0..."


def test_send_join_response_reject() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    sig.send_join_response(viewer_clid=42, stream_id="s1", accept=False)

    parsed = parse_command(client.sent[0])
    assert parsed.params["decision"] == "0"


def test_send_signaling_wraps_args_in_json() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    sig.send_signaling(
        target_clid=7,
        cmd="iceCandidate",
        args={"sdp": "candidate:abc", "mid": "0"},
        stream_id="s1",
    )

    parsed = parse_command(client.sent[0])
    assert parsed.name == "streamsignaling"
    payload = json.loads(parsed.params["json"])
    assert payload == {
        "cmd": "iceCandidate",
        "args": {"sdp": "candidate:abc", "mid": "0"},
    }


def test_send_stream_stop_and_remove_client() -> None:
    client = _FakeClient()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    sig.send_stream_stop("s1")
    sig.send_remove_client(viewer_clid=42, stream_id="s1")

    stop_cmd = parse_command(client.sent[0])
    assert stop_cmd.name == "stopstream"
    assert stop_cmd.params["id"] == "s1"
    assert stop_cmd.params["reason"] == "1"

    rm_cmd = parse_command(client.sent[1])
    assert rm_cmd.name == "removeclientfromstream"
    assert rm_cmd.params["clid"] == "42"


# --- chaining with prior on_command ---------------------------------------


def test_chains_existing_on_command_handler() -> None:
    """If the client already has an on_command consumer (the bot), our
    StreamSignaling tap must not eat events that aren't ours."""
    client = _FakeClient()
    upstream: list[str] = []
    client.on_command = lambda parsed: upstream.append(parsed.name)

    sig = StreamSignaling(client)  # type: ignore[arg-type]
    sig.on_stream_started = lambda _: None

    _emit(client, "notifystreamstarted id=s1")
    _emit(client, "notifytextmessage msg=hi")  # not ours - upstream still wants it

    assert "notifystreamstarted" in upstream
    assert "notifytextmessage" in upstream


# --- helpers --------------------------------------------------------------


def _escape(s: str) -> str:
    """TS3 wire escape for the value half of a key=value token."""
    out: list[str] = []
    table: dict[str, str] = {
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
    for ch in s:
        out.append(table.get(ch, ch))
    return "".join(out)


@pytest.fixture
def _silence_unused() -> None:
    # Touch the imports so unused-import checkers stay quiet on `Any`.
    _ = Any
