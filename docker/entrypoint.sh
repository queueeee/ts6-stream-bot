#!/usr/bin/env bash
# Bootstrap: virtual display + virtual audio, then exec the app.
set -euo pipefail

log() { echo "[entrypoint] $*"; }

# Display number stripped of the leading colon, e.g. ":99" -> "99".
DISPLAY_NUM="${DISPLAY#:}"
XVFB_LOCK="/tmp/.X${DISPLAY_NUM}-lock"
XVFB_SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"

# PulseAudio runtime + lock locations. With Docker `restart: unless-stopped`
# the container's filesystem (incl. /run and /root) persists across PID-1
# restarts, so a previous PulseAudio's pid file blocks the new one with
# "Daemon already running. pa_pid_file_create() failed."
PULSE_PIDS=(
  "/run/pulse/pid"
  "/var/run/pulse/pid"
  "/root/.config/pulse/pid"
  "/tmp/pulse/pid"
)

cleanup() {
  log "shutting down ..."
  [[ -n "${XVFB_PID:-}" ]] && kill "$XVFB_PID" 2>/dev/null || true
  [[ -n "${PULSE_PID:-}" ]] && kill "$PULSE_PID" 2>/dev/null || true
  # Belt-and-suspenders: wipe leftovers so the next start doesn't trip on
  # "Server is already active for display N" / "Daemon already running".
  rm -f "$XVFB_LOCK" "$XVFB_SOCKET" 2>/dev/null || true
  rm -f "${PULSE_PIDS[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Same hygiene up-front, in case the previous run died without our trap firing
# (e.g. SIGKILL from a crash).
rm -f "$XVFB_LOCK" "$XVFB_SOCKET" 2>/dev/null || true
rm -f "${PULSE_PIDS[@]}" 2>/dev/null || true

# --- Xvfb ------------------------------------------------------------------
log "starting Xvfb on $DISPLAY (${SCREEN_WIDTH:-1920}x${SCREEN_HEIGHT:-1080}x24)"
Xvfb "$DISPLAY" \
  -screen 0 "${SCREEN_WIDTH:-1920}x${SCREEN_HEIGHT:-1080}x24" \
  -nolisten tcp \
  -ac \
  &
XVFB_PID=$!

# Wait until the display socket exists. We check for the unix socket directly
# rather than running xdpyinfo, because x11-utils isn't in the runtime image
# (was the cause of the previous "did not come up in time" loop) and because
# the socket appearing is a sufficient ready signal for x11grab + Chromium.
for _ in {1..50}; do
  if [[ -S "$XVFB_SOCKET" ]]; then break; fi
  sleep 0.1
done
if [[ ! -S "$XVFB_SOCKET" ]]; then
  log "ERROR: Xvfb did not come up in time (no socket at $XVFB_SOCKET)"
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
