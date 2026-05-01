#!/usr/bin/env bash
# Run a local dev cycle. Requires Xvfb + PulseAudio + Chromium installed locally.
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export PULSE_SINK="${PULSE_SINK:-bot_sink}"

# Start Xvfb if not already running
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  echo "[dev] starting Xvfb $DISPLAY"
  Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp -ac &
  sleep 1
fi

# Pulse - assume system PulseAudio is up; create the bot_sink if missing
if ! pactl list sinks short | grep -q "$PULSE_SINK"; then
  pactl load-module module-null-sink sink_name="$PULSE_SINK" >/dev/null
fi

# Run app
cd "$(dirname "$0")/.."
exec python -m ts6_stream_bot
