#!/bin/bash
# Live end-to-end checks for the shim. Requires the RPi hardware + fork build.
# Not run in CI; run manually on the Pi.
#
#   SAMPLES=/path/to/dir SAMPLE=some-4k-hevc.mkv bash tests/live.sh
#
# SAMPLE must be a 4K HEVC file (any container the fork can demux) — the tests
# assert that the hardware rules fire, which needs a source rpivid can decode.
#
# WARNING: test 4 SIGKILLs a running hardware encode on purpose, to prove the
# admission slot is released. On some RPi kernels that wedges the bcm2835
# encoder (/dev/video11) until reboot — do not run it on a box you need up.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHIM="$ROOT/bin/ffmpeg"
D="${SAMPLES:?set SAMPLES=/path/to/sample/dir}"
SAMPLE="${SAMPLE:?set SAMPLE=<4K HEVC file in $SAMPLES>}"
SLOTS=/tmp/rpi-orch-slots
export RPI_ORCH_RULES="$ROOT/rules.toml"

# A Jellyfin-shaped 4K->720p command with tone-map, output discarded.
jf() {  # $1 = input
  local vf='tonemapx=tonemap=hable,scale=trunc(min(max(iw\,ih*a)\,min(1280\,720*a))/64)*64:trunc(min(max(iw/a\,ih)\,min(1280/a\,720))/2)*2,format=yuv420p'
  echo -n "-analyzeduration 200M -probesize 1G -i $1 -map_metadata -1 -codec:v:0 h264_v4l2m2m -b:v 3M -vf $vf -f null -"
}

pass=0; fail=0
ok(){ echo "  PASS: $1"; pass=$((pass+1)); }
no(){ echo "  FAIL: $1"; fail=$((fail+1)); }

echo "=== 1. single session engages + transcodes ==="
rm -rf "$SLOTS"
out=$(timeout 120 "$SHIM" $(jf "$D/$SAMPLE") 2>&1)
echo "$out" | grep -q "\[rpi-orch\] engaged: rule='hw decode, large downscale" && ok "engaged large-downscale rule" || no "did not engage expected rule"
echo "$out" | grep -qoE 'frame=[ ]*[0-9]+' && ok "produced frames" || no "no frames"
echo "$out" | grep -a "\[rpi-orch\]" | head -1 | sed 's/^/    /'

echo "=== 2. admission control: 3x concurrent 4K, hw_slots=1 ==="
rm -rf "$SLOTS"
for i in 1 2 3; do
  timeout 150 "$SHIM" $(jf "$D/$SAMPLE") > /tmp/orch_s$i.log 2>&1 &
done
wait
hw=$(grep -l "engaged: rule='hw decode" /tmp/orch_s*.log 2>/dev/null | wc -l)
sw=$(grep -l "engaged: rule='software decode" /tmp/orch_s*.log 2>/dev/null | wc -l)
fails=$(grep -lE 'Cannot allocate|Segmentation|no usable mapping' /tmp/orch_s*.log 2>/dev/null | wc -l)
echo "    hw-rule sessions=$hw  software-rule sessions=$sw  cma-failures=$fails"
[ "$hw" -eq 1 ] && ok "exactly one HW session admitted" || no "expected 1 HW session, got $hw"
[ "$sw" -eq 2 ] && ok "two fell back to software" || no "expected 2 software, got $sw"
[ "$fails" -eq 0 ] && ok "no CMA failures/crashes" || no "$fails sessions hit CMA errors"

echo "=== 3. fail-safe: bad rules file -> passthrough (no engage, still runs) ==="
RPI_ORCH_RULES=/nonexistent/rules.toml timeout 60 "$SHIM" $(jf "$D/$SAMPLE") > /tmp/orch_fs.log 2>&1
grep -q "\[rpi-orch\] engaged" /tmp/orch_fs.log && no "engaged despite missing rules" || ok "did not engage (passthrough)"
# passthrough runs the raw jf command; the software scale filter has no HW, so it
# still *runs* (may be slow) — we only assert it didn't crash on our account.
grep -qiE 'Segmentation|Traceback' /tmp/orch_fs.log && no "crashed in passthrough" || ok "passthrough clean"

echo "=== 4. slot released on SIGKILL ==="
rm -rf "$SLOTS"
timeout 150 "$SHIM" $(jf "$D/$SAMPLE") >/tmp/orch_k1.log 2>&1 &
k1=$!
sleep 6
grep -q "engaged: rule='hw decode" /tmp/orch_k1.log && ok "first session took HW slot" || no "first session not on HW"
kill -9 $k1 2>/dev/null; wait $k1 2>/dev/null
pkill -9 -f "$(basename "$D")" 2>/dev/null; sleep 1
timeout 60 "$SHIM" $(jf "$D/$SAMPLE") >/tmp/orch_k2.log 2>&1 &
k2=$!; sleep 5
grep -q "engaged: rule='hw decode" /tmp/orch_k2.log && ok "slot freed on kill, next session got HW" || no "slot not released"
kill -9 $k2 2>/dev/null; wait 2>/dev/null
pkill -9 -f "$(basename "$D")" 2>/dev/null

echo "=== RESULT: pass=$pass fail=$fail ==="
rm -f /tmp/orch_s*.log /tmp/orch_fs.log /tmp/orch_k*.log
