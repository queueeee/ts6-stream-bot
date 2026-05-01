# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99 \
    PULSE_SERVER=unix:/tmp/pulse/native

# System dependencies:
# - xvfb: virtual X display
# - pulseaudio: virtual audio sink + monitor source
# - ffmpeg: video/audio capture (x11grab + pulse) - feeds the TS6 voice + WebRTC pipeline
# - chromium + deps: rendering engine for Playwright
# - dbus, fonts, etc: chromium runtime needs
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    pulseaudio \
    pulseaudio-utils \
    ffmpeg \
    dbus-x11 \
    procps \
    curl \
    ca-certificates \
    fonts-liberation \
    fonts-noto-color-emoji \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    pydantic-settings \
    playwright \
    structlog \
    yt-dlp \
    httpx \
    anyio \
    prometheus-client

# Playwright browser binaries (chromium only)
RUN playwright install chromium && \
    playwright install-deps chromium 2>/dev/null || true

# App source
COPY src/ ./src/
COPY docker/ ./docker/

# Make entrypoint executable
RUN chmod +x /app/docker/entrypoint.sh

# Pulse runtime dir
RUN mkdir -p /tmp/pulse && chmod 777 /tmp/pulse

EXPOSE 8080

ENV PYTHONPATH=/app/src

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-m", "ts6_stream_bot"]
