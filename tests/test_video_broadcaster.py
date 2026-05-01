"""Tests for the single-encoder video broadcaster.

The broadcaster's job is to encode raw frames once and fan the resulting
``av.Packet``s out to N per-viewer queues. The expensive part (libvpx)
runs in a real test below to catch breakage; the rest of the surface
(subscription, drop-on-slow-consumer, sentinel-on-shutdown) is checked
with synthetic packets so the unit tests stay fast.
"""

from __future__ import annotations

import asyncio
from fractions import Fraction
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av.video.frame import VideoFrame

from ts6_stream_bot.pipeline.video_broadcaster import (
    BroadcastVideoTrack,
    VideoBroadcaster,
    VideoBroadcasterConfig,
)


def _make_config(**overrides: Any) -> VideoBroadcasterConfig:
    base = {
        "bitrate": 500_000,
        "width": 320,
        "height": 240,
        "framerate": 15,
        "queue_size": 4,
    }
    base.update(overrides)
    return VideoBroadcasterConfig(**base)


def _synthetic_frame(width: int = 320, height: int = 240, pts: int = 0) -> VideoFrame:
    """Build a tiny YUV420p frame in pure numpy. Avoids decoding a real
    video file from disk - tests stay self-contained."""
    arr = np.zeros((height * 3 // 2, width), dtype=np.uint8)
    frame = VideoFrame.from_ndarray(arr, format="yuv420p")
    frame.pts = pts
    frame.time_base = Fraction(1, 90000)
    return frame


class _FrameSource(MediaStreamTrack):
    """Test track that emits a fixed number of synthetic frames then
    raises MediaStreamError. Mirrors how the real x11grab MediaPlayer
    behaves at end-of-input."""

    kind = "video"

    def __init__(self, count: int, *, width: int = 320, height: int = 240) -> None:
        super().__init__()
        self._count = count
        self._emitted = 0
        self._width = width
        self._height = height

    async def recv(self) -> VideoFrame:
        if self._emitted >= self._count:
            raise MediaStreamError
        frame = _synthetic_frame(self._width, self._height, pts=self._emitted * 6000)
        self._emitted += 1
        return frame


# --- subscription mechanics ----------------------------------------------


async def test_subscribe_returns_track_with_video_kind() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config())
    track = bc.subscribe()
    assert isinstance(track, BroadcastVideoTrack)
    assert track.kind == "video"


async def test_subscribe_forces_keyframe_flag() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config())
    bc._force_keyframe = False
    bc.subscribe()
    assert bc._force_keyframe is True


async def test_unsubscribe_removes_subscriber() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config())
    track = bc.subscribe()
    assert len(bc._subscribers) == 1
    track.stop()
    assert len(bc._subscribers) == 0


# --- recv() / sentinel ---------------------------------------------------


async def test_recv_returns_queued_packet() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config())
    track = bc.subscribe()
    sentinel = MagicMock(name="packet")
    bc._subscribers[0].queue.put_nowait(sentinel)
    got = await track.recv()
    assert got is sentinel


async def test_recv_raises_media_stream_error_on_none_sentinel() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config())
    track = bc.subscribe()
    bc._subscribers[0].queue.put_nowait(None)
    with pytest.raises(MediaStreamError):
        await track.recv()


# --- fanout / slow-consumer behaviour ------------------------------------


def test_fanout_distributes_packet_to_all_subscribers() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config())
    a = bc.subscribe()
    b = bc.subscribe()
    pkt = MagicMock(name="packet")

    bc._fanout(pkt)

    assert bc._subscribers[0].queue.get_nowait() is pkt
    assert bc._subscribers[1].queue.get_nowait() is pkt
    # Sanity: tracks are distinct, queues are distinct.
    assert a is not b


def test_full_queue_drops_oldest_and_increments_drop_counter() -> None:
    """One slow viewer must not stall the rest. We drop the oldest packet
    in their personal queue when it's full."""
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config(queue_size=2))
    bc.subscribe()

    p0, p1, p2 = (MagicMock(name=f"p{i}") for i in range(3))

    bc._fanout(p0)
    bc._fanout(p1)
    bc._fanout(p2)  # queue is full when this lands

    sub = bc._subscribers[0]
    # Oldest (p0) was evicted to make room for p2; p1 is still there.
    remaining = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    assert remaining == [p1, p2]
    assert sub.drops == 1


def test_one_slow_subscriber_doesnt_starve_a_fast_one() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config(queue_size=1))
    bc.subscribe()  # slow viewer (we'll never drain)
    bc.subscribe()  # fast viewer (we'll drain each tick)

    fast_packets: list[MagicMock] = []
    for i in range(5):
        pkt = MagicMock(name=f"pkt{i}")
        bc._fanout(pkt)
        fast_packets.append(bc._subscribers[1].queue.get_nowait())

    assert fast_packets == [m for m in fast_packets]  # 5 deliveries
    assert len(fast_packets) == 5
    # Slow viewer's queue still has the latest, and it's exactly the bound.
    assert bc._subscribers[0].queue.qsize() == 1


# --- end-to-end pump (real libvpx) ---------------------------------------


async def test_pump_real_encoder_produces_packets_for_subscribers() -> None:
    """Smoke test against actual libvpx via PyAV. Doesn't validate the
    bytes - we just confirm the full path encode -> fanout -> recv()
    delivers a Packet to every subscriber. If this regresses we've
    broken pre-encoded mode and aiortc's RTCRtpSender will fall back
    to wanting frames."""
    bc = VideoBroadcaster(lambda: _FrameSource(10), _make_config())
    track_a = bc.subscribe()
    track_b = bc.subscribe()

    await bc.start()

    async def first_packet(track: BroadcastVideoTrack) -> Any:
        return await asyncio.wait_for(track.recv(), timeout=5.0)

    pkt_a, pkt_b = await asyncio.gather(first_packet(track_a), first_packet(track_b))

    # Real av.Packet, with bytes available - that's what RTCRtpSender.pack expects.
    assert pkt_a is not None
    assert pkt_b is not None
    assert len(bytes(pkt_a)) > 0
    assert len(bytes(pkt_b)) > 0

    await bc.stop()


async def test_stop_wakes_pending_recv_with_sentinel() -> None:
    bc = VideoBroadcaster(lambda: _FrameSource(0), _make_config())
    track = bc.subscribe()
    await bc.start()

    # No frames will arrive (source is empty); stop should wake recv().
    recv_task = asyncio.create_task(track.recv())
    await asyncio.sleep(0.05)
    await bc.stop()

    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(recv_task, timeout=2.0)


async def test_start_raises_when_factory_returns_none() -> None:
    bc = VideoBroadcaster(lambda: None, _make_config())
    with pytest.raises(RuntimeError):
        await bc.start()
