# ts6-stream-bot

Self-hosted streaming companion for a TeamSpeak 6 server. Spins up a headful Chromium in a virtual display, captures the browser frame, encodes it to HLS via ffmpeg, and exposes the stream over HTTP. Users open the stream URL in their browser and watch synchronously while voice runs over TS6.

This is a developer-friendly skeleton — the streaming/encoding/control plane is fully implemented; the parts that touch DRM-protected sources are intentionally left as `NotImplementedError` for the operator to fill in.

## Features

- **Headful Chromium** in `Xvfb` virtual display (1920x1080), driven by Playwright
- **HLS output** via ffmpeg (h264 + aac, low-latency mode)
- **REST API** to drive the bot: `POST /play`, `POST /pause`, `POST /seek`, `GET /status`
- **Pluggable sources**: abstract `StreamSource` base class; ships with `YoutubeSource`, `DirectFileSource`, `BrowserUrlSource`
- **Single Docker container** that drops cleanly into your existing `ts6-net` Docker network alongside the TS6 server and manager

## Architecture (high level)

```
+--------------------------------------------------+
|  Docker container "ts6-stream-bot"               |
|                                                  |
|  +----------+   X11   +----------+               |
|  | Xvfb     |<--------| Chromium |               |
|  | :99      |         | (Playwr) |               |
|  +----------+         +----------+               |
|         ^                  ^                     |
|         |                  | controls            |
|         | screen capture   |                     |
|         |                  |                     |
|  +------v---------+   +----+--------+            |
|  | ffmpeg         |   | FastAPI     |            |
|  | x11grab+pulse  |   | REST + WS   |            |
|  | -> HLS segs    |   | (port 8080) |            |
|  +-------+--------+   +-------------+            |
|          |                                       |
|          v                                       |
|  /var/hls/<room>/index.m3u8                      |
+----------|---------------------------------------+
           |
           | HTTP (port 8081, nginx)
           v
       Users open the .m3u8 URL in browser
```

## Quickstart

Prerequisites: Docker + the existing `ts6-net` network from the TS6 setup.

```bash
git clone <your-repo-url> ts6-stream-bot
cd ts6-stream-bot
cp .env.example .env
# edit .env if needed (API key for control endpoint, exposed ports)

docker compose up -d --build
docker compose logs -f
```

Once running:

```bash
# Health check
curl http://193.34.69.21:8080/health

# Start playback
curl -X POST http://193.34.69.21:8080/play \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $BOT_API_KEY" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'

# Users open this URL in their browser to watch:
# http://193.34.69.21:8081/stream/default/index.m3u8
```

For voice, users join the configured TS6 channel as usual.

## Where to go next

If you want to extend the bot — especially around adding new stream sources — read [`CLAUDE.md`](./CLAUDE.md). It is written as a briefing for Claude Code and covers architecture, conventions, where to plug in new sources, and what is intentionally left unimplemented.

## License

MIT — see `LICENSE`.
