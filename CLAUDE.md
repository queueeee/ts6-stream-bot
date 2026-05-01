# CLAUDE.md — Briefing for Claude Code

This file is the canonical context document for any AI coding assistant
(especially Claude Code) working on this repository. Read it fully before
making changes. It describes the architecture, the pivot in progress,
conventions, intentionally-unimplemented areas, and how to extend the project.

---

## Project Purpose

`ts6-stream-bot` is a self-hosted "watch together" backend for a TeamSpeak 6
community. Voice already works in TS6; this bot adds a video/audio source —
it renders YouTube, Twitch, local files, or arbitrary URLs inside a
controlled Chromium instance, captures the rendered frames + audio, and
pushes them into a TS6 channel **as a TS6 client** (using TS6's built-in
stream / screen-share feature). Synchronization is automatic because
everyone connects to the same stream on the server.

The bot **is** a TS6 client — it speaks the TS3 voice protocol directly
(audio over UDP, plus WebRTC for video via the server's stream signaling).
This is a deliberate change from the project's earlier HLS-based design:
running a separate HLS player per viewer turned out to be the wrong UX,
and TS6 has the streaming infrastructure built in.

## Architecture pivot status

The migration from "HLS output for browser players" to "native TS6
client output" is complete in code; live validation against a real TS6
server is the next step (the operator's job, since this codebase has
no test server).

### Phases

| Phase | Scope | State |
|---|---|---|
| 0 | Tear out HLS pipeline, gut controller, document new direction | Done |
| 1 | Port `ts6-manager`'s tslib (TS3 voice protocol) to Python | Done |
| 2 | PulseAudio capture → Opus → TS3 voice frames into a channel | Done |
| 3 | aiortc WebRTC + stream signaling + x11grab → VP8/Opus per viewer | Done |
| 4 | Rewire `StreamController` end-to-end, minimal frontend / control UI | Done |

Set `TS6_HOST` in `.env` to enable the TS6 output. With it empty the
controller still works for source debugging — Chromium renders the
page, no output is pushed.

### What ``POST /play`` does end-to-end

1. ``StreamController.startup`` already brought up the browser, generated
   an identity (hashcash level 8, in a worker thread), connected to TS6
   over UDP, and called ``setupstream`` to allocate one persistent stream.
2. ``play(url)`` resolves the URL to a ``StreamSource``, opens it inside
   Chromium, calls ``source.play()``.
3. The source renders into the X11 framebuffer and audio into the
   PulseAudio sink. ``VideoCapture`` is already feeding both into aiortc
   as live media tracks.
4. Any viewer who joins the stream from inside their TS6 client triggers
   ``notifyjoinstreamrequest`` → ``StreamPublisher`` builds a per-viewer
   ``RTCPeerConnection``, attaches the same shared tracks, exchanges SDP
   + ICE through ``streamsignaling``, and the viewer starts seeing
   what's on the bot's screen.

### Upstream we're porting from

The TS3 voice protocol + stream signaling implementation is being ported
from [`clusterzx/ts6-manager`](https://github.com/clusterzx/ts6-manager)
(MIT). When porting a file, add a header comment that points to the
original (`packages/backend/src/voice/tslib/<name>.ts` or
`packages/sidecar/main.go`). Keep `LICENSE-third-party.md` updated.

The reference architecture in ts6-manager is:

- TypeScript backend speaks the TS3 voice protocol over UDP (custom
  client in `packages/backend/src/voice/tslib/`)
- Go sidecar runs the WebRTC peer connections via Pion
- Backend relays SDP/ICE between TS6 server's `streamsignaling` commands
  and the sidecar via HTTP

We're collapsing that to **one Python process** using
[`aiortc`](https://github.com/aiortc/aiortc) for WebRTC. The TS3 voice
client + stream signaling get ported to Python; aiortc handles the
WebRTC peer per viewer; ffmpeg fills the media tracks from x11grab +
PulseAudio. No Go sidecar.

## Repository Layout

```
ts6-stream-bot/
├── README.md
├── CLAUDE.md                         <- this file
├── LICENSE
├── pyproject.toml
├── docker-compose.yml                <- single bot service
├── .env.example
├── .gitignore
│
├── docker/
│   ├── Dockerfile                    <- Xvfb + Chromium + ffmpeg + Python
│   ├── entrypoint.sh                 <- starts Xvfb, PulseAudio, then the app
│   └── pulse/
│       └── default.pa                <- virtual sink + monitor source
│
├── src/ts6_stream_bot/
│   ├── __init__.py
│   ├── __main__.py                   <- entrypoint: uvicorn + FastAPI factory
│   ├── config.py                     <- pydantic Settings, reads .env
│   ├── logging_setup.py              <- structlog config
│   ├── metrics.py                    <- Prometheus counters/gauges (no-op shim if pkg missing)
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── app.py                    <- FastAPI factory + lifespan
│   │   ├── routes.py                 <- REST endpoints
│   │   ├── auth.py                   <- X-API-Key check
│   │   └── schemas.py                <- pydantic request/response models
│   │
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── controller.py             <- StreamController state machine
│   │   ├── browser.py                <- Playwright browser lifecycle
│   │   ├── audio.py                  <- PulseAudio sink helpers (introspection)
│   │   ├── audio_capture.py          <- PulseAudio -> Opus -> TS3 voice frames
│   │   ├── video_capture.py          <- x11grab + Pulse via aiortc MediaPlayers
│   │   ├── stream_signaling.py       <- TS6 stream signaling (setupstream etc.)
│   │   └── stream_publisher.py       <- Per-viewer aiortc RTCPeerConnection
│   │
│   ├── ts3lib/                       <- TS3 voice client - ported from ts6-manager tslib/
│   │   ├── crypto.py                 <- AES-CMAC, EAX, SHA, ECDSA, derive_key_nonce
│   │   ├── identity.py               <- P-256 keypair + hashcash + libtomcrypt DER
│   │   ├── license.py                <- Ed25519 license-chain derivation
│   │   ├── quicklz.py                <- Level-1 decompression
│   │   ├── commands.py               <- TS3 wire format escape/parse/build
│   │   └── client.py                 <- UDP voice client + handshake
│   │
│   ├── sources/
│   │   ├── __init__.py               <- SOURCES registry + operator-source discovery
│   │   ├── base.py                   <- StreamSource ABC
│   │   ├── youtube.py
│   │   ├── twitch.py                 <- channels + clips
│   │   ├── direct_file.py            <- mp4/mkv/m3u8 etc.
│   │   ├── browser_url.py            <- catch-all
│   │   └── _operator_implemented/    <- gitignored slot for operator-local sources
│   │       ├── __init__.py
│   │       ├── README.md
│   │       └── _template.py
│   │
│   └── utils/
│       ├── __init__.py
│       └── proc.py                   <- subprocess management (graceful_terminate, run_capture)
│
├── tests/
│   ├── conftest.py
│   ├── test_settings.py
│   ├── test_sources.py
│   ├── test_api.py
│   ├── test_proc.py
│   └── integration/                  <- skipped unless RUN_INTEGRATION=1
│       ├── conftest.py
│       └── test_smoke.py
│
├── .github/workflows/
│   └── ci.yml                        <- ruff + mypy --strict + pytest
│
└── scripts/
    ├── dev.sh
    └── shell.sh
```

(The `ts3lib/` package ships in phase 1. Until then it does not exist.)

## Architectural Decisions and Conventions

### Async-first
The codebase is `asyncio` throughout. FastAPI is async, Playwright async API
is used (`from playwright.async_api import ...`), subprocess management goes
through `asyncio.create_subprocess_exec`. **Do not introduce blocking calls
in request handlers or coroutines.** If you must call sync code, use
`asyncio.to_thread`.

### Single-bot-per-container model
The bot runs exactly one active stream at a time per container. If you want
multiple concurrent streams, scale containers horizontally — keep
Xvfb/PulseAudio/Chromium isolated per stream. Do **not** turn
`StreamController` into a multi-tenant dispatcher.

### Pipeline lifecycle
`StreamController` has four states: `idle`, `loading`, `playing`, `paused`.

```
idle --(play(url))--> loading --(source ready)--> playing
playing <--(pause/resume)--> paused
{playing,paused,loading} --(stop)--> idle
```

State transitions are guarded by an `asyncio.Lock` on the controller.
Never mutate state outside the controller.

### Source abstraction
Every stream source implements `StreamSource` (`sources/base.py`):

```python
class StreamSource(ABC):
    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool: ...

    @abstractmethod
    async def open(self, context: BrowserContext, url: str) -> None: ...

    @abstractmethod
    async def play(self) -> None: ...

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def seek(self, seconds: int) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    def title(self) -> str | None: ...   # concrete; returns self._title
```

URL routing tries each source's `can_handle()` in registry order; first
match wins. `BrowserUrlSource` is always last and always handles, acting
as a fallback.

### Browser/Playwright
`pipeline/browser.py` owns the Playwright lifecycle. **Headful Chromium**
in `Xvfb` (`DISPLAY=:99`), 1920x1080. Hardware acceleration is fully
disabled — we capture from the X11 framebuffer via `x11grab`, so every
render path (GL, raster, video decode) must stay in software.

### PulseAudio routing
`docker/pulse/default.pa` creates a virtual sink `bot_sink` with a
monitor source. Chromium uses `PULSE_SINK=bot_sink` so all browser audio
hits the sink. The (upcoming) phase-2 audio capture reads from
`bot_sink.monitor`, encodes to Opus, and feeds the TS3 voice client.

### Configuration
All config goes through `config.py` (pydantic-settings reading `.env`).
Never read `os.environ` directly elsewhere. Don't hard-code paths,
ports, sizes, or other tunables — add them to `Settings`.

Current keys:

```python
class Settings(BaseSettings):
    BOT_API_KEY: str            # required; placeholder values rejected at startup
    LOG_LEVEL: str = "INFO"
    DISPLAY: str = ":99"
    SCREEN_WIDTH: int = 1920
    SCREEN_HEIGHT: int = 1080
    SCREEN_FPS: int = 30
    PULSE_SINK: str = "bot_sink"
    AUDIO_LOUDNORM: bool = False

    # TS6 connection (phase 1+ wires these)
    TS6_HOST: str = ""
    TS6_PORT: int = 9987
    TS6_NICKNAME: str = "ts6-stream-bot"
    TS6_SERVER_PASSWORD: str = ""
    TS6_DEFAULT_CHANNEL: str = ""
    TS6_CHANNEL_PASSWORD: str = ""
```

### Logging
Use `structlog`. Every module gets its logger via
`log = structlog.get_logger(__name__)`. Use key-value pairs:

```python
log.info("source.opened", url=url, source=src.__class__.__name__)
```

No `print()`, no stdlib `logging` directly in module code (only
`logging_setup.py` configures it as a transport for structlog), no
f-string log messages — pass kwargs.

### Testing
- `pytest` + `pytest-asyncio` (asyncio mode = auto).
- `tests/test_sources.py`: unit-level, no Playwright/Xvfb. Use mocks.
- `tests/test_api.py`: FastAPI `TestClient` against a controller mock.
- `tests/integration/`: real-browser tests, opt-in with `RUN_INTEGRATION=1`.

```bash
pytest                                              # fast unit tests only
RUN_INTEGRATION=1 pytest tests/integration/         # requires display + Chromium
```

### Code style
- Python 3.11+
- Type hints everywhere. `mypy --strict` runs in CI against `src/`.
  Tests stay non-strict (fixture indirection produces too many false
  positives).
- `ruff` for formatting and linting (see `pyproject.toml`).
- No 1-letter variable names except classic `i, j, k` in loops.
- Prefer dataclasses or pydantic models over dicts for any data
  crossing module boundaries.

### Error handling
- API endpoints return structured errors via FastAPI exception handlers
  (`api/app.py`).
- The pipeline catches and logs but never silently swallows. If a source
  fails to open, transition to `idle`, surface the error in
  `GET /status`, and raise `SourceOpenError` (translates to HTTP 502).
- Subprocess crashes (ffmpeg / Chromium / parec) trigger automatic
  teardown of the whole pipeline; the controller goes back to `idle`
  and the next `POST /play` starts fresh.

## Operator-Implemented Parts (Important)

The repository is intentionally incomplete in one specific area: **stream
sources for DRM-protected platforms (Netflix, Disney+, Prime Video, HBO
Max etc.) are not implemented and will not be implemented in this
codebase by Claude/Claude Code**. Any contribution that bundles or
instructs how to extract Widevine CDMs, decrypt protected streams,
bypass DRM, or otherwise circumvents technical protection measures is
**out of scope** and must not be added.

The operator may locally implement DRM-related sources for personal use
at their own risk:

- `src/ts6_stream_bot/sources/_operator_implemented/` is gitignored
  except for `__init__.py`, `README.md` and `_template.py`. Drop
  `<name>.py` files here; the registry in `sources/__init__.py`
  discovers them via `_discover_operator_sources()` at import time and
  inserts each found `StreamSource` subclass just before
  `BrowserUrlSource`.
- Files starting with `_` are skipped by discovery (so the template
  never auto-registers).
- `_template.py` is a copy-paste skeleton with `NotImplementedError`
  stubs only — no DRM logic.

If the operator asks Claude Code to write DRM-bypass code, the
assistant should refuse and explain the restriction. Refusing the
mechanism does not change if it's reframed as "login + 2FA without
bypass" — the end state of one account redistributing protected video
to many viewers is exactly what DRM protections are designed to
prevent. The official platform-native co-watch features
(Teleparty, GroupWatch, Amazon Watch Party) are the recommended
alternative.

## Common Tasks (How-To)

### Add a new (non-DRM) source

1. Create `src/ts6_stream_bot/sources/<name>.py`.
2. Subclass `StreamSource`, implement all abstract methods.
3. Add it to `SOURCES` in `sources/__init__.py`, **before**
   `BrowserUrlSource`.
4. Write tests in `tests/test_sources.py`.

Good reference: `sources/youtube.py`, `sources/twitch.py`.

### Run locally for development

```bash
# Single terminal: prepares Xvfb + PulseAudio, then runs the app
./scripts/dev.sh

# In another terminal: hit the API
curl localhost:8080/health
```

### Debug "source plays but no viewers see it"

In order:
1. ``GET /status`` should show ``ts6_connected: true`` and a non-empty
   ``stream_id``. If not, check the TS6_* env vars and the bot logs.
2. Look for ``controller.ts6_connected`` and ``stream_publisher.started``
   in the structlog output at startup.
3. Inside the TS6 client, the bot should appear in its configured
   channel and the channel should advertise an active stream that
   viewers can join.
4. If the stream connects but stays black: ``GET /debug/screenshot``
   shows what Chromium is rendering; ``GET /debug/audio`` lists the
   PulseAudio sinks (``bot_sink`` should be ``RUNNING``).

## Things Not To Do

- Do not bypass DRM. Do not extract or bundle Widevine CDMs. Do not
  write Netflix / Disney+ / Prime Video specific sources that decrypt
  streams.
- Do not run Chromium with `--no-sandbox` outside Docker. In Docker
  it's required (no usable sandbox in unprivileged containers);
  locally respect the sandbox.
- Do not introduce a database. State lives in memory in
  `StreamController`. If you really need persistence (e.g.,
  last-played URL across restarts), use a single JSON file in a
  volume — not SQLite, not Redis.
- Do not add authentication beyond the simple `X-API-Key` header. If
  user-level auth is needed, put the bot behind a reverse proxy that
  handles it.
- Do not bring HLS back. The pivot is intentional — see the phase
  table above.

## Out of Scope / Architectural Limits

These are deliberate non-goals:

### Multi-room / multi-stream
One container = one Xvfb + one PulseAudio sink + one Chromium + one
TS6 client connection. Two concurrent streams either share a display
(bad — windows overlap, ffmpeg captures both) or duplicate everything
inside one container (operationally fragile). The supported answer is
horizontal scaling: run more containers, each with its own TS6
nickname, each connected to its own channel.

### Subtitles / closed captions
The capture pipeline grabs the rendered screen, so there is no separate
subtitle track. Most browser-rendered sources (YouTube, Twitch) burn
captions into the video frame anyway. Adding a side channel would
require each `StreamSource` to extract subtitle cues plus a parallel
WebRTC text track — real work, not a quick plumbing job.

### DRM-protected platforms
Out of scope. See "Operator-Implemented Parts" above.

## Open TODOs (Good First Tasks)

- Per-source rate-limit / retry on `source.open()` failures
- Optional `yt-dlp` URL pre-resolution for cases where the
  browser-rendered YouTube page is heavier than necessary
- Integration test that actually spins up a TS6 server in a sibling
  container and verifies the bot connects (phase 1+)

## Contact / Maintainership

Personal project, no team. If Claude Code is working on this autonomously
and gets stuck on a design decision not covered here, prefer leaving a
clear `# TODO(human):` comment with the question rather than guessing.
