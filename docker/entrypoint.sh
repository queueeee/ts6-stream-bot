#!/usr/bin/env bash
# Bootstrap: virtual display + virtual audio, then exec the app.
set -euo pipefail

log() { echo "[entrypoint] $*"; }

cleanup() {
  log "shutting down ..."
  [[ -n "${XVFB_PID:-}" ]] && kill "$XVFB_PID" 2>/dev/null || true
  [[ -n "${PULSE_PID:-}" ]] && kill "$PULSE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- Xvfb ------------------------------------------------------------------
log "starting Xvfb on $DISPLAY (${SCREEN_WIDTH:-1920}x${SCREEN_HEIGHT:-1080}x24)"
Xvfb "$DISPLAY" \
  -screen 0 "${SCREEN_WIDTH:-1920}x${SCREEN_HEIGHT:-1080}x24" \
  -nolisten tcp \
  -ac \
  &
XVFB_PID=$!

# Wait until display socket is ready
for _ in {1..30}; do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  log "ERROR: Xvfb did not come up in time"
  exit 1
fi
log "Xvfb ready"

# --- PulseAudio ------------------------------------------------------------
log "starting PulseAudio (per-container, no system mode)"
# Use the bundled config which sets up the bot_sink and its monitor source
pulseaudio \
  --exit-idle-time=-1 \
  --disallow-exit \
  --disallow-module-loading=false \
  --file=/app/docker/pulse/default.pa \
  --daemonize=no \
  &
PULSE_PID=$!

# Wait for pulse
for _ in {1..30}; do
  if pactl info >/dev/null 2>&1; then break; fi
  sleep 0.2
done
if ! pactl info >/dev/null 2>&1; then
  log "WARNING: PulseAudio did not come up in time - audio will not work"
fi

# Make sure our virtual sink is the default
pactl set-default-sink "${PULSE_SINK:-bot_sink}" 2>/dev/null || true
log "PulseAudio ready, default sink: $(pactl get-default-sink 2>/dev/null || echo unknown)"

# --- Hand off to app -------------------------------------------------------
log "starting app: $*"
exec "$@"
