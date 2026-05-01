"""StreamPublisher: glue between TS6 stream signaling and aiortc.

Owns one ``RTCPeerConnection`` per joined viewer. Reads SDP / ICE off
``StreamSignaling`` callbacks, hands SDP back via the same channel,
and runs the offer/answer dance from the bot side. Modelled after
ts6-manager's voice-bot.ts streaming methods (MIT) but collapsed into
one Python module - we don't have a separate Go sidecar here.

Lifecycle::

    publisher = StreamPublisher(ts3_client, signaling, video_capture)
    await publisher.start(name="My Stream", bitrate=4608)
    # ... bot is live; viewers can join via the TS6 UI ...
    await publisher.stop()

ICE strategy: aiortc's RTCPeerConnection gathers ICE candidates as
part of ``setLocalDescription``. We wait for ``iceGatheringState ==
"complete"`` before sending the offer, so the SDP carries all our
candidates. Incoming candidates from the viewer (TS6 clients trickle)
are forwarded through ``pc.addIceCandidate`` as they arrive.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

import structlog
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp

from ts6_stream_bot.config import settings
from ts6_stream_bot.pipeline.stream_signaling import (
    SignalingMessage,
    SignalingType,
    StreamSignaling,
)
from ts6_stream_bot.pipeline.video_capture import VideoCapture
from ts6_stream_bot.ts3lib.client import Ts3Client

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _Viewer:
    clid: int
    pc: RTCPeerConnection
    joined_at: float
    # remote_set fires once setRemoteDescription has completed for this
    # viewer. ICE candidates arriving before that event hold off in
    # `_apply_ice_candidate` instead of being silently rejected by
    # aiortc with "addIceCandidate called without remote description".
    remote_set: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class StreamPublisherStatus:
    streaming: bool
    stream_id: str | None
    viewer_count: int
    viewers: list[int] = field(default_factory=list)


class StreamPublisher:
    """One TS6 stream + N viewer peer connections."""

    def __init__(
        self,
        *,
        client: Ts3Client,
        signaling: StreamSignaling,
        capture: VideoCapture,
    ) -> None:
        self._client = client
        self._signaling = signaling
        self._capture = capture

        self._stream_id: str | None = None
        self._stream_started_event = asyncio.Event()

        self._viewers: dict[int, _Viewer] = {}
        self._lock = asyncio.Lock()
        # Track in-flight tasks so the GC doesn't clean them up mid-flight.
        self._tasks: set[asyncio.Task[None]] = set()
        # MediaRelay fans out one source track to many per-viewer subscribers
        # so two PeerConnections don't both call recv() on the same underlying
        # track at the same time (would race on the parec / x11grab subprocess).
        self._relay = MediaRelay()

        # Wire up signaling callbacks. We chain so existing handlers stay alive.
        self._prev_join = signaling.on_join_stream_request
        self._prev_signaling = signaling.on_signaling_message
        self._prev_left = signaling.on_stream_client_left
        self._prev_started = signaling.on_stream_started

        signaling.on_join_stream_request = self._on_join_request
        signaling.on_signaling_message = self._on_signaling_message
        signaling.on_stream_client_left = self._on_client_left
        signaling.on_stream_started = self._on_stream_started

    @property
    def is_streaming(self) -> bool:
        return self._stream_id is not None

    @property
    def stream_id(self) -> str | None:
        return self._stream_id

    def status(self) -> StreamPublisherStatus:
        return StreamPublisherStatus(
            streaming=self.is_streaming,
            stream_id=self._stream_id,
            viewer_count=len(self._viewers),
            viewers=sorted(self._viewers.keys()),
        )

    # --- lifecycle --------------------------------------------------------

    async def start(
        self,
        *,
        name: str = "Bot Stream",
        bitrate: int = 4608,
        accessibility: int = 1,
        mode: int = 1,
        viewer_limit: int = 0,
        audio: bool = True,
        timeout: float = 10.0,
    ) -> str:
        """Allocate a TS6 stream + bring up the capture pipeline. Returns
        the server-assigned stream id once ``notifystreamstarted`` arrives.
        Raises ``TimeoutError`` if the server doesn't answer in ``timeout``s.
        """
        if self.is_streaming:
            assert self._stream_id is not None
            return self._stream_id

        # Make sure the underlying ffmpeg pipelines are running before we
        # accept any join requests.
        await self._capture.start()

        self._stream_started_event.clear()
        self._signaling.send_setup_stream(
            name=name,
            bitrate=bitrate,
            accessibility=accessibility,
            mode=mode,
            viewer_limit=viewer_limit,
            audio=audio,
        )

        try:
            await asyncio.wait_for(self._stream_started_event.wait(), timeout=timeout)
        except TimeoutError:
            await self._capture.stop()
            raise

        assert self._stream_id is not None
        log.info("stream_publisher.started", stream_id=self._stream_id, name=name, bitrate=bitrate)
        return self._stream_id

    async def stop(self) -> None:
        """Kick all viewers, stopstream, and tear down capture."""
        if self._stream_id is None:
            await self._capture.stop()
            return

        async with self._lock:
            stream_id = self._stream_id
            viewers = list(self._viewers.values())
            self._viewers.clear()

        # Kick + close each peer.
        for v in viewers:
            with contextlib.suppress(Exception):
                self._signaling.send_remove_client(viewer_clid=v.clid, stream_id=stream_id)
            with contextlib.suppress(Exception):
                await v.pc.close()

        # Tell the server we're done. We send and assume - the bot's TS3
        # resend loop covers single-packet loss.
        self._signaling.send_stream_stop(stream_id)

        # Brief grace period for the stopstream packet to be ACKed before
        # we tear down ffmpeg out from under the encoder.
        await asyncio.sleep(0.5)

        await self._capture.stop()
        self._stream_id = None
        log.info("stream_publisher.stopped", stream_id=stream_id)

    # --- signaling callbacks ---------------------------------------------

    def _on_stream_started(self, stream) -> None:  # type: ignore[no-untyped-def]
        # Only mark ourselves started when the stream's clid matches us.
        if stream.clid != self._client.client_id:
            if self._prev_started is not None:
                with contextlib.suppress(Exception):
                    self._prev_started(stream)
            return
        self._stream_id = stream.id
        self._stream_started_event.set()
        if self._prev_started is not None:
            with contextlib.suppress(Exception):
                self._prev_started(stream)

    def _on_join_request(self, params: dict[str, str]) -> None:
        try:
            viewer_clid = int(params.get("clid") or 0)
        except ValueError:
            viewer_clid = 0
        stream_id = params.get("id") or self._stream_id
        if not viewer_clid or not stream_id:
            return
        # Run the WebRTC handshake on the event loop.
        self._spawn(self._handle_viewer_join(viewer_clid, stream_id))
        if self._prev_join is not None:
            with contextlib.suppress(Exception):
                self._prev_join(params)

    def _on_signaling_message(self, msg: SignalingMessage) -> None:
        if msg.type == SignalingType.ANSWER and msg.clid is not None and msg.sdp is not None:
            self._spawn(self._apply_answer(msg.clid, msg.sdp))
        elif (
            msg.type == SignalingType.ICE_CANDIDATE
            and msg.clid is not None
            and msg.candidate is not None
        ):
            self._spawn(
                self._apply_ice_candidate(
                    msg.clid,
                    msg.candidate,
                    msg.sdp_mid or "0",
                    msg.sdp_mline_index or 0,
                )
            )
        elif msg.type == SignalingType.RECONNECT and msg.clid is not None:
            self._spawn(self._handle_reconnect(msg.clid))
        if self._prev_signaling is not None:
            with contextlib.suppress(Exception):
                self._prev_signaling(msg)

    def _on_client_left(self, params: dict[str, str]) -> None:
        try:
            clid = int(params.get("clid") or 0)
        except ValueError:
            clid = 0
        if clid:
            self._spawn(self._close_viewer(clid))
        if self._prev_left is not None:
            with contextlib.suppress(Exception):
                self._prev_left(params)

    # --- per-viewer flow --------------------------------------------------

    async def _handle_viewer_join(self, viewer_clid: int, stream_id: str) -> None:
        async with self._lock:
            # Same viewer joining twice (e.g. reconnect): drop the old PC first.
            existing = self._viewers.pop(viewer_clid, None)
        if existing is not None:
            with contextlib.suppress(Exception):
                await existing.pc.close()

        # STUN exposes the bot's public-NAT'd address as a server-reflexive
        # candidate. TURN relays media when direct NAT punching fails -
        # both are env-overridable in case the operator's network needs
        # something other than Google's public STUN / no-TURN default.
        ice_servers: list[RTCIceServer] = []
        if settings.STUN_URL:
            ice_servers.append(RTCIceServer(urls=settings.STUN_URL))
        if settings.TURN_URL:
            ice_servers.append(
                RTCIceServer(
                    urls=settings.TURN_URL,
                    username=settings.TURN_USERNAME or None,
                    credential=settings.TURN_PASSWORD or None,
                )
            )
        pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=ice_servers) if ice_servers else None
        )

        # Surface aiortc's own connection-state lifecycle so we can tell
        # whether a viewer disconnected because their client closed the
        # tab (TS6 sends notifystreamclientleft) vs. their RTCPeer dying
        # silently (no signaling, just ICE failure / DTLS timeout). The
        # latter previously left the slot allocated server-side and made
        # the user unable to re-join.
        @pc.on("connectionstatechange")
        async def _on_connection_state_change() -> None:
            state = pc.connectionState
            log.info(
                "stream_publisher.viewer_pc_state",
                clid=viewer_clid,
                state=state,
                ice=pc.iceConnectionState,
            )
            if state in ("failed", "closed"):
                self._spawn(self._evict_viewer_locally(viewer_clid, stream_id, state))

        @pc.on("iceconnectionstatechange")
        async def _on_ice_state_change() -> None:
            log.info(
                "stream_publisher.viewer_ice_state",
                clid=viewer_clid,
                ice=pc.iceConnectionState,
            )

        # Each viewer needs its OWN track that pulls from the shared source
        # via MediaRelay. Sharing the source track directly across PCs makes
        # both senders call recv() concurrently and crashes parec's
        # readexactly() with a "another coroutine is already waiting" error.
        if self._capture.video_track is not None:
            pc.addTrack(self._relay.subscribe(self._capture.video_track))
        if self._capture.audio_track is not None:
            pc.addTrack(self._relay.subscribe(self._capture.audio_track))

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self._wait_for_ice_gathering(pc)
        except Exception as exc:
            log.exception("stream_publisher.offer_failed", clid=viewer_clid, error=str(exc))
            with contextlib.suppress(Exception):
                await pc.close()
            return

        offer_sdp = pc.localDescription.sdp

        async with self._lock:
            self._viewers[viewer_clid] = _Viewer(
                clid=viewer_clid, pc=pc, joined_at=asyncio.get_event_loop().time()
            )

        self._signaling.send_join_response(
            viewer_clid=viewer_clid, stream_id=stream_id, accept=True, offer_sdp=offer_sdp
        )
        log.info("stream_publisher.viewer_offer_sent", clid=viewer_clid, total=len(self._viewers))

    async def _apply_answer(self, viewer_clid: int, answer_sdp: str) -> None:
        viewer = self._viewers.get(viewer_clid)
        if viewer is None:
            log.debug("stream_publisher.answer_for_unknown_viewer", clid=viewer_clid)
            return
        try:
            await viewer.pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer_sdp, type="answer")
            )
            # Unblock any ICE candidates that arrived first - aiortc rejects
            # addIceCandidate calls before setRemoteDescription, and the
            # signaling layer has no ordering guarantee between the answer
            # task and the per-candidate tasks once they're both spawned.
            viewer.remote_set.set()
            log.info("stream_publisher.viewer_connected", clid=viewer_clid)
        except Exception as exc:
            log.exception("stream_publisher.set_answer_failed", clid=viewer_clid, error=str(exc))
            await self._close_viewer(viewer_clid)

    async def _apply_ice_candidate(
        self, viewer_clid: int, candidate_sdp: str, sdp_mid: str, sdp_mline_index: int
    ) -> None:
        viewer = self._viewers.get(viewer_clid)
        if viewer is None:
            return
        # Wait until setRemoteDescription has completed; aiortc otherwise
        # rejects the call with "addIceCandidate called without remote
        # description" and the candidate is lost. The early candidates are
        # often the only ones that include public srflx info, so losing them
        # turned every previous ICE attempt into a NAT-punch lottery.
        try:
            await asyncio.wait_for(viewer.remote_set.wait(), timeout=10.0)
        except TimeoutError:
            log.warning(
                "stream_publisher.ice_wait_timeout",
                clid=viewer_clid,
                detail="remote description never arrived",
            )
            return
        try:
            candidate = candidate_from_sdp(candidate_sdp)
            candidate.sdpMid = sdp_mid
            candidate.sdpMLineIndex = sdp_mline_index
            await viewer.pc.addIceCandidate(candidate)
        except Exception as exc:
            log.warning("stream_publisher.ice_apply_failed", clid=viewer_clid, error=str(exc))

    async def _handle_reconnect(self, viewer_clid: int) -> None:
        if not self._stream_id:
            return
        log.info("stream_publisher.viewer_reconnect", clid=viewer_clid)
        await self._close_viewer(viewer_clid)
        await self._handle_viewer_join(viewer_clid, self._stream_id)

    async def _evict_viewer_locally(
        self, viewer_clid: int, stream_id: str, reason: str
    ) -> None:
        """Drop a PC that died on aiortc's side (failed/closed) without
        the server sending notifystreamclientleft. Telling the server to
        ``removeclientfromstream`` frees the slot so the viewer can
        retry the join from their UI; otherwise TS6 thinks they're still
        connected and the bot ignores their next click."""
        async with self._lock:
            viewer = self._viewers.pop(viewer_clid, None)
        if viewer is None:
            return
        with contextlib.suppress(Exception):
            await viewer.pc.close()
        with contextlib.suppress(Exception):
            self._signaling.send_remove_client(
                viewer_clid=viewer_clid, stream_id=stream_id
            )
        log.info(
            "stream_publisher.viewer_evicted",
            clid=viewer_clid,
            reason=reason,
            remaining=len(self._viewers),
        )

    async def _close_viewer(self, viewer_clid: int) -> None:
        async with self._lock:
            viewer = self._viewers.pop(viewer_clid, None)
        if viewer is None:
            return
        with contextlib.suppress(Exception):
            await viewer.pc.close()
        log.info("stream_publisher.viewer_left", clid=viewer_clid, remaining=len(self._viewers))

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        """Schedule a coroutine + retain the task reference until it finishes."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _wait_for_ice_gathering(
        pc: RTCPeerConnection, *, timeout: float = 5.0, poll: float = 0.05
    ) -> None:
        """Wait until ``iceGatheringState == 'complete'`` so our outgoing
        SDP includes every candidate. aiortc doesn't expose a trickle
        callback, so we poll - it's only ever a few hundred ms in practice."""
        deadline = asyncio.get_event_loop().time() + timeout
        while pc.iceGatheringState != "complete":
            if asyncio.get_event_loop().time() > deadline:
                log.warning(
                    "stream_publisher.ice_gathering_timeout",
                    state=pc.iceGatheringState,
                )
                return
            await asyncio.sleep(poll)


__all__ = [
    "StreamPublisher",
    "StreamPublisherStatus",
]
