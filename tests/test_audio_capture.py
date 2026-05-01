"""AudioCapture tests.

Two layers:

1. Pure encoder tests (``_encode_and_dispatch``): drive synthetic PCM
   buffers through PyAV's libopus encoder and verify packets show up
   on the callback. No subprocess needed.
2. End-to-end with a substitute capture process: replace ``parec`` with
   a small Python emitter that writes a fixed amount of PCM bytes and
   exits, so we can assert the lifecycle without a real PulseAudio
   sink.
"""

from __future__ import annotations

import asyncio
import struct

import av
import numpy as np
import pytest

from ts6_stream_bot.pipeline.audio_capture import (
    OPUS_SAMPLE_RATE,
    PCM_BYTES_PER_FRAME,
    SAMPLES_PER_FRAME,
    AudioCapture,
)

# --- helpers ---------------------------------------------------------------


def _silence_frame() -> bytes:
    return b"\x00" * PCM_BYTES_PER_FRAME


def _sine_frame(freq_hz: float = 440.0, amplitude: float = 0.3) -> bytes:
    """Build one 20ms stereo s16-le frame containing a sine wave."""
    t = np.arange(SAMPLES_PER_FRAME) / OPUS_SAMPLE_RATE
    samples = (np.sin(2 * np.pi * freq_hz * t) * amplitude * 32767).astype(np.int16)
    interleaved = np.repeat(samples, 2)  # mono -> stereo (L=R)
    return interleaved.tobytes()


def _build_emitter_argv(pcm: bytes) -> list[str]:
    """Spawn a tiny Python emitter that writes ``pcm`` to stdout and exits.
    Stand-in for ``parec`` in lifecycle tests."""
    py = (
        "import sys, base64;"
        f"sys.stdout.buffer.write(base64.b64decode({base64_of(pcm)!r}));"
        "sys.stdout.buffer.flush()"
    )
    return ["python3", "-c", py]


def base64_of(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


# --- encoder unit tests ---------------------------------------------------


def test_encoder_produces_packet_for_silence() -> None:
    """A 20ms silent frame round-trips through libopus to a tiny packet
    (silence frames are typically 3-5 bytes)."""
    seen: list[bytes] = []
    cap = AudioCapture(sink_monitor="x", on_opus_frame=seen.append)
    cap._encoder = cap._build_encoder()
    cap._encode_and_dispatch(_silence_frame())
    # Encoder output is one packet per 20ms frame.
    assert len(seen) >= 1
    assert all(0 < len(p) < 200 for p in seen), [len(p) for p in seen]


def test_encoder_produces_larger_packet_for_signal() -> None:
    """A 440 Hz tone should encode to a meaningfully larger packet than
    silence - serves as a sanity check that we're feeding non-zero PCM
    through libopus, not just zeros."""
    silence_pkts: list[bytes] = []
    tone_pkts: list[bytes] = []

    silent_cap = AudioCapture(sink_monitor="x", on_opus_frame=silence_pkts.append)
    silent_cap._encoder = silent_cap._build_encoder()
    silent_cap._encode_and_dispatch(_silence_frame())

    tone_cap = AudioCapture(sink_monitor="x", on_opus_frame=tone_pkts.append)
    tone_cap._encoder = tone_cap._build_encoder()
    tone_cap._encode_and_dispatch(_sine_frame())

    silence_size = sum(len(p) for p in silence_pkts)
    tone_size = sum(len(p) for p in tone_pkts)
    assert tone_size > silence_size * 2, (silence_size, tone_size)


def test_encoder_round_trip_silence_decodes_to_silence() -> None:
    """End-to-end: encode silence with our pipeline, decode with a fresh
    libopus decoder, the result should still be ~silence (small dither)."""
    pkts: list[bytes] = []
    cap = AudioCapture(sink_monitor="x", on_opus_frame=pkts.append)
    cap._encoder = cap._build_encoder()

    # Push 5 frames so the encoder has settled.
    for _ in range(5):
        cap._encode_and_dispatch(_silence_frame())

    decoder = av.codec.CodecContext.create("libopus", "r")
    decoder.sample_rate = OPUS_SAMPLE_RATE
    decoder.layout = "stereo"
    decoder.format = "s16"

    decoded_samples = 0
    peak = 0
    for raw in pkts:
        for f in decoder.decode(av.Packet(raw)):
            arr = f.to_ndarray()
            decoded_samples += arr.shape[1] // 2  # samples per channel
            peak = max(peak, int(np.max(np.abs(arr))))

    assert decoded_samples > 0
    assert peak < 100, f"silence decoded with peak {peak}"


# --- lifecycle integration ------------------------------------------------


@pytest.mark.asyncio
async def test_start_consumes_emitter_and_produces_opus() -> None:
    """End-to-end with a substitute capture process: emit 10 frames of
    PCM, verify we get matching Opus packets out."""
    pcm_blob = b"".join(_silence_frame() for _ in range(10))

    seen: list[bytes] = []
    cap = AudioCapture(
        sink_monitor="ignored",
        on_opus_frame=seen.append,
        capture_argv=_build_emitter_argv(pcm_blob),
    )
    await cap.start()
    # Wait for the emitter to finish + the read loop to drain.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if not cap.is_running:
            break
    await cap.stop()

    assert len(seen) >= 10, f"got only {len(seen)} packets"


@pytest.mark.asyncio
async def test_stop_is_idempotent_when_never_started() -> None:
    """``stop()`` on a fresh instance should be a no-op, not raise."""
    cap = AudioCapture(sink_monitor="x", on_opus_frame=lambda _: None)
    await cap.stop()


@pytest.mark.asyncio
async def test_stop_terminates_long_running_emitter() -> None:
    """An emitter that would otherwise run forever must be terminated by
    ``stop()`` within the 2s grace window."""
    forever_argv = [
        "python3",
        "-c",
        "import sys, time;buf = b'\\x00' * 3840;  ".strip() + "\nimport sys, time;"
        "while True:\n"
        "    sys.stdout.buffer.write(b'\\x00' * 3840)\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(0.02)\n",
    ]
    cap = AudioCapture(
        sink_monitor="x",
        on_opus_frame=lambda _: None,
        capture_argv=forever_argv,
    )
    await cap.start()
    await asyncio.sleep(0.1)
    await cap.stop()
    assert not cap.is_running


def test_constants_are_consistent() -> None:
    # 20ms at 48kHz, stereo, s16: 960 samples per channel * 2 channels * 2 bytes = 3840.
    assert PCM_BYTES_PER_FRAME == SAMPLES_PER_FRAME * 2 * 2
    assert SAMPLES_PER_FRAME == 960
    # struct sanity - one 20ms frame is 1920 stereo samples.
    assert struct.calcsize(f"{SAMPLES_PER_FRAME * 2}h") == PCM_BYTES_PER_FRAME
