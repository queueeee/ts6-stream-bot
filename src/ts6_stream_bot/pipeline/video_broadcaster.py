"""Single-encoder video broadcast.

Replaces the MediaRelay-based fan-out for the video track. The earlier
design subscribed every viewer's ``RTCRtpSender`` to a copy of the raw
x11grab frames, and each sender ran its own libvpx encoder; per-viewer
RSS measured ~750 MB at 720p30 in live tests, which is what made the
4 GB host with 5+ viewers a non-starter.

Here a single ``av.CodecContext`` runs in a pump task, encodes each
captured frame exactly once, and fans out the resulting ``av.Packet``
to per-viewer ``asyncio.Queue``s. Each subscriber exposes a
``BroadcastVideoTrack`` whose ``recv()`` returns ``av.Packet`` (not
``av.VideoFrame``); aiortc's ``RTCRtpSender`` then drops into the
pre-encoded path::

    if isinstance(data, Frame):
        ... encode ...
    else:
        payloads, timestamp = self.__encoder.pack(data)  # cheap RTP packetize

So the only per-viewer cost is the RTP packetization plus the SRTP
output - libvpx itself runs once for the whole channel.

Trade-offs we accept for the win:

* Shared encoder = shared bitrate. We don't honour per-viewer REMB
  hints; the codec runs at the configured ``STREAM_BITRATE``. The
  earlier design didn't actually use REMB-driven adaptation either
  (we set static bitrate) so this is a no-op in practice.
* Any one viewer's PLI / FIR causes a keyframe for all viewers
  rather than just that one. Acceptable: keyframes are infrequent
  (gop_size=3000 frames = 100 s at 30 fps) and a few extra ones
  cost less than running N encoders.
* Slow viewers get frames dropped from their personal queue rather
  than holding up everyone else. The connection-state callback in
  StreamPublisher already evicts persistently broken peers.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
from collections.abc import Callable
from dataclasses import dataclass

import av
import structlog
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av.frame import Frame
from av.packet import Packet
from av.video.codeccontext import VideoCodecContext
from av.video.frame import PictureType, VideoFrame

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class VideoBroadcasterConfig:
    """Encoder + queue knobs. Defaults mirror aiortc's per-sender
    ``Vp8Encoder`` so the wire-level behaviour is unchanged - only
    the number of times we run the encoder differs."""

    bitrate: int  # bits per second (note: STREAM_BITRATE in .env is kbps)
    width: int
    height: int
    framerate: int
    cpu_used: int = -6
    deadline: str = "realtime"
    gop_size: int = 3000
    qmin: int = 2
    qmax: int = 56
    queue_size: int = 30  # ~1 second at 30 fps; slow viewers drop oldest
    thread_count: int | None = None  # None = auto-tune via aiortc's heuristic


class BroadcastVideoTrack(MediaStreamTrack):
    """Per-viewer track that yields pre-encoded ``av.Packet``s.

    aiortc's ``RTCRtpSender`` checks ``isinstance(data, Frame)`` -
    since we return ``Packet``, it calls ``encoder.pack(data)`` for
    cheap RTP packetization instead of ``encoder.encode(frame)``.
    """

    kind = "video"

    def __init__(
        self,
        broadcaster: VideoBroadcaster,
        queue: asyncio.Queue[Packet | None],
    ) -> None:
        super().__init__()
        self._broadcaster = broadcaster
        self._queue = queue

    async def recv(self) -> Packet:
        packet = await self._queue.get()
        if packet is None:
            # Sentinel: broadcaster shutting down. Surface as the
            # MediaStreamError aiortc expects to clean up the sender.
            raise MediaStreamError
        return packet

    def stop(self) -> None:
        super().stop()
        self._broadcaster._unsubscribe(self)


@dataclass(slots=True)
class _Subscriber:
    queue: asyncio.Queue[Packet | None]
    track: BroadcastVideoTrack
    drops: int = 0


class VideoBroadcaster:
    """One libvpx encoder. Many viewer queues."""

    def __init__(
        self,
        source_track_factory: Callable[[], MediaStreamTrack | None],
        config: VideoBroadcasterConfig,
    ) -> None:
        # The factory indirection lets us be constructed before VideoCapture
        # has started (capture.video_track is None until capture.start()).
        # ``start()`` resolves the factory and only then commits to a track.
        self._source_factory = source_track_factory
        self._config = config

        self._codec: VideoCodecContext | None = None
        self._source: MediaStreamTrack | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._stopped = False
        # Set to False once the source raises MediaStreamError or the
        # pump task crashes. The publisher checks this before attaching
        # a new viewer so dead sources reject joins instead of handing
        # out subscriptions to a queue nothing will ever fill.
        self._source_alive = False
        self._force_keyframe = False
        self._subscribers: list[_Subscriber] = []
        self._encoded_frames = 0
        # How many frames we discarded because the source queue had
        # piled up while the encoder was busy. Logged on stop() so an
        # operator can tell whether their host is keeping up with the
        # configured resolution / framerate.
        self._stale_frames_dropped = 0

    @property
    def is_alive(self) -> bool:
        """``False`` once the source has ended (or the pump crashed).
        StreamPublisher uses this to refuse joins instead of attaching
        viewers to a queue that will never get a frame."""
        return self._source_alive

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._pump_task is not None:
            return
        track = self._source_factory()
        if track is None:
            raise RuntimeError("video broadcaster: source track is None at start")
        self._source = track
        self._stopped = False
        self._source_alive = True
        self._pump_task = asyncio.create_task(self._pump_loop(), name="video-broadcaster-pump")
        log.info(
            "video_broadcaster.started",
            width=self._config.width,
            height=self._config.height,
            framerate=self._config.framerate,
            bitrate=self._config.bitrate,
        )

    async def stop(self) -> None:
        self._stopped = True
        # Wake every subscriber out of recv() so the per-viewer sender
        # can shut down cleanly.
        for sub in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
            self._pump_task = None
        # Flush any trailing encoded packets so libvpx releases internal buffers.
        if self._codec is not None:
            with contextlib.suppress(Exception):
                for _ in self._codec.encode(None):
                    pass
        self._codec = None
        log.info(
            "video_broadcaster.stopped",
            encoded_frames=self._encoded_frames,
            stale_frames_dropped=self._stale_frames_dropped,
        )

    # --- subscription -----------------------------------------------------

    def subscribe(self) -> BroadcastVideoTrack:
        """Return a new track. Forces a keyframe so the joining viewer
        can start decoding without waiting for the next periodic I-frame."""
        queue: asyncio.Queue[Packet | None] = asyncio.Queue(maxsize=self._config.queue_size)
        track = BroadcastVideoTrack(self, queue)
        sub = _Subscriber(queue=queue, track=track)
        self._subscribers.append(sub)
        self._force_keyframe = True
        log.info(
            "video_broadcaster.subscribed",
            total_subscribers=len(self._subscribers),
        )
        return track

    def _unsubscribe(self, track: BroadcastVideoTrack) -> None:
        before = len(self._subscribers)
        self._subscribers = [s for s in self._subscribers if s.track is not track]
        after = len(self._subscribers)
        if before != after:
            log.info(
                "video_broadcaster.unsubscribed",
                total_subscribers=after,
            )

    # --- internals --------------------------------------------------------

    def _build_codec(self, frame: VideoFrame) -> VideoCodecContext:
        cfg = self._config
        # Defaults below mirror aiortc.codecs.vpx.Vp8Encoder so the
        # wire-level encoder behaviour is unchanged. PyAV's stubs return
        # ``CodecContext`` from ``create()``; for a video codec the
        # actual instance is a ``VideoCodecContext`` with the width /
        # height / pix_fmt knobs we need.
        codec: VideoCodecContext = av.CodecContext.create("libvpx", "w")
        codec.width = frame.width
        codec.height = frame.height
        codec.bit_rate = cfg.bitrate
        codec.pix_fmt = "yuv420p"
        codec.gop_size = cfg.gop_size
        codec.qmin = cfg.qmin
        codec.qmax = cfg.qmax
        codec.options = {
            "bufsize": str(cfg.bitrate),
            "cpu-used": str(cfg.cpu_used),
            "deadline": cfg.deadline,
            "lag-in-frames": "0",
            "minrate": str(cfg.bitrate),
            "maxrate": str(cfg.bitrate),
            "noise-sensitivity": "4",
            "overshoot-pct": "15",
            "partitions": "0",
            "static-thresh": "1",
            "undershoot-pct": "100",
        }
        codec.thread_count = cfg.thread_count or _auto_thread_count(
            frame.width * frame.height, multiprocessing.cpu_count()
        )
        return codec

    async def _pump_loop(self) -> None:
        try:
            await self._pump()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("video_broadcaster.pump_crashed", error=str(exc))
            # Wake subscribers so their senders fail fast rather than hang.
            for sub in list(self._subscribers):
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(None)
        finally:
            # Mark dead so StreamPublisher refuses any new join requests
            # rather than attaching them to a queue nothing will fill.
            self._source_alive = False

    async def _pump(self) -> None:
        assert self._source is not None
        loop = asyncio.get_running_loop()
        while not self._stopped:
            try:
                frame = await self._source.recv()
            except MediaStreamError:
                log.info("video_broadcaster.source_ended")
                return

            # Skip backlog: aiortc's MediaPlayer keeps an unbounded
            # ``asyncio.Queue`` between the ffmpeg worker thread and our
            # consumer. If the encoder is even slightly slower than the
            # source frame rate, raw frames pile up there - at 1080p
            # that's ~8 MB each (bgr0 from x11grab) and the live deploy
            # leaked ~850 MB in 16 s before OOM. By draining whatever
            # extra frames are already queued and keeping only the most
            # recent one, we cap the backlog to a single frame and drop
            # stale ones rather than the whole pipeline drowning. The
            # subscriber-side encoder backlog is independently bounded
            # by ``queue_size`` per subscriber.
            frame, dropped = self._drain_to_latest(frame)
            if dropped:
                self._stale_frames_dropped += dropped

            if not isinstance(frame, VideoFrame):
                # Source drift - shouldn't happen with x11grab, log and skip.
                log.warning("video_broadcaster.unexpected_frame_type", got=type(frame).__name__)
                continue

            if frame.format.name != "yuv420p":
                frame = frame.reformat(format="yuv420p")

            if self._codec is None:
                self._codec = self._build_codec(frame)

            if self._force_keyframe:
                frame.pict_type = PictureType.I
                self._force_keyframe = False

            # libvpx's encode is the expensive call. Running it in the
            # default thread pool lets the event loop keep draining the
            # source queue (via the next ``self._source.recv()``) while
            # the encode runs in a worker thread - which is what aiortc
            # itself does for per-sender encoders in
            # rtcrtpsender._next_encoded_frame.
            try:
                packets = await loop.run_in_executor(None, self._encode_frame, frame)
            except av.error.FFmpegError as exc:
                log.warning("video_broadcaster.encode_failed", error=str(exc))
                continue

            for packet in packets:
                self._encoded_frames += 1
                self._fanout(packet)

    def _drain_to_latest(self, current: Frame | Packet) -> tuple[Frame | Packet, int]:
        """Reach into the source's internal asyncio queue and pull every
        already-buffered frame. Return the newest one (the rest are
        discarded). The MediaPlayer track aiortc gives us has a private
        ``_queue`` attribute - we rely on it being a plain
        ``asyncio.Queue`` because it has been since aiortc 1.0.x and
        nothing else exposes a non-blocking peek."""
        latest = current
        dropped = 0
        queue = getattr(self._source, "_queue", None)
        if queue is None:
            return latest, dropped
        while True:
            try:
                latest = queue.get_nowait()
            except asyncio.QueueEmpty:
                return latest, dropped
            dropped += 1

    def _encode_frame(self, frame: VideoFrame) -> list[Packet]:
        assert self._codec is not None
        return list(self._codec.encode(frame))

    def _fanout(self, packet: Packet) -> None:
        """Push a packet into every subscriber queue. On QueueFull, drop
        the oldest packet for that subscriber rather than block - one
        slow viewer must not stall the others."""
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(packet)
                continue
            except asyncio.QueueFull:
                pass
            # Make room and retry once. If the second put still fails,
            # we silently skip this packet for this viewer; the encoder
            # will produce another and the queue is bounded so this
            # bounded-skip is the only way out.
            with contextlib.suppress(asyncio.QueueEmpty):
                sub.queue.get_nowait()
            sub.drops += 1
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(packet)


def _auto_thread_count(pixels: int, cpu_count: int) -> int:
    """Match aiortc's libvpx thread heuristic (vpx.number_of_threads)."""
    if pixels >= 1920 * 1080:
        return min(cpu_count, 8)
    if pixels >= 1280 * 720:
        return min(cpu_count, 4)
    if pixels >= 640 * 480:
        return min(cpu_count, 2)
    return 1


__all__ = [
    "BroadcastVideoTrack",
    "VideoBroadcaster",
    "VideoBroadcasterConfig",
]
