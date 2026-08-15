#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Kasper Fabæch Brandt
# Install the orchestrator shim to a root-owned, world-traversable prefix so the
# Jellyfin service user can execute it — a checkout under a home directory is
# typically mode 0700, which the `jellyfin` user cannot even traverse (this
# blocks the fork ffmpeg it execs as much as the shim itself; systemd's
# ProtectHome is not the issue, plain Unix permissions are). Copies the shim,
# the rules, and the fork ffmpeg (a static libav build — self-contained, only
# system .so deps), then writes an installed rules.toml pointing at the copy.
#
# Run as root:   sudo ./install.sh [PREFIX]
# Re-run it after rebuilding the fork ffmpeg to refresh the copied binary.
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

# The fork must be able to run anything the stock build can — the shim rewrites
# only the video side of the graph and passes the rest (notably -codec:a)
# through. A gap here is a hard "Encoder not found" at playback time, so refuse
# to deploy: catching it now is the whole point, and the shim deliberately has
# no runtime fallback that would soften it into a slow-but-working transcode.
if ! "$SRC/check-encoders.sh" "$FORK/ffmpeg"; then
  echo "error: refusing to install a fork build that is missing encoders." >&2
  echo "       Rebuild it, or re-run with SKIP_ENCODER_CHECK=1 to override." >&2
  [ "${SKIP_ENCODER_CHECK:-0}" = 1 ] || exit 1
  echo "warning: SKIP_ENCODER_CHECK=1 — installing anyway." >&2
fi

echo "installing to $PREFIX  (fork ffmpeg from $FORK)"
install -d -m 0755 "$PREFIX" "$PREFIX/bin"

# Only the fork *ffmpeg* needs copying out (a symlink can't help — its target
# under /home stays behind the 0700 wall). Static build, so the copy runs
# standalone. Passthrough + probe use the stock jellyfin-ffmpeg in place, which
# the service can already reach, so no other binaries are copied.
install -m 0755 "$FORK/ffmpeg" "$PREFIX/ffmpeg-real"

# Shim + helper.
install -m 0755 "$SRC/bin/ffmpeg"  "$PREFIX/bin/ffmpeg"
install -m 0755 "$SRC/bin/ffprobe" "$PREFIX/bin/ffprobe"
install -m 0644 "$SRC/bin/_orch.py" "$PREFIX/bin/_orch.py"

# Installed rules.toml: copy, then repoint hw_ffmpeg at the copied fork build.
# ffmpeg/ffprobe stay as the repo's stock jellyfin-ffmpeg paths.
sed -E \
  -e "s|^hw_ffmpeg[[:space:]]*=.*|hw_ffmpeg = \"$PREFIX/ffmpeg-real\"|" \
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
echo "  2. sudo systemctl restart jellyfin"
echo "  to revert: restore the original --ffmpeg=/usr/lib/jellyfin-ffmpeg/ffmpeg and restart."
