# ts6-stream-bot

Self-hosted streaming companion for a TeamSpeak 6 server. Runs as a TS6
client (audio + screen-share over the server's built-in stream feature),
rendering YouTube / Twitch / direct files / arbitrary URLs in a controlled
Chromium and pushing the captured frames into a TS6 channel. Voice runs
over TS6 itself — there's no separate web player.

> **Status:** mid-pivot. Phases 1–3 (native TS6 voice client + WebRTC video)
> are in progress. Phase 0 has stripped the previous HLS-based output, so
> the bot currently renders sources but produces no audio/video output.
> See `CLAUDE.md` for the staged plan.

## Architecture (target)

```
+----------------------------------------------------------+
|  Docker container "ts6-stream-bot"                       |
|                                                          |
|  +----------+   X11   +----------+                       |
|  | Xvfb     |<--------| Chromium |                       |
|  | :99      |         | (Playwr) |                       |
|  +----------+         +----------+                       |
|         ^                  ^                             |
|         | x11grab          | Playwright                  |
|         |                  |                             |
|  +------v------+      +----+--------+                    |
|  | ffmpeg      |      | FastAPI     |                    |
|  | x11grab +   |      | REST API    |                    |
|  | pulse mon.  |      | (port 8080) |                    |
|  +------+------+      +-------------+                    |
|         | RTP                                             |
|         v                                                 |
|  +-------------+      +----------------+                  |
|  | aiortc      |      | TS3 voice      |--UDP voice -+   |
|  | WebRTC peer |<-----| client (.py)   |             |   |
|  | (per viewer)|      | + signaling    |             |   |
|  +------+------+      +----------------+             |   |
|         |                                            |   |
+---------|--------------------------------------------|---+
          | WebRTC                                     | TS3 protocol
          v                                            v
   TS6 viewers in channel                       TS6 server
```

## REST API

| Method | Path                | Auth | Purpose |
|--------|---------------------|------|---------|
| GET    | `/health`           | —    | Liveness probe |
| GET    | `/status`           | —    | Current state, URL, source class |
| GET    | `/metrics`          | —    | Prometheus metrics |
| POST   | `/play`             | key  | `{"url": "..."}` |
| POST   | `/pause`            | key  | Pause active source |
| POST   | `/resume`           | key  | Resume paused source |
| POST   | `/seek`             | key  | `{"seconds": 42}` |
| POST   | `/stop`             | key  | Tear down active source |
| GET    | `/debug/screenshot` | key  | PNG of current page (or 409) |
| GET    | `/debug/audio`      | key  | List PulseAudio sinks |

Auth is `X-API-Key: <BOT_API_KEY>`.

## Quickstart

Prerequisites: Docker + an existing `ts6-net` Docker network shared with
your TS6 server.

```bash
git clone <your-repo-url> ts6-stream-bot
cd ts6-stream-bot
cp .env.example .env
# Edit .env: set BOT_API_KEY and (once phase 1 is done) the TS6_* values
echo "BOT_API_KEY=$(openssl rand -base64 32)" >> .env

docker compose up -d --build
docker compose logs -f
```

```bash
# Health check
curl http://localhost:8080/health

# Start playback (renders the page; output to TS6 lights up after phase 1+)
curl -X POST http://localhost:8080/play \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $BOT_API_KEY" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

## Sources

Pluggable: subclass `StreamSource` under `sources/`, register it in the
`SOURCES` list in `sources/__init__.py` (before the `BrowserUrlSource`
catch-all). Existing sources: YouTube, Twitch (channels + clips), direct
files (`.mp4` / `.mkv` / `.m3u8`), generic browser URL.

DRM-protected platforms (Netflix, Disney+, Prime Video, …) are out of
scope for this codebase. There's a wired discovery slot for
operator-local sources at `sources/_operator_implemented/` — anything
you put there is your responsibility, not Claude's.

## Where to go next

For architecture details, conventions, and the staged pivot plan, read
[`CLAUDE.md`](./CLAUDE.md).

## Acknowledgements

The TS3 voice client + WebRTC stream signaling are being ported from
[`clusterzx/ts6-manager`](https://github.com/clusterzx/ts6-manager) (MIT).
See `LICENSE-third-party.md` once phase 1 lands.

## License

MIT — see `LICENSE`.
