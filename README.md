# transcode-orchestrator

*Drop-in `--ffmpeg=` shim that routes Jellyfin's transcodes onto the Raspberry Pi 4
video hardware, using the [jellyfin-rpi-ffmpeg](https://github.com/poizan42/jellyfin-rpi-ffmpeg)
build. No Jellyfin rebuild, no patched server — one reversible line in
`/etc/default/jellyfin`.*

A thin **policy shim** that sits at Jellyfin's `--ffmpeg=` path. For a recognised
video *transcode* it probes the source, evaluates a declarative rule file
(`rules.toml`), rewrites the hwaccel + filter-graph arguments to route through the
Raspberry Pi 4 hardware pipeline (rpivid HEVC decode → NEON SAND unpack →
bcm2835 ISP scale → `h264_v4l2m2m` encode), and performs **cross-session admission
control** so concurrent 4K sessions don't exhaust CMA. For everything else —
capability probes, thumbnails, trickplay, unrecognised commands, or any internal
error — it execs the real ffmpeg with the original arguments unchanged.

**The shim must never be the reason playback breaks.** Every failure path
degrades to a byte-identical passthrough.

## Why this exists

Three facts, established by measurement on a Pi 4, make a decision layer
*outside* a single ffmpeg process necessary:

- **Concurrency is unmanaged.** rpivid itself is not exclusive (two concurrent HW
  decodes run fine), but CMA is the real limit: a 4K hardware decode needs a large
  contiguous allocation, and how many fit depends on how much CMA *you* gave the
  board. Each ffmpeg decides in isolation, so nothing stops one more 4K session
  from starting and failing. A single process cannot arbitrate a global resource —
  an admission gate has to live above ffmpeg.
- **The RPi fork isn't in Jellyfin's playback path.** Jellyfin runs the distro
  `jellyfin-ffmpeg`; the 8.x fork carrying the RPi work
  ([poizan42/jellyfin-rpi-ffmpeg](https://github.com/poizan42/jellyfin-rpi-ffmpeg)
  — the SAND unpack, tone-map tiers and ISP bridge this shim's rules depend on)
  is not wired in by default. The shim is the reversible one-line hook that puts
  it there.
- **The graph Jellyfin builds doesn't use the Pi's pipeline.** What it emits
  depends on how you configure it, so take this as the tested case rather than a
  general claim: with *Hardware acceleration* = **V4L2**, hardware decoding enabled
  for H264, and **hardware encoding off**, Jellyfin builds a software decode +
  software `scale`/`tonemap` chain feeding `libx264`. Even with hardware encoding
  on (`h264_v4l2m2m`), the *filter* chain is still software `scale`/`tonemapx`:
  Jellyfin's filter-chain builder has no V4L2 arm, only QSV/VAAPI/NVENC/RKMPP
  ones — and the filters are where nearly all the CPU goes. Either way, swapping
  the binary alone
  would not exercise the hardware pipeline; something has to rewrite the command.
  The shim does that, matching only what it positively recognises and passing
  through anything else — including graph shapes from other configurations
  (`hwupload`/`hwmap`/`overlay`/deinterlace are all on the passthrough list).

## Layout

```
bin/ffmpeg     the shim (Python 3.11, stdlib only — tomllib is built in)
bin/ffprobe    passthrough sibling (Jellyfin derives the ffprobe path from the
               ffmpeg path by regex, MediaEncoder.cs:221, so this file MUST exist)
bin/_orch.py   shared helpers: config load, real-binary resolution, exec, logging
rules.toml     the policy: real-binary paths, hw_encoders, hw_slots, the rules
install.sh     copy shim + rules + fork binary to a Jellyfin-reachable prefix
check-capabilities.sh  deploy-time check that the fork is a superset of the stock
               build — decoders/encoders/filters/muxers/demuxers/bsfs/protocols
               (install.sh refuses to deploy on an unexplained gap)
tests/         test_rewrite.py (pure golden-rewrite unit tests, no hardware)
               live.sh        (end-to-end + concurrency + fail-safe, needs the Pi)
```

## What it does, step by step

1. **Cheap gate** (never probes for non-transcodes): engage only if the output
   video encoder is one of `hw_encoders` **or a software H.264 encoder we redirect**
   (`h264_sw_encoders`, e.g. `libx264` → `h264_v4l2m2m` — a software H.264 encode
   can't keep up in real time, so Jellyfin asking for `libx264` must still land on
   the hardware encoder), there's a `-vf` (not `-filter_complex`), a real file
   input, `scale` is present, and no disallowed filter is in the chain (`overlay`,
   `subtitles`, deinterlace, `hwupload/hwmap/hwdownload`, or our own filters — see
   `DISALLOWED`). Anything else → immediate passthrough. (No HW HEVC *encoder* on
   the Pi 4, so `libx265` is not redirected — it passes through.)
2. **Read the target box** from Jellyfin's `scale=` expression (the first two
   resolution-plausible integers are its `maxWidth,maxHeight`), and note whether it
   asked to tone-map (`tonemap` substring → `tm=accurate`, else `tm=none`).
3. **Probe the source once** with the fork's ffprobe: codec, dimensions, pix_fmt,
   bit depth, chroma, HDR transfer. Compute `hw_decodable` (hevc, 4:2:0, 8/10-bit,
   ≤4K) and the fitted output size (aspect-preserving, no upscale, width/64 × /2).
4. **Evaluate rules**, first match wins. A rule that needs a hardware slot is
   skipped if none is free, so the software rule is the guaranteed fallback.
5. **Rewrite argv**: swap the `-vf` graph, insert the rule's hwaccel args before
   the primary `-i`, and — if the input was a redirected software H.264 encoder —
   swap the encoder to `h264_v4l2m2m`, strip its libx264/libx265-only options
   (`-preset`, `-crf`, `-x264opts`, `-profile:v`, `-level`, `-sc_threshold`, …),
   and carry `-maxrate` across as `-b:v` (the HW encoder does average-bitrate rate
   control, not CRF). Everything else is preserved.
6. **exec** the real ffmpeg. On *any* exception anywhere above → passthrough.

### One binary: the fork, for everything
The shim execs **the fork** both for the rewritten hardware graph (which needs its
`sand_to_yuv420p_drm` / `scale_v4l2m2m` filters and `h264_v4l2m2m`) *and* for every
command it passes through untouched.

Passthrough deliberately does **not** go to the stock `jellyfin-ffmpeg`. The fork
is not only about the hardware path — it also carries **software-decode
optimisations** (the NEON/CABAC HEVC work, ~1.3× on 10-bit software decode), and
those pay off precisely on the commands the shim *can't* route onto the hardware:
the formats rpivid won't take, the `libx265` transcodes, the interlaced sources.
Sending those to the stock build would throw the optimisations away exactly where
they are most needed.

The price is that the fork must be a **superset** of the stock build — see the
next section. If yours isn't, `passthrough_ffmpeg` in `rules.toml` sends
unrewritten commands to the stock binary instead; that is an explicit, documented
choice, not the default.

### The fork must be a superset — checked at deploy time, not at runtime
Jellyfin builds every command line for the stock build, and the shim hands the
fork *all* of them (rewriting only the video side of the ones it recognises — the
audio cluster and everything else goes through verbatim). So anything the stock
build can do and the fork can't is a live failure waiting to happen. It happened
once: `-codec:a:0 libmp3lame` against the then-lean fork died with
`Encoder not found` → "Source error" in the client.

**[`check-capabilities.sh`](check-capabilities.sh)** diffs the fork against the
stock build across **decoders, encoders, filters, muxers, demuxers, bitstream
filters and protocols**, minus a short justified `EXPECTED_MISSING` list (other
vendors' hardware, nonfree, dead formats). `install.sh` runs it and **refuses to
deploy** on an unexplained gap (`SKIP_CAPABILITY_CHECK=1` overrides). Run it any
time:

```bash
./check-capabilities.sh
```

**A gotcha this check exists to catch:** jellyfin-ffmpeg ships much of what it
does as a **quilt series in `debian/patches/`, applied at Debian build time** —
not in its git tree. Build a fork of it straight from the tree and you silently
get *none* of it, including `tonemapx`, the filter Jellyfin puts in its software
HDR chains. The fork this shim targets applies that series in-tree for exactly
this reason. If you build your own, apply it too, or this check will tell you.

**The shim deliberately has no runtime capability check.** Detecting a gap at
transcode time and routing around it would convert a build defect into a quiet
regression — playback keeps working, slower, and nobody notices the fork is broken
until the box can't keep up. The failure should stay loud and land where it can be
fixed: in the fork build, or in these rules.

### Admission control (`flock`, survives exec)

`hw_slots` slot files in `slot_dir`; the shim takes the first with
`flock(LOCK_EX|LOCK_NB)`, then **clears `FD_CLOEXEC`** so the lock is inherited by
the exec'd ffmpeg and held for that process's whole life. The kernel releases it
automatically on exit or SIGKILL — no daemon, no stale state, no cleanup path to
get wrong.

**`hw_slots` is yours to tune** — it is the shim's model of *your* CMA budget, and
CMA size is a boot-time tradeoff against normal system RAM that only you can make
(`cma=` on the kernel command line). The shipped `hw_slots = 1` is what was
measured at `cma=512M` on a Pi 4: one 4K HEVC session fits, two exhaust it. Give
the board more CMA and you can raise it; give it less and the hardware rules
simply stop firing, leaving every session on the software-decode fallback —
slower, but never a CMA failure. There is no autodetection: `CmaFree` is a
famously misleading number (it counts pages that are movable-but-not-free), so
the shim asks you for a slot count instead of guessing one.

## The rule file (`rules.toml`)

`when` is a Python expression evaluated with **no builtins** against a fixed fact
namespace; `vf` is a `str.format` template over the same facts. First match wins.

Facts available to `when` / `vf`:

| fact | meaning |
|---|---|
| `src_codec` `src_w` `src_h` `src_pix_fmt` `src_bit_depth` `src_chroma` `src_hdr` | probed source |
| `out_w` `out_h` | fitted output size (aspect-preserving, /64 × /2) |
| `hw_decodable` | rpivid can decode it (hevc, 4:2:0, 8/10-bit, ≤4K) |
| `tm` | `"accurate"` if Jellyfin asked to tone-map, else `"none"` |

`true`/`false`/`null` are accepted as aliases of Python's `True`/`False`/`None`, so
TOML authors can write lowercase.

### Adding / editing a rule

Append a `[[rule]]` table (order matters — first match wins):

```toml
[[rule]]
name    = "my rule"                         # shown in the [rpi-orch] engaged log
when    = "hw_decodable and src_w >= 2 * out_w"
hw_slot = true                              # optional: take an admission slot
args    = ["-hwaccel", "drm", "-hwaccel_output_format", "drm_prime"]
vf      = "sand_to_yuv420p_drm=tm={tm}:out=half,scale_v4l2m2m={out_w}:{out_h}"
```

Gotcha worth repeating: within one filter, options are **`:`-separated**
(`tm={tm}:out=half`); a **`,`** starts the *next* filter. Getting this wrong
produces `Error parsing filterchain … around: ,…` at runtime — the golden test
now pins the exact string to catch it.

The shipped rules, in order (`hw_decodable` = HEVC, 4:2:0, 8/10-bit, ≤4K via rpivid):

1. `hw_decodable and src_w==2*out_w and src_h==2*out_h` → HEVC HW decode, `out=half`
   with **no** ISP scale (exact 2:1, e.g. 4K→1080p; the ISP pass would be identity).
2. `hw_decodable and src_w >= 2*out_w` → HEVC HW decode + NEON fused half-downscale
   (`out=half`) + ISP to target (e.g. 4K→720p: NEON halves to 1080p, ISP does 720p).
3. `hw_decodable` → HEVC HW decode + ISP to target.
4. `h264_hw_decodable` (h264, 8-bit, 4:2:0, ≤1080p, progressive, non-HDR) → the
   **bcm2835 H.264 decoder** (`/dev/video10`) via `-no_cvt_hw -c:v h264_v4l2m2m`,
   emitting DRM_PRIME **straight into `scale_v4l2m2m`** — fully hardware, zero-copy,
   no sand bridge, no tone-map. ~0.2–0.4 cores (vs ~2.9 software).
5. `true` → software decode (no `-hwaccel`), but scale + encode stay on hardware —
   the adaptive `sand_to_yuv420p_drm` filter accepts a software `yuv420p` frame and
   re-emits DRM_PRIME for the ISP. Reached for 4K/10-bit/non-4:2:0/interlaced H.264,
   non-HEVC-non-H.264 sources, or when no HEVC HW slot is free.

## Deploying it (manual, reversible — not done by this repo)

**Reachability first.** The Jellyfin service runs as user `jellyfin`, which
**cannot** run anything inside a home directory left at the usual mode `0700` —
not being the owner or in the group, it can't even *traverse* into it. That
blocks not just the shim but the fork `ffmpeg`/`ffprobe` it execs, if those live
under the same home. (`ProtectHome=no` on the unit, so systemd isn't the blocker
— plain Unix permissions are.) Pointing `--ffmpeg=` at a path under `$HOME` fails
at launch. So the shim and the binaries it calls must be installed somewhere
`jellyfin` can reach.

**Install to a root-owned prefix** (the recommended path — no change to your home
directory's permissions):

```sh
sudo ./install.sh                       # -> /opt/rpi-ffmpeg-orchestrator
sudo ./install.sh /usr/local/lib/orch   # ...or any prefix you prefer
```

It expects the fork build in a sibling checkout (`../ffmpeg-jellyfin`); set
`FORK_DIR=/path/to/jellyfin-rpi-ffmpeg` if it lives elsewhere.

`install.sh` copies the shim and the fork's **`ffmpeg` and `ffprobe`** (a static
libav build, so they are self-contained — only system `.so` deps) to
`/opt/rpi-ffmpeg-orchestrator/`, makes them world-traversable, and writes an
installed `rules.toml` pointing at the copies. A symlink to the fork would *not*
work — its target stays behind the same permission wall — so the binaries are
copied; **re-run `install.sh` after rebuilding the fork** to refresh them.

Then edit `/etc/default/jellyfin` and point `JELLYFIN_FFMPEG_OPT` at the installed
shim:

```sh
# was: --ffmpeg=/usr/lib/jellyfin-ffmpeg/ffmpeg
JELLYFIN_FFMPEG_OPT="--ffmpeg=/opt/rpi-ffmpeg-orchestrator/bin/ffmpeg"
```

then `sudo systemctl restart jellyfin`. To revert, restore the original line and
restart. No server rebuild either way.

> **Restart Jellyfin whenever the fork's capabilities change** — after every
> `install.sh`, not just the first. Jellyfin probes ffmpeg's encoders and filters
> **once at startup** and builds every later command line from that cached answer.
> Swap in a binary with a different feature set under a running server and it will
> keep asking for what the *old* one had, and those transcodes die with
> `Unknown encoder`. (This is also why `passthrough_ffmpeg` deserves care: point it
> at the stock build and Jellyfin probes *that* while engaged commands run on the
> fork, so the two can disagree by construction.)

**Jellyfin settings this was tested with** (Dashboard → Playback → Transcoding):
*Hardware acceleration* = **Video4Linux2 (V4L2)**, *hardware decoding* enabled for
**H264**, *hardware encoding* **off** (so Jellyfin asks for `libx264` and the shim
redirects it to `h264_v4l2m2m`). Turning hardware encoding on also works — the
shim then keeps Jellyfin's `h264_v4l2m2m` and only rewrites the graph. Other
combinations aren't tested; the shim's response to an unrecognised command shape
is to pass it through, so the failure mode is "no speed-up", not "broken
playback".

*Alternatives, if you'd rather keep the checkout as the single live source (no
copy to refresh):* open traversal on the home directory holding it — either
`chmod 0711 "$HOME"` (any user can traverse, not list) or
`sudo usermod -aG "$USER" jellyfin && chmod 0750 "$HOME"` (traversal for your
group only, but grants `jellyfin` group-read across your home). Both trade a bit
of home-directory privacy for skipping the copy step; the `/opt` install avoids
that trade at the cost of re-copying on rebuild.

A different `rules.toml` can be forced with the `RPI_ORCH_RULES` env var; otherwise
the shim looks next to itself (`../rules.toml`) then
`/etc/rpi-ffmpeg-orchestrator/rules.toml`.

## Testing

```sh
# Pure, fast, no hardware — asserts exact rewritten argv for representative
# Jellyfin command shapes (gates + each rule):
python3 tests/test_rewrite.py

# End-to-end on the Pi: single session, 3x concurrent 4K under hw_slots=1,
# fail-safe passthrough, slot-release-on-kill. Point it at a 4K HEVC sample:
SAMPLES=/path/to/samples SAMPLE=some-4k-hevc.mkv bash tests/live.sh
```

`live.sh` deliberately SIGKILLs a running hardware encode (to prove the slot is
released). On some RPi kernels that wedges the bcm2835 encoder until reboot —
don't run it on a box you need to stay up.

Validated on the Pi 4: single 4K HDR→720p session engages the half-downscale rule
and completes; 3 concurrent 4K sessions with `hw_slots=1` run exactly one on HW and
two on the software rule, **all completing with zero CMA failures** (the payoff vs.
the unmanaged case); malformed/missing config falls through to byte-identical
passthrough; and a SIGKILL'd HW session frees its slot for the next one immediately.

## v1 tradeoffs (called out deliberately)

- **`when` is `eval()`'d** (with no builtins) against a fixed fact set. This is a
  local, trusted, root-owned config; it deliberately avoids inventing a DSL for v1.
  If the file ever stops being trusted, replace the evaluator before shipping.
- **It rewrites an already-built graph** rather than building the right one. It can
  *degrade* (swap in the software rule when no slot is free) because it rewrites
  before exec, but it cannot touch decisions Jellyfin baked in upstream of `-vf`
  (bitrate, muxing, segment layout). Jellyfin has **two** builders to track — the
  progressive path (`EncodingHelper`) and the HLS path (`DynamicHlsController`, the
  common case) — and the parser stays strictly defensive: match only what it
  positively recognises, pass everything else through untouched.
- **No mid-stream re-decide.** One probe + one decision at launch. A source that
  changes resolution/format mid-stream is handled *inside* ffmpeg (the adaptive
  filters), not re-decided here.

## Later iterations (deferred, documented for continuity)

- Move the rule evaluator **in-process** as a rule-evaluated `graph_desc`, so a
  mid-stream change re-decides without a relaunch.
- Migrate the policy **into the Jellyfin fork**: a `v4l2m2m` arm in
  `EncodingHelper.cs`'s filter switch + a hwaccel branch, with admission gated on
  `_activeTranscodingJobs` *before* arg construction (the one thing a wrapper can't
  do as cleanly). Cost: a .NET ARM64 build and maintaining a fork against churn.

The v1 bet: proving the *rules* is the valuable part, and the shim proves them with
a one-line reversible hook instead of a server rebuild.

## Requirements

- Raspberry Pi 4 (BCM2711) running a 64-bit RPi OS — `bcm2835-codec` (`/dev/video10`
  H.264 decode, `/dev/video11` H.264 encode, `/dev/video12` ISP) and rpivid HEVC
  decode (`/dev/video19`).
- A build of [poizan42/jellyfin-rpi-ffmpeg](https://github.com/poizan42/jellyfin-rpi-ffmpeg)
  (branch `jellyfin-rpi`) — it provides `sand_to_yuv420p_drm`, `scale_v4l2m2m` and
  the tone-map tiers the rules reference. Build it **full-featured** (see that
  repo's README → "Building"); `check-capabilities.sh` enforces it.
- The stock `jellyfin-ffmpeg` package. The shim doesn't run it — it is the
  reference the capability check diffs against, and what you fall back to by
  restoring one line in `/etc/default/jellyfin`.
- Python 3.11+ (`tomllib`); no third-party modules.
- Enough CMA for what you want to run in hardware. 4K HEVC decode needs a big
  contiguous pool; the numbers here were measured at `cma=512M` on a Pi 4, but the
  size is a tradeoff against normal system RAM and is yours to choose — set it on
  the kernel command line and match `hw_slots` in `rules.toml` to it.

## Provenance

Extracted from a private research repo where it was developed alongside the ffmpeg
fork, so history starts at the extraction. Every number quoted here
(1×4K-fits/2×4K-exhausts, the per-rule CPU costs, the ~6% exact-half win) was
measured on a Pi 4B, not estimated.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Kasper Fabæch Brandt.

(The ffmpeg fork it drives is a separate project under FFmpeg's own licence; this
repo contains no FFmpeg code.)
