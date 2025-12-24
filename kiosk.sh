#!/bin/bash
set -euo pipefail

APP_URL="http://127.0.0.1:5000/healthz"
TARGET_URL="${TARGET_URL:-http://localhost:5000/screensaver}"

# Use the active desktop session (DISPLAY and XAUTHORITY are usually correct for X11)
export DISPLAY=${DISPLAY:-:0}
export XAUTHORITY=${XAUTHORITY:-/home/rgeorgiev/.Xauthority}

# Wait until the app is actually reachable (no fixed limit; exit only on success)
echo "[kiosk] Waiting for app at $APP_URL …"
until curl -sSf "$APP_URL" >/dev/null; do
  sleep 2
done
echo "[kiosk] App is up, launching Chromium to $TARGET_URL"

# Skip X11 xset calls under Wayland
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
  xset -dpms || true
  xset s off || true
  xset s noblank || true
else
  echo "[kiosk] Wayland detected; skipping xset DPMS/blanking"
fi

# Prefer 'chromium' (Bookworm) or fallback to 'chromium-browser' (older)
CHROMIUM_BIN="$(command -v chromium || true)"
if [[ -z "$CHROMIUM_BIN" ]]; then
  CHROMIUM_BIN="$(command -v chromium-browser || true)"
fi
if [[ -z "$CHROMIUM_BIN" ]]; then
  echo "[kiosk] Chromium not found. Install: sudo apt-get install -y chromium chromium-browser" >&2
  exit 1
fi

# On Wayland, hint Chromium to use Ozone/Wayland; safe on X11 (ignored)
EXTRA_FLAGS=()
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
  EXTRA_FLAGS+=(--ozone-platform=wayland --enable-features=UseOzonePlatform,WaylandWindowDecorations --use-gl=egl)
fi

exec "$CHROMIUM_BIN" \
  --kiosk \
  --noerrdialogs \
  --disable-session-crashed-bubble \
  --autoplay-policy=no-user-gesture-required \
  --incognito \
  "${EXTRA_FLAGS[@]}" \
  "$TARGET_URL"
