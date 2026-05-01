# CLAUDE.md — Briefing for Claude Code

This file is the canonical context document for any AI coding assistant (especially Claude Code) working on this repository. Read it fully before making changes. It describes architecture, conventions, intentionally-unimplemented areas, and how to extend the project.

---

## Project Purpose

`ts6-stream-bot` provides a self-hosted "watch together" backend for a TeamSpeak 6 community. The TS6 server handles voice; this bot renders video sources (YouTube, Twitch, local files, custom sources) inside a controlled Chromium instance, captures the rendered frames, and exposes them as an HLS stream that users can open in their browser. Synchronization is implicit: everyone reads the same HLS segments, so everyone sees the same frame within a few seconds.

The bot is **not** a TS6 voice client. It does not connect to the TS6 voice protocol. The TS6 server stays untouched. The bot lives next to it as a separate Docker service in the shared `ts6-net` bridge network.

## Operator-Implemented Parts (Important)

The repository is intentionally incomplete in one specific area: **stream sources for DRM-protected platforms (Netflix, Disney+, Prime Video, HBO Max etc.) are not implemented and will not be implemented in this codebase by Claude/Claude Code**. Any contribution that bundles or instructs how to extract Widevine CDMs, decrypt protected streams, bypass DRM, or otherwise circumvents technical protection measures is **out of scope** and must not be added.

The operator who runs this bot may locally implement DRM-related sources for personal use at their own risk. The hooks for that are clearly marked in the source tree (`src/ts6_stream_bot/sources/_operator_implemented/` is reserved for it and is git-ignored). If the operator asks Claude Code to write code that bypasses DRM, the assistant should refuse and explain the restriction.

Everything else — refactors, new non-DRM sources (YouTube, Twitch, Vimeo, local files, IPTV, RTMP ingest), better encoding parameters, monitoring, UI improvements, tests — is fair game and welcomed.

## Repository Layout

```
ts6-stream-bot/
├── README.md
├── CLAUDE.md                       <- this file
├── LICENSE
├── pyproject.toml                  <- Python deps (managed via uv or pip)
├── docker-compose.yml              <- the bot service + nginx for HLS hosting
├── .env.example                    <- env vars template
├── .gitignore
│
├── docker/
│   ├── Dockerfile                  <- the bot container (Xvfb + Chromium + ffmpeg + Python)
│   ├── nginx.conf                  <- HLS-serving nginx config
│   ├── entrypoint.sh               <- starts Xvfb, PulseAudio, then the Python app
│   └── pulse/
│       └── default.pa              <- PulseAudio config: virtual sink + monitor source
│
├── src/ts6_stream_bot/
│   ├── __init__.py
│   ├── __main__.py                 <- entrypoint: starts FastAPI app
│   ├── config.py                   <- pydantic Settings, reads .env
│   ├── logging_setup.py            <- structlog-based logger
│   ├── metrics.py                  <- Prometheus counters/gauges
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── app.py                  <- FastAPI factory
│   │   ├── routes.py               <- REST endpoints (/play, /pause, /status, /health, /metrics, /debug/*)
│   │   ├── auth.py                 <- X-API-Key check
│   │   └── schemas.py              <- pydantic request/response models
│   │
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── controller.py           <- StreamController: orchestrates source + capture + encoder
│   │   ├── browser.py              <- Playwright browser wrapper (headful, in Xvfb DISPLAY)
│   │   ├── capture.py              <- ffmpeg subprocess: x11grab + pulse -> HLS
│   │   └── audio.py                <- PulseAudio sink/monitor wiring helpers
│   │
│   ├── sources/
│   │   ├── __init__.py             <- registry: maps URL/type -> Source class
│   │   ├── base.py                 <- abstract StreamSource ABC
│   │   ├── youtube.py              <- YoutubeSource
│   │   ├── twitch.py               <- TwitchSource (channels + clips)
│   │   ├── direct_file.py          <- DirectFileSource (mp4/mkv/m3u8 etc.)
│   │   ├── browser_url.py          <- generic catch-all: just opens any URL in browser
│   │   └── _operator_implemented/  <- gitignored; for operator-local sources (DRM)
│   │       └── .gitkeep
│   │
│   └── utils/
│       ├── __init__.py
│       ├── proc.py                 <- subprocess management with graceful shutdown
│       └── paths.py                <- HLS filesystem + URL path helpers (single source of truth)
│
├── tests/
│   ├── conftest.py
│   ├── test_sources.py             <- unit tests for source resolution
│   ├── test_api.py                 <- FastAPI TestClient tests
│   ├── test_settings.py            <- Settings validation
│   ├── test_paths.py               <- HLS path helpers + room validation
│   └── integration/                <- real-browser tests, skipped unless RUN_INTEGRATION=1
│       ├── conftest.py
│       └── test_smoke.py
│
├── .github/workflows/
│   └── ci.yml                      <- ruff + mypy --strict + pytest on push/PR
│
├── frontend/
│   └── index.html                  <- hls.js player + control panel at /
│
└── scripts/
    ├── dev.sh                      <- prepare Xvfb+Pulse and run the app locally
    └── shell.sh                    <- exec into the running container for debugging
```

## Architectural Decisions and Conventions

### Async-first
The codebase is `asyncio` throughout. FastAPI is async, Playwright async API is used (`from playwright.async_api import ...`), subprocess management goes through `asyncio.create_subprocess_exec`. **Do not introduce blocking calls in request handlers or coroutines.** If you need to call sync code, use `asyncio.to_thread`.

### Single-bot-per-container model
For now the bot supports exactly one active stream at a time per container. The room concept (`/stream/<room>/...`) exists in the path layout for future multi-bot support but the controller is a singleton. If you want multiple concurrent streams, the right move is to scale containers horizontally with a small reverse proxy, **not** to make `StreamController` multi-tenant. That keeps Xvfb/PulseAudio/Chromium isolated per stream.

### Pipeline lifecycle
`StreamController` has exactly four states: `idle`, `loading`, `playing`, `paused`. Transitions:

```
idle --(play(url))--> loading --(source ready)--> playing
playing <--(pause/resume)--> paused
{playing,paused,loading} --(stop)--> idle
```

State transitions are guarded by an `asyncio.Lock` on the controller. Never mutate state outside the controller.

### Source abstraction
Every stream source implements `StreamSource` (in `sources/base.py`). The abstract methods:

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

Sources are registered in `sources/__init__.py` via the `SOURCES` list. URL routing tries each source's `can_handle()` in order; first match wins. The generic `BrowserUrlSource` is always last and always handles, acting as a fallback.

To add a source, write a new file in `sources/`, subclass `StreamSource`, register it in the `SOURCES` list **before** `BrowserUrlSource`. A working example exists in `sources/youtube.py`.

### Capture pipeline
`pipeline/capture.py` spawns ffmpeg with these inputs:
- video: `x11grab` from `:99` at 30 fps, 1920x1080
- audio: PulseAudio monitor source `bot_sink.monitor`

ffmpeg outputs HLS segments to `/var/hls/<room>/`. nginx (separate container) serves them on port 8081. Keep the HLS settings (`hls_time`, `hls_list_size`, `hls_flags`) in the config file, do not hard-code them inside `capture.py`.

Encoder defaults:
- video: `libx264 -preset veryfast -tune zerolatency`, keyframe interval `SCREEN_FPS * HLS_SEGMENT_DURATION` (so segment boundaries land on keyframes)
- audio: `aac -b:a 128k -ar 44100 -ac 2`. Optional `loudnorm` filter via `AUDIO_LOUDNORM=true`.
- container: HLS, 2-second segments, 6-segment sliding window

These are tuned for ~4 second end-to-end latency. If you change them, document the tradeoff in the PR description.

### Browser/Playwright
`pipeline/browser.py` owns the Playwright lifecycle. The browser is **headful** (you'll see "headless: False" — this is intentional, headless can break video playback paths and DRM detection differently than headful). Single browser instance, single page, navigated by the active source.

The browser DISPLAY is `:99` (set via env var). Screen size is 1920x1080. Hardware acceleration is **disabled** (`--disable-gpu`) — this is required so that frames hit the X11 framebuffer where ffmpeg can grab them.

### PulseAudio routing
`docker/pulse/default.pa` creates a virtual sink `bot_sink` with a monitor source. Chromium is started with `PULSE_SINK=bot_sink` so all browser audio routes to the sink. ffmpeg captures from `bot_sink.monitor`. Don't change this without understanding the routing — getting "no audio" or "audio echoing" issues are 90% PulseAudio config problems.

### Configuration
All config goes through `config.py` (pydantic-settings reading from `.env`). Never read `os.environ` directly elsewhere. Do not hard-code paths, ports, sizes, or other tunables — add them to `Settings` instead. Existing config keys:

```python
class Settings(BaseSettings):
    BOT_API_KEY: str         # required; "changeme"/empty rejected at startup
    HLS_OUTPUT_DIR: Path = Path("/var/hls")
    DISPLAY: str = ":99"
    SCREEN_WIDTH: int = 1920
    SCREEN_HEIGHT: int = 1080
    SCREEN_FPS: int = 30
    PULSE_SINK: str = "bot_sink"
    HLS_SEGMENT_DURATION: int = 2
    HLS_PLAYLIST_SIZE: int = 6
    AUDIO_LOUDNORM: bool = False
    LOG_LEVEL: str = "INFO"
    DEFAULT_ROOM: str = "default"
```

### Logging
Use `structlog`. Every module gets its logger via `log = structlog.get_logger(__name__)`. Use key-value pairs:

```python
log.info("source.opened", url=url, source=src.__class__.__name__)
```

Don't use `print()`, don't use stdlib `logging` directly in module code (only `logging_setup.py` configures stdlib `logging` as a transport for structlog), don't f-string log messages — pass kwargs.

### Testing
- `pytest` + `pytest-asyncio`. All async tests use `@pytest.mark.asyncio`.
- `tests/test_sources.py`: unit-level, no Playwright/Xvfb. Use `unittest.mock`.
- `tests/test_api.py`: FastAPI `TestClient` against a controller with a mock pipeline.
- Integration tests that need a real browser are in `tests/integration/` and skipped by default. Set `RUN_INTEGRATION=1` to opt in (requires Xvfb + Chromium on the runner).

Running tests:
```bash
pytest                                              # fast unit tests only
RUN_INTEGRATION=1 pytest tests/integration/         # requires display + Chromium
```

### Code style
- Python 3.11+
- Type hints everywhere. `mypy --strict` runs in CI against `src/`. Tests stay non-strict (fixture indirection produces too many false positives).
- `ruff` for formatting and linting (see `pyproject.toml`).
- No 1-letter variable names except classic `i, j, k` in loops.
- Prefer dataclasses or pydantic models over dicts for any data crossing module boundaries.

### Error handling
- API endpoints return structured errors via FastAPI exception handlers — see `api/app.py`.
- The pipeline catches and logs, but never silently swallows. If a source fails to open, transition to `idle` and surface the error in `GET /status`.
- ffmpeg/Chromium subprocess crashes trigger automatic teardown of the whole pipeline; the controller goes back to `idle` and the next `POST /play` starts fresh.

## Common Tasks (How-To)

### Add a new (non-DRM) source

1. Create `src/ts6_stream_bot/sources/<name>.py`.
2. Subclass `StreamSource`, implement all abstract methods.
3. Add it to the `SOURCES` list in `sources/__init__.py`, **before** `BrowserUrlSource`.
4. Write tests in `tests/test_sources.py`.
5. Update `README.md` if the source needs special configuration.

Good reference: `sources/youtube.py`.

### Run locally for development

`scripts/dev.sh` brings up Xvfb + a PulseAudio sink and then `exec`'s the app — it's a one-shot, not a daemon you leave running in another terminal.

```bash
# Single terminal: prepare Xvfb + Pulse, then run the app
./scripts/dev.sh

# In another terminal: hit the API
curl localhost:8080/health
```

### Debug "the stream looks frozen"

In order:
1. Is ffmpeg still running? `docker compose exec bot ps aux | grep ffmpeg`
2. Are HLS segments being written? `docker compose exec bot ls -la /var/hls/default/`
3. Is the browser stuck? Hit `GET /debug/screenshot` (Playwright `page.screenshot()`) — returns a PNG of the current page. Requires `X-API-Key`.
4. PulseAudio sink alive? `docker compose exec bot pactl list sinks short`

### Update the encoder settings

Change defaults in `src/ts6_stream_bot/config.py` (`Settings`). Don't touch `pipeline/capture.py` constants. If the new setting is meaningful enough to override per-stream, plumb it through the API request schema.

## Things Not To Do

- Do not bypass DRM. Do not extract or bundle Widevine CDMs. Do not write Netflix/Disney+ specific source classes that decrypt streams.
- Do not connect to the TS6 voice protocol. This bot does HLS + browser. The voice side stays in TS6 itself.
- Do not run Chromium in `--no-sandbox` outside of Docker. In Docker it's required (no usable sandbox in containers without privileged mode); locally you should respect the sandbox.
- Do not introduce a database. State lives in memory in `StreamController`. If you really need persistence (e.g., last-played URL across restarts), use a single JSON file in a volume — not SQLite, not Redis.
- Do not add authentication beyond the simple `X-API-Key` header. If you need user-level auth, the right answer is to put this behind a reverse proxy that handles it.

## Out of Scope / Architectural Limits

These are not "TODOs to grab" — they are deliberate non-goals or things that
require a different approach than a code change here.

### Subtitles / closed captions
HLS itself supports `#EXT-X-MEDIA TYPE=SUBTITLES` with WebVTT segments, so on
paper this is a TODO. In practice, the capture pipeline grabs the rendered
**screen** (`x11grab`) — there is no separate subtitle track to multiplex into
HLS. To make subtitles work you would need a side channel: each `StreamSource`
extracts subtitle cues from the underlying media and the capture pipeline
writes them to a parallel `subs.m3u8` playlist. That is real work, not a quick
plumbing job, and most browser-rendered sources (YouTube, Twitch) burn captions
into the video frame anyway. If this is wanted, the right starting point is
`StreamSource` + a new `SubtitleTrack` abstraction; do **not** try to bolt it
into `pipeline/capture.py` directly.

### Multi-room / multi-stream
One container runs exactly one Xvfb + one PulseAudio sink + one Chromium. Two
concurrent streams would either share a display (bad — the browser windows
would overlap and ffmpeg would capture both) or require parallel Xvfb/Pulse
setups inside one container (operationally fragile). The supported answer is
**horizontal scaling**: run more containers, route to them with a small reverse
proxy (`/stream/<room>/...` → the right container). Do not turn
`StreamController` into a multi-tenant dispatcher.

### DRM-protected platforms (Netflix, Disney+, Prime Video, HBO Max, …)
Out of scope, will not be added. See "Operator-Implemented Parts" above.

## Open TODOs (Good First Tasks)

- Per-source rate-limit / retry on `source.open()` failures
- A small Vue/HTMX rewrite of `frontend/index.html` (current is hand-written JS)
- Optional `yt-dlp` URL pre-resolution for cases where the browser-rendered
  YouTube page is heavier than necessary

## Contact / Maintainership

This is a personal project; there's no team. If Claude Code is working on this autonomously and gets stuck on a design decision not covered here, prefer leaving a clear `# TODO(human):` comment with the question rather than guessing.
