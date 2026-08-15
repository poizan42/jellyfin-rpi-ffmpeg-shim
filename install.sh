#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Kasper Fabæch Brandt
# Install the orchestrator shim to a root-owned, world-traversable prefix so the
# Jellyfin service user can execute it — a checkout under a home directory is
# typically mode 0700, which the `jellyfin` user cannot even traverse (this
# blocks the fork ffmpeg it execs as much as the shim itself; systemd's
# ProtectHome is not the issue, plain Unix permissions are). Copies the shim,
# the rules, and the fork ffmpeg/ffprobe (a static libav build — self-contained,
# only system .so deps), then writes an installed rules.toml pointing at them.
#
# Run as root:   sudo ./install.sh [PREFIX]
# Re-run it after rebuilding the fork ffmpeg to refresh the copied binaries.
#
# FORK_DIR overrides where the fork build is found (default: a sibling checkout
# of https://github.com/poizan42/jellyfin-rpi-ffmpeg at ../ffmpeg-jellyfin).
#
# It does NOT edit /etc/default/jellyfin or restart the service — it prints the
# one line to change and the restart command for you to apply.
set -euo pipefail

PREFIX="${1:-/opt/rpi-ffmpeg-orchestrator}"
SRC="$(cd "$(dirname "$0")" && pwd)"
FORK="${FORK_DIR:-$SRC/../ffmpeg-jellyfin}"
FORK="$(cd "$FORK" 2>/dev/null && pwd)" || {
  echo "error: fork checkout not found (set FORK_DIR=/path/to/jellyfin-rpi-ffmpeg)" >&2
  exit 1
}

if [ "$(id -u)" != 0 ]; then
  echo "error: must run as root (writes to $PREFIX). Re-run: sudo $0 $*" >&2
  exit 1
fi
for b in ffmpeg ffprobe; do
  [ -x "$FORK/$b" ] || { echo "error: fork $b not found/executable at $FORK/$b" >&2; exit 1; }
done

# The fork must be able to run anything the stock build can: the shim execs it
# for EVERY command, the passed-through ones included, and Jellyfin builds those
# for the stock build. A gap is a hard failure at playback time, so refuse to
# deploy — catching it now is the whole point, and the shim deliberately has no
# runtime fallback that would soften it into a slow-but-working transcode.
if ! "$SRC/check-capabilities.sh" "$FORK/ffmpeg"; then
  echo "error: refusing to install a fork build that is missing capabilities the" >&2
  echo "       stock build has. Rebuild it, set passthrough_ffmpeg in rules.toml," >&2
  echo "       or re-run with SKIP_CAPABILITY_CHECK=1 to override." >&2
  [ "${SKIP_CAPABILITY_CHECK:-0}" = 1 ] || exit 1
  echo "warning: SKIP_CAPABILITY_CHECK=1 — installing anyway." >&2
fi

echo "installing to $PREFIX  (fork ffmpeg from $FORK)"
install -d -m 0755 "$PREFIX" "$PREFIX/bin"

# Copy both fork binaries out (a symlink can't help — its target under a 0700
# home stays unreachable). Static build, so the copies run standalone. ffprobe
# is copied too because the shim runs EVERYTHING on the fork, probes included.
install -m 0755 "$FORK/ffmpeg"  "$PREFIX/ffmpeg-real"
install -m 0755 "$FORK/ffprobe" "$PREFIX/ffprobe-real"

# Shim + helper.
install -m 0755 "$SRC/bin/ffmpeg"  "$PREFIX/bin/ffmpeg"
install -m 0755 "$SRC/bin/ffprobe" "$PREFIX/bin/ffprobe"
install -m 0644 "$SRC/bin/_orch.py" "$PREFIX/bin/_orch.py"

# Installed rules.toml: copy, then repoint ffmpeg/ffprobe at the copies.
sed -E \
  -e "s|^ffmpeg([[:space:]]*)=.*|ffmpeg\1= \"$PREFIX/ffmpeg-real\"|" \
  -e "s|^ffprobe([[:space:]]*)=.*|ffprobe\1= \"$PREFIX/ffprobe-real\"|" \
  "$SRC/rules.toml" > "$PREFIX/rules.toml"
chmod 0644 "$PREFIX/rules.toml"

# Belt-and-braces: everything world-readable/traversable regardless of umask.
chmod -R a+rX "$PREFIX"

echo
echo "installed:"
ls -l "$PREFIX" "$PREFIX/bin"
echo
echo "next (manual — not done by this script):"
echo "  1. edit /etc/default/jellyfin, set:"
echo "       JELLYFIN_FFMPEG_OPT=\"--ffmpeg=$PREFIX/bin/ffmpeg\""
echo "  2. sudo systemctl restart jellyfin   <-- REQUIRED even if step 1 is"
echo "     already set. Jellyfin probes ffmpeg's encoders/filters ONCE at"
echo "     startup and builds every later command from that cached answer."
echo "     A running Jellyfin will keep asking this fresh binary for encoders"
echo "     the previous one had, and those transcodes fail with"
echo "     \"Unknown encoder\"."
echo "  to revert: restore the original --ffmpeg=/usr/lib/jellyfin-ffmpeg/ffmpeg and restart."
