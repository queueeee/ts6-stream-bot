"""ParecAudioTrack tests.

Substitutes a Python emitter for ``parec`` so we can verify the track
hands out 20 ms / 48 kHz / stereo s16 audio frames without touching real
PulseAudio. The shape, sample rate, layout, and pts increments are what
aiortc actually cares about when fanning the track out to peers.
"""

from __future__ import annotations

import base64

import av
import pytest
from aiortc.mediastreams import MediaStreamError

from ts6_stream_bot.pipeline.parec_audio_track import (
    _PCM_BYTES_PER_FRAME,
    _SAMPLES_PER_FRAME,
    ParecAudioTrack,
)


def _emitter_argv(pcm: bytes) -> list[str]:
    """Stand-in for parec: write ``pcm`` to stdout and exit."""
    blob = base64.b64encode(pcm).decode("ascii")
    return [
        "python3",
        "-c",
        f"import sys, base64; sys.stdout.buffer.write(base64.b64decode({blob!r}))",
    ]


@pytest.mark.asyncio
async def test_recv_returns_audio_frame_with_correct_shape() -> None:
    track = ParecAudioTrack(
        source="ignored",
        capture_argv=_emitter_argv(b"\x00" * _PCM_BYTES_PER_FRAME * 3),
    )
    try:
        frame = await track.recv()
        assert isinstance(frame, av.AudioFrame)
        assert frame.sample_rate == 48000
        # AudioFrame with packed s16 stereo: 1 plane, samples * 2 channels.
        assert frame.format.name == "s16"
        assert frame.layout.name == "stereo"
        assert frame.samples == _SAMPLES_PER_FRAME
        assert frame.pts == 0
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_recv_increments_pts_per_frame() -> None:
    track = ParecAudioTrack(
        source="ignored",
        capture_argv=_emitter_argv(b"\x00" * _PCM_BYTES_PER_FRAME * 5),
    )
    try:
        pts_seen = [(await track.recv()).pts for _ in range(3)]
        assert pts_seen == [0, _SAMPLES_PER_FRAME, _SAMPLES_PER_FRAME * 2]
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_recv_after_emitter_exits_raises_media_stream_error() -> None:
    """When parec exits we surface MediaStreamError so aiortc closes the
    track cleanly instead of looping forever on an empty pipe."""
    track = ParecAudioTrack(
        source="ignored",
        capture_argv=_emitter_argv(b"\x00" * _PCM_BYTES_PER_FRAME),
    )
    try:
        await track.recv()  # consume the one available frame
        with pytest.raises(MediaStreamError):
            await track.recv()
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent_before_start() -> None:
    track = ParecAudioTrack(
        source="ignored",
        capture_argv=_emitter_argv(b""),
    )
    track.stop()
    track.stop()  # second call must not raise
    with pytest.raises(MediaStreamError):
        await track.recv()


@pytest.mark.asyncio
async def test_stop_terminates_emitter() -> None:
    """A long-running emitter must be killed by stop()."""
    forever = [
        "python3",
        "-c",
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'\\x00' * 3840)\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(0.02)\n",
    ]
    track = ParecAudioTrack(source="ignored", capture_argv=forever)
    await track.recv()  # ensure parec is up
    track.stop()
    # The track is now stopped and recv should refuse to start a new one.
    with pytest.raises(MediaStreamError):
        await track.recv()
