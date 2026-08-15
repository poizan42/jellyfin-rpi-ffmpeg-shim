#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Kasper Fabæch Brandt
# Diff what the fork ffmpeg can do against the stock jellyfin-ffmpeg.
#
# Why: the shim execs the fork for EVERYTHING -- both the rewritten hardware
# graph and every command it passes through untouched (see rules.toml). That is
# deliberate: the fork also carries software-decode optimisations, which pay off
# precisely on the commands that cannot go on the hardware. The price is that
# the fork must be a superset of the stock build, because Jellyfin builds its
# command lines for the stock build. Anything it has and the fork lacks -- an
# encoder, decoder, filter, muxer, demuxer, bitstream filter, protocol -- is a
# live playback failure waiting to happen ("Encoder not found" -> "Source error"
# in the client), which is exactly how the libmp3lame gap reached production.
#
# This is deliberately a *deploy-time* check. The shim has no runtime capability
# guard and must not grow one: routing around a gap at transcode time would turn
# a build defect into a quiet regression instead of a loud failure. Fix the gap
# in the fork build, or -- if you must run a non-superset fork -- set
# `passthrough_ffmpeg` in rules.toml, which is an explicit, documented choice.
#
#   ./check-capabilities.sh [FORK_FFMPEG] [STOCK_FFMPEG]
#
# FORK_FFMPEG defaults to a sibling checkout of the fork
# (../ffmpeg-jellyfin/ffmpeg — https://github.com/poizan42/jellyfin-rpi-ffmpeg);
# STOCK_FFMPEG defaults to the stock jellyfin-ffmpeg.
#
# Exit 0 = no unexpected gap, 1 = gap (or a binary that can't be queried).
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
FORK="${1:-$SRC/../ffmpeg-jellyfin/ffmpeg}"
STOCK="${2:-/usr/lib/jellyfin-ffmpeg/ffmpeg}"

# Capabilities the fork intentionally does not build. Keep this list short and
# justified — every entry is a promise that Jellyfin will never need it here.
#   *_nvenc/_cuvid/_rkmpp/_qsv/_vaapi/_amf, cuda/opencl/rkrga filters
#                          : other vendors' hardware, unusable on a Pi
#   libfdk_aac             : nonfree; the native aac encoder/decoder covers it
#   libsvtav1 / libtheora  : no AV1 encode target here; theora is dead
#   sonic / sonicls        : experimental encoders, never selected
EXPECTED_MISSING='_(nvenc|cuvid|rkmpp|rkrga|qsv|vaapi|amf|cuda|opencl)$|^(cuda|opencl|rkmpp)$|^libfdk_aac$|^libsvtav1$|^libtheora$|^sonic(ls)?$'

# Capability kinds to compare. The listing formats differ per kind AND across
# FFmpeg versions (8.x dropped the filter "command support" flag, so that column
# is 2 chars there and 3 in 7.x) — hence a parser per kind rather than one
# regex, and a hard error if one yields nothing.
KINDS="decoders encoders filters muxers demuxers bsfs protocols"

cap_list() {  # $1 = binary, $2 = kind
  case "$2" in
    # " TSC name  A->A  desc" (7.x) / " TS name  A->A  desc" (8.x)
    filters)         "$1" -hide_banner -filters 2>/dev/null |
                       sed -nE 's/^ [A-Z.]{2,3} +([A-Za-z0-9_]+) +[AVN|]+->.*/\1/p' ;;
    # bare names, one per line, after a "Bitstream filters:" header
    bsfs)            "$1" -hide_banner -bsfs 2>/dev/null |
                       sed -nE 's/^([a-z][a-z0-9_]*)$/\1/p' ;;
    # indented bare names under "Input:"/"Output:" headers
    protocols)       "$1" -hide_banner -protocols 2>/dev/null |
                       sed -nE 's/^[[:space:]]+([A-Za-z][A-Za-z0-9_+.-]*)$/\1/p' ;;
    # "  E  name   long name"  (flags are D/E/d in a 3-wide field)
    muxers|demuxers) "$1" -hide_banner -"$2" 2>/dev/null |
                       sed -nE 's/^ [DEd. ]{3} ([A-Za-z0-9][A-Za-z0-9_,+-]*) .*/\1/p' ;;
    # " V....D name  long name" — 6 flag chars
    *)               "$1" -hide_banner -"$2" 2>/dev/null |
                       sed -nE 's/^ [A-Z.]{6} ([^ ]+).*/\1/p' ;;
  esac | sort -u
}

for b in "$FORK" "$STOCK"; do
  [ -x "$b" ] || { echo "check-capabilities: not executable: $b" >&2; exit 1; }
done

rc=0
for kind in $KINDS; do
  f_list="$(cap_list "$FORK" "$kind")"
  s_list="$(cap_list "$STOCK" "$kind")"
  nf=$(echo "$f_list" | grep -c .); ns=$(echo "$s_list" | grep -c .)
  if [ "$nf" -eq 0 ] || [ "$ns" -eq 0 ]; then
    echo "check-capabilities: could not read $kind (fork=$nf stock=$ns)" >&2
    rc=1; continue
  fi

  gap="$(comm -13 <(echo "$f_list") <(echo "$s_list"))"
  unexpected="$(echo "$gap" | grep -vE "$EXPECTED_MISSING" | grep . || true)"
  expected="$(echo "$gap" | grep -E "$EXPECTED_MISSING" | grep . || true)"
  ng=$(echo "$gap" | grep -c .)

  printf '%-10s fork=%-4s stock=%-4s gap=%s\n' "$kind" "$nf" "$ns" "$ng"
  [ -n "$expected" ] && echo "    intentionally absent: $(echo "$expected" | tr '\n' ' ')"

  if [ -n "$unexpected" ]; then
    rc=1
    cat >&2 <<EOF

  ##############################################################################
  ## CAPABILITY GAP ($kind) — present in the stock build, missing from the fork:
  ##
$(echo "$unexpected" | sed 's/^/  ##   /')
  ##
  ## The shim execs the fork for EVERY command, including the ones it passes
  ## through untouched, and Jellyfin builds those for the stock build. If it
  ## picks one of the above, that transcode fails outright.
  ##
  ## Fix the fork build (ffmpeg-jellyfin/README.jellyfin-rpi.md -> "Building"),
  ## add the entry to EXPECTED_MISSING here with a justification, or set
  ## passthrough_ffmpeg in rules.toml to send unrewritten commands to the stock
  ## build (giving up the fork's software-decode optimisations on that path).
  ##############################################################################

EOF
  fi
done

[ "$rc" -eq 0 ] && echo "OK — the fork can run anything the stock build can."
exit "$rc"
