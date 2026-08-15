#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Kasper Fabæch Brandt
# Diff the fork ffmpeg's encoder list against the stock jellyfin-ffmpeg's.
#
# Why: the shim rewrites only the *video* side of Jellyfin's graph and passes
# everything else (notably -codec:a) through verbatim into the fork's command
# line. So any encoder the stock build has and the fork lacks is a live
# playback failure waiting to happen ("Encoder not found" -> "Source error" in
# the client) — which is exactly how the libmp3lame bug reached production.
#
# This is deliberately a *deploy-time* check. The shim itself has no
# encoder-availability guard and must not grow one: degrading to a software
# transcode would turn a build defect into a quiet performance regression
# instead of a loud failure. Fix the gap in the fork build (or in rules.toml),
# not by teaching the shim to route around it.
#
#   ./check-encoders.sh [FORK_FFMPEG] [STOCK_FFMPEG]
#
# FORK_FFMPEG defaults to a sibling checkout of the fork
# (../ffmpeg-jellyfin/ffmpeg — https://github.com/poizan42/jellyfin-rpi-ffmpeg);
# STOCK_FFMPEG defaults to the `ffmpeg` path in rules.toml.
#
# Exit 0 = no unexpected gap, 1 = gap (or a binary that can't be queried).
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
FORK="${1:-$SRC/../ffmpeg-jellyfin/ffmpeg}"
STOCK="${2:-$(sed -nE 's/^ffmpeg[[:space:]]*=[[:space:]]*"(.*)".*/\1/p' "$SRC/rules.toml" | head -1)}"
STOCK="${STOCK:-/usr/lib/jellyfin-ffmpeg/ffmpeg}"

# Encoders the fork intentionally does not build. Keep this list short and
# justified — every entry is a promise that Jellyfin will never ask for it on
# this box. See ffmpeg-jellyfin/README.jellyfin-rpi.md -> "Building".
#   *_nvenc, *_rkmpp, *_qsv, *_vaapi, *_amf : other vendors' hardware
#   libfdk_aac                              : nonfree, and we have the native aac
#   libsvtav1 / libtheora                   : no AV1 encode target; theora is dead
#   sonic / sonicls                         : experimental, never selected
EXPECTED_MISSING='^(av1|h264|hevc|mjpeg|mpeg2|vp8|vp9|vp9_qsv)_(nvenc|rkmpp|qsv|vaapi|amf|v4l2m2m_other)$|^libfdk_aac$|^libsvtav1$|^libtheora$|^sonic(ls)?$'

enc_list() {  # $1 = binary
  "$1" -hide_banner -encoders 2>/dev/null | sed -nE 's/^ [A-Z.]{6} ([^ ]+).*/\1/p' | sort -u
}

for b in "$FORK" "$STOCK"; do
  [ -x "$b" ] || { echo "check-encoders: not executable: $b" >&2; exit 1; }
done

f_list="$(enc_list "$FORK")"
s_list="$(enc_list "$STOCK")"
[ -n "$f_list" ] && [ -n "$s_list" ] || {
  echo "check-encoders: could not read an encoder list (fork=$(echo "$f_list" | grep -c .) stock=$(echo "$s_list" | grep -c .))" >&2
  exit 1
}

gap="$(comm -13 <(echo "$f_list") <(echo "$s_list"))"
unexpected="$(echo "$gap" | grep -vE "$EXPECTED_MISSING" | grep . || true)"
expected="$(echo "$gap" | grep -E "$EXPECTED_MISSING" | grep . || true)"

echo "encoder check: fork=$(echo "$f_list" | grep -c .)  stock=$(echo "$s_list" | grep -c .)  gap=$(echo "$gap" | grep -c .)"
[ -n "$expected" ] && echo "  intentionally absent: $(echo "$expected" | tr '\n' ' ')"

if [ -n "$unexpected" ]; then
  cat >&2 <<EOF

  ##############################################################################
  ## ENCODER GAP — the fork is missing encoders the stock build provides:
  ##
$(echo "$unexpected" | sed 's/^/  ##   /')
  ##
  ## Jellyfin builds its command line for the stock build, and the shim passes
  ## audio (and any non-rewritten) encoder args through untouched. If Jellyfin
  ## picks one of the above, the transcode dies with "Encoder not found".
  ##
  ## Fix the fork build (ffmpeg-jellyfin/README.jellyfin-rpi.md -> "Building"),
  ## or add the encoder to EXPECTED_MISSING here with a justification.
  ##############################################################################

EOF
  exit 1
fi

echo "  no unexpected gap — the fork can run anything the stock build can."
