#!/bin/sh
set -eu

PROFILE_DIR="${PROFILE_DIR:-/data/profile}"
INTERNAL_CDP_PORT="${INTERNAL_CDP_PORT:-9223}"
EXTERNAL_CDP_PORT="${EXTERNAL_CDP_PORT:-9222}"

mkdir -p "$PROFILE_DIR"
chown chrome:chrome "$PROFILE_DIR"

# Chromium 152 binds CDP to its own loopback even when asked for 0.0.0.0.
# Export it to the container interface through socat; Apple Container then
# publishes this port only on the Mac's 127.0.0.1.
socat \
  "TCP-LISTEN:${EXTERNAL_CDP_PORT},bind=0.0.0.0,fork,reuseaddr" \
  "TCP:127.0.0.1:${INTERNAL_CDP_PORT}" &

exec runuser -u chrome -- /usr/bin/chromium \
  --headless=new \
  --remote-debugging-port="$INTERNAL_CDP_PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-background-networking \
  --disable-sync \
  --disable-features=Translate \
  --no-first-run \
  --no-default-browser-check \
  --disable-extensions-except=/opt/ubol \
  --load-extension=/opt/ubol \
  about:blank
