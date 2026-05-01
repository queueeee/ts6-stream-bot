"""StreamPublisher unit tests with mocked WebRTC + capture.

End-to-end with real X11 + PulseAudio + a TS6 server is operator
business; what we cover here is the publisher's signaling glue:

* ``start()`` waits for ``notifystreamstarted`` + records the stream id.
* Join flow: ``notifyjoinstreamrequest`` triggers ``createOffer`` +
  ``setLocalDescription`` + a ``respondjoinstreamrequest`` carrying the
  offer SDP.
* Answer flow: ``notifystreamsignaling`` with ``cmd=answer`` calls
  ``setRemoteDescription`` on the right peer.
* ICE flow: incoming candidates land on ``addIceCandidate`` with the
  parsed candidate object.
* Leave flow: ``notifystreamclientleft`` closes the matching peer and
  removes it from the viewer map.
* ``stop()`` kicks every viewer + sends ``stopstream`` + tears down
  capture.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ts6_stream_bot.pipeline.stream_publisher import StreamPublisher
from ts6_stream_bot.pipeline.stream_signaling import (
    ActiveStream,
    SignalingMessage,
    SignalingType,
    StreamSignaling,
)
from ts6_stream_bot.ts3lib.commands import ParsedCommand, parse_command

# --- fakes ----------------------------------------------------------------


@dataclass
class _FakeClient:
    sent: list[str]
    on_command: Callable[[ParsedCommand], None] | None = None
    client_id: int = 7  # we, the bot

    @classmethod
    def make(cls) -> _FakeClient:
        return cls(sent=[])

    def send_command(self, cmd: str) -> None:
        self.sent.append(cmd)


class _FakeTrack:
    kind = "video"

    def stop(self) -> None:
        return None


class _FakeCapture:
    """Stand-in for VideoCapture so tests don't touch ffmpeg."""

    def __init__(self) -> None:
        self.video_track: _FakeTrack | None = _FakeTrack()
        self.audio_track: _FakeTrack | None = _FakeTrack()
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _make_pc_mock() -> MagicMock:
    """Build an aiortc-shaped peer-connection mock that survives the
    publisher's offer/answer/ice/close calls without a real stack."""
    pc = MagicMock()
    pc.addTrack = MagicMock()
    pc.iceGatheringState = "complete"

    async def _create_offer():
        return MagicMock(sdp="OFFER_SDP", type="offer")

    async def _set_local(description):
        pc.localDescription = MagicMock(sdp="OFFER_SDP_WITH_CANDIDATES")

    async def _set_remote(description):
        pc.remoteDescription = description

    async def _add_ice(candidate):
        pc._ice = candidate

    async def _close():
        pc._closed = True

    pc.createOffer = _create_offer
    pc.setLocalDescription = _set_local
    pc.setRemoteDescription = AsyncMock(side_effect=_set_remote)
    pc.addIceCandidate = AsyncMock(side_effect=_add_ice)
    pc.close = AsyncMock(side_effect=_close)
    return pc


def _emit(client: _FakeClient, raw: str) -> None:
    assert client.on_command is not None
    client.on_command(parse_command(raw))


# --- fixture: publisher with mocked aiortc / capture --------------------


class _FakeBroadcaster:
    """Stand-in for VideoBroadcaster so the publisher tests don't need
    a real libvpx context. ``subscribe()`` returns a fresh sentinel
    object per call so each viewer's ``addTrack`` argument is unique
    and the multi-viewer regression test can count subscriptions."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.subscribe_calls = 0
        self.tracks: list[Any] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def subscribe(self) -> Any:
        self.subscribe_calls += 1
        track = MagicMock(name=f"broadcast-track-{self.subscribe_calls}")
        self.tracks.append(track)
        return track


@pytest.fixture
def wired(monkeypatch):
    client = _FakeClient.make()
    sig = StreamSignaling(client)  # type: ignore[arg-type]
    capture = _FakeCapture()
    broadcaster = _FakeBroadcaster()

    pc_mocks: list[MagicMock] = []

    def _factory(*args: Any, **kwargs: Any) -> MagicMock:
        pc = _make_pc_mock()
        pc_mocks.append(pc)
        return pc

    # Replace the imported RTCPeerConnection with our mock factory so the
    # publisher's `RTCPeerConnection()` calls return the mock.
    monkeypatch.setattr("ts6_stream_bot.pipeline.stream_publisher.RTCPeerConnection", _factory)

    publisher = StreamPublisher(  # type: ignore[arg-type]
        client=client,
        signaling=sig,
        capture=capture,
        video_broadcaster=broadcaster,
    )
    # Stub the MediaRelay so .subscribe() returns the source track verbatim.
    # Audio still flows through the relay; video bypasses it via the
    # broadcaster fake above.
    publisher._relay = MagicMock()
    publisher._relay.subscribe = MagicMock(side_effect=lambda track: track)
    return publisher, client, sig, capture, broadcaster, pc_mocks


# --- start ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_sends_setupstream_and_waits_for_started_notification(wired) -> None:
    publisher, client, sig, capture, _bc, _ = wired

    async def _trigger_started() -> None:
        await asyncio.sleep(0.01)
        sig.on_stream_started(
            ActiveStream(  # type: ignore[misc]
                id="stream-7",
                clid=7,
                name="My Stream",
                type=3,
                access=1,
                mode=1,
                bitrate=4608,
                viewer_limit=0,
                audio=True,
            )
        )

    asyncio.create_task(_trigger_started())  # noqa: RUF006 - test scope only
    sid = await publisher.start(name="My Stream", bitrate=4608, timeout=2.0)

    assert sid == "stream-7"
    assert publisher.is_streaming
    assert capture.started is True
    setup = parse_command(client.sent[0])
    assert setup.name == "setupstream"
    assert setup.params["name"] == "My Stream"


@pytest.mark.asyncio
async def test_start_ignores_started_for_other_clid(wired) -> None:
    """If the server emits notifystreamstarted for someone else's stream
    (shouldn't happen in practice, but be defensive), our start() must
    not latch onto it."""
    publisher, _client, sig, _capture, _bc, _ = wired

    async def _trigger_other_then_ours() -> None:
        await asyncio.sleep(0.01)
        sig.on_stream_started(
            ActiveStream(  # type: ignore[misc]
                id="other",
                clid=999,
                name="other",
                type=3,
                access=1,
                mode=1,
                bitrate=0,
                viewer_limit=0,
                audio=False,
            )
        )
        await asyncio.sleep(0.01)
        sig.on_stream_started(
            ActiveStream(  # type: ignore[misc]
                id="ours",
                clid=7,
                name="ours",
                type=3,
                access=1,
                mode=1,
                bitrate=0,
                viewer_limit=0,
                audio=True,
            )
        )

    asyncio.create_task(_trigger_other_then_ours())  # noqa: RUF006 - test scope only
    sid = await publisher.start(timeout=2.0)
    assert sid == "ours"


# --- join flow -----------------------------------------------------------


@pytest.mark.asyncio
async def test_join_request_creates_pc_and_sends_offer(wired) -> None:
    publisher, client, _sig, capture, _bc, pcs = wired

    # Skip start(); set the stream id directly.
    publisher._stream_id = "s1"
    capture.started = True

    _emit(client, "notifyjoinstreamrequest id=s1 clid=42")
    # Let the publisher's create_task drain.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs and "respondjoinstreamrequest" in "".join(client.sent):
            break

    assert len(pcs) == 1
    pc = pcs[0]
    # Video now comes from the broadcaster (one libvpx instance for the
    # whole stream); audio still flows through the per-PC MediaRelay
    # subscription so each viewer's Opus encoder reads its own queue
    # rather than racing on the parec subprocess.
    assert _bc.subscribe_calls == 1
    pc.addTrack.assert_any_call(_bc.tracks[0])
    pc.addTrack.assert_any_call(capture.audio_track)
    assert publisher._relay.subscribe.call_count == 1  # type: ignore[attr-defined]

    join_resp = next(
        parse_command(c) for c in client.sent if c.startswith("respondjoinstreamrequest")
    )
    assert join_resp.params["clid"] == "42"
    assert join_resp.params["decision"] == "1"
    assert join_resp.params["offer"] == "OFFER_SDP_WITH_CANDIDATES"
    assert 42 in publisher._viewers


@pytest.mark.asyncio
async def test_two_viewers_each_get_their_own_track_subscription(wired) -> None:
    """Multi-viewer regression: a second join must NOT reuse the first
    viewer's track. Video gets one broadcaster.subscribe() per viewer
    (each viewer reads its own per-subscriber queue of av.Packets);
    audio stays with MediaRelay.subscribe() so each Opus encoder reads
    its own queue rather than racing on the shared parec recv().
    This is what was crashing the live deploy with 'readexactly()
    called while another coroutine is already waiting for incoming data'."""
    publisher, client, _sig, _capture, _bc, pcs = wired
    publisher._stream_id = "s1"

    _emit(client, "notifyjoinstreamrequest id=s1 clid=42")
    _emit(client, "notifyjoinstreamrequest id=s1 clid=43")
    for _ in range(40):
        await asyncio.sleep(0.01)
        if len(pcs) >= 2 and len(publisher._viewers) >= 2:
            break

    assert len(pcs) == 2, f"expected 2 PCs, got {len(pcs)}"
    # One broadcaster subscription per viewer, plus one relay subscription
    # per viewer for audio.
    assert _bc.subscribe_calls == 2
    assert publisher._relay.subscribe.call_count == 2  # type: ignore[attr-defined]


# --- answer flow ---------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_calls_set_remote_description(wired) -> None:
    publisher, client, sig, _capture, _bc, pcs = wired
    publisher._stream_id = "s1"

    _emit(client, "notifyjoinstreamrequest id=s1 clid=42")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs:
            break

    # Now feed the answer through the signaling layer.
    sig.on_signaling_message(  # type: ignore[misc]
        SignalingMessage(type=SignalingType.ANSWER, sdp="ANSWER_SDP", clid=42, raw="")
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs[0].setRemoteDescription.await_count > 0:
            break

    pcs[0].setRemoteDescription.assert_awaited_once()
    desc = pcs[0].setRemoteDescription.await_args.args[0]
    assert desc.sdp == "ANSWER_SDP"
    assert desc.type == "answer"


# --- ICE -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ice_candidate_is_forwarded_to_pc(wired, monkeypatch) -> None:
    publisher, client, sig, _capture, _bc, pcs = wired
    publisher._stream_id = "s1"

    # Patch candidate_from_sdp so we don't have to construct a real
    # well-formed candidate string.
    fake_candidate = MagicMock()
    monkeypatch.setattr(
        "ts6_stream_bot.pipeline.stream_publisher.candidate_from_sdp",
        lambda s: fake_candidate,
    )

    _emit(client, "notifyjoinstreamrequest id=s1 clid=42")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs:
            break

    # ICE candidates now wait for the answer to be applied (so aiortc's
    # "addIceCandidate called without remote description" reject doesn't
    # eat them). Send the answer first - that flips the per-viewer event
    # which unblocks queued ICE work.
    sig.on_signaling_message(  # type: ignore[misc]
        SignalingMessage(type=SignalingType.ANSWER, sdp="ANSWER_SDP", clid=42, raw="")
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs[0].setRemoteDescription.await_count > 0:
            break

    sig.on_signaling_message(  # type: ignore[misc]
        SignalingMessage(
            type=SignalingType.ICE_CANDIDATE,
            candidate="candidate:abc",
            sdp_mid="0",
            sdp_mline_index=0,
            clid=42,
            raw="",
        )
    )
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs[0].addIceCandidate.await_count > 0:
            break

    pcs[0].addIceCandidate.assert_awaited_once_with(fake_candidate)
    assert fake_candidate.sdpMid == "0"
    assert fake_candidate.sdpMLineIndex == 0


@pytest.mark.asyncio
async def test_ice_candidate_arriving_before_answer_is_buffered(wired, monkeypatch) -> None:
    """Regression for the live deploy where every ICE candidate arrived
    before the answer task finished and aiortc dropped the call. Now
    candidates wait for the answer via the per-viewer remote_set event."""
    publisher, client, sig, _capture, _bc, pcs = wired
    publisher._stream_id = "s1"
    fake_candidate = MagicMock()
    monkeypatch.setattr(
        "ts6_stream_bot.pipeline.stream_publisher.candidate_from_sdp",
        lambda s: fake_candidate,
    )

    _emit(client, "notifyjoinstreamrequest id=s1 clid=42")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs:
            break

    # ICE arrives FIRST (the racy ordering from the live trace).
    sig.on_signaling_message(  # type: ignore[misc]
        SignalingMessage(
            type=SignalingType.ICE_CANDIDATE,
            candidate="candidate:abc",
            sdp_mid="0",
            sdp_mline_index=0,
            clid=42,
            raw="",
        )
    )
    # Give the task a chance to run; it should NOT call addIceCandidate yet.
    for _ in range(5):
        await asyncio.sleep(0.01)
    assert pcs[0].addIceCandidate.await_count == 0

    # Now the answer lands and unblocks the queued candidate.
    sig.on_signaling_message(  # type: ignore[misc]
        SignalingMessage(type=SignalingType.ANSWER, sdp="ANSWER_SDP", clid=42, raw="")
    )
    for _ in range(40):
        await asyncio.sleep(0.01)
        if pcs[0].addIceCandidate.await_count > 0:
            break

    assert pcs[0].addIceCandidate.await_count == 1


# --- leave + stop --------------------------------------------------------


@pytest.mark.asyncio
async def test_client_left_closes_pc_and_drops_viewer(wired) -> None:
    publisher, client, _sig, _capture, _bc, pcs = wired
    publisher._stream_id = "s1"

    _emit(client, "notifyjoinstreamrequest id=s1 clid=42")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if pcs:
            break

    _emit(client, "notifystreamclientleft clid=42")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if 42 not in publisher._viewers:
            break

    assert 42 not in publisher._viewers
    pcs[0].close.assert_awaited()


@pytest.mark.asyncio
async def test_stop_kicks_all_viewers_and_sends_stopstream(wired) -> None:
    publisher, client, _sig, capture, _bc, pcs = wired
    publisher._stream_id = "s1"

    _emit(client, "notifyjoinstreamrequest id=s1 clid=42")
    _emit(client, "notifyjoinstreamrequest id=s1 clid=43")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(pcs) >= 2 and len(publisher._viewers) >= 2:
            break

    await publisher.stop()

    sent_names = [parse_command(c).name for c in client.sent]
    assert "removeclientfromstream" in sent_names
    assert "stopstream" in sent_names
    for pc in pcs:
        pc.close.assert_awaited()
    assert capture.stopped is True
    assert publisher._stream_id is None
    assert len(publisher._viewers) == 0


@pytest.mark.asyncio
async def test_stop_without_start_only_stops_capture(wired) -> None:
    publisher, _client, _sig, capture, _bc, _ = wired
    await publisher.stop()
    assert capture.stopped is True
    assert publisher._stream_id is None


# --- status surface ------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reflects_state(wired) -> None:
    publisher, _, _, _, _bc, _pcs = wired
    s = publisher.status()
    assert s.streaming is False
    assert s.viewer_count == 0

    publisher._stream_id = "s1"
    publisher._viewers[1] = MagicMock(clid=1)
    publisher._viewers[2] = MagicMock(clid=2)
    s = publisher.status()
    assert s.streaming is True
    assert s.viewer_count == 2
    assert s.viewers == [1, 2]
