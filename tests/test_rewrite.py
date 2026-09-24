#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Kasper Fabæch Brandt
"""Pure golden-rewrite tests for the orchestrator shim. No hardware, no exec.

Run: python3 tests/test_rewrite.py   (from the transcode-orchestrator dir)
"""
import os
import sys
import unittest
from fractions import Fraction
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bin"))
shim = SourceFileLoader("shim", os.path.join(ROOT, "bin", "ffmpeg")).load_module()

import tomllib  # noqa: E402
with open(os.path.join(ROOT, "rules.toml"), "rb") as fh:
    CFG = tomllib.load(fh)

# The configured HDR tone-map tier — tests assert against this rather than a
# hard-coded tier, so changing tm_hdr in rules.toml doesn't break them.
TM = CFG.get("tm_hdr", "accurate")


def stub_probe(**kw):
    base = {"codec": "hevc", "w": 3840, "h": 2160, "pix_fmt": "yuv420p10le",
            "bit_depth": 10, "chroma": "420", "hdr": True, "dv": None,
            "field_order": "progressive"}
    base.update(kw)
    return base


def jf_vf(max_w, max_h, tonemap=False):
    """A Jellyfin-shaped v4l2 software scale filter (the max-dimensions form)."""
    s = (r"scale=trunc(min(max(iw\,ih*a)\,min(%d\,%d*a))/64)*64:"
         r"trunc(min(max(iw/a\,ih)\,min(%d/a\,%d))/2)*2" % (max_w, max_h, max_w, max_h))
    if tonemap:
        s = "tonemapx=tonemap=hable:desat=0," + s
    return s + ",format=yuv420p"


def jf_cmd(vf, codec="h264_v4l2m2m", inp="/movies/x.mkv"):
    return ["-analyzeduration", "200M", "-probesize", "1G", "-i", inp,
            "-map_metadata", "-1", "-map_chapters", "-1", "-threads", "0",
            "-codec:v:0", codec, "-b:v", "3M", "-maxrate", "3M",
            "-vf", vf, "-y", "/cache/out.mp4"]


def jf_x264_cmd(max_w=1280, inp="/movies/x.mkv"):
    """Jellyfin's real software-H.264 HLS shape: libx264 with the width-only
    scale form and the full x264 option cluster (as captured from a live log)."""
    vf = (r"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
          r"scale=trunc(min(max(iw\,ih*a)\,%d)/2)*2:trunc(ow/a/2)*2,"
          r"format=yuv420p" % max_w)
    return ["-analyzeduration", "200M", "-probesize", "1G", "-i", inp,
            "-map_metadata", "-1", "-map_chapters", "-1", "-threads", "0",
            "-map", "0:0", "-map", "0:1",
            "-codec:v:0", "libx264", "-preset", "veryfast", "-crf", "23",
            "-maxrate", "1116000", "-bufsize", "2232000",
            "-profile:v:0", "high", "-level", "51",
            "-x264opts:0", "subme=0:me_range=16:me=hex:open_gop=0",
            "-force_key_frames:0", "expr:gte(t,n_forced*3)",
            "-sc_threshold:v:0", "0",
            "-vf", vf, "-codec:a:0", "aac", "-ac", "2",
            "-f", "hls", "-y", "/cache/out.m3u8"]


def run(argv, probe_ret, slot=True, kept=1.0, cfg=None):
    """Invoke decide() with probe, the noref measurement and acquire stubbed."""
    orig = shim.probe, shim.noref_kept
    shim.probe = lambda *_a, **_k: probe_ret
    shim.noref_kept = lambda *_a, **_k: kept
    try:
        return shim.decide(argv, cfg or CFG, "ffprobe", lambda: slot)
    finally:
        shim.probe, shim.noref_kept = orig


class TestGate(unittest.TestCase):
    def test_non_redirected_encoder_passthrough(self):
        # libx265 (no HW HEVC encoder on the Pi) and stream-copy must pass through.
        for codec in ("libx265", "copy", "libvpx-vp9"):
            self.assertIsNone(
                run(jf_cmd(jf_vf(1280, 720), codec=codec), stub_probe()), codec)

    def test_filter_complex_passthrough(self):
        argv = jf_cmd(jf_vf(1280, 720)) + ["-filter_complex", "[0:v][0:s]overlay"]
        self.assertIsNone(run(argv, stub_probe()))

    def test_no_vf_passthrough(self):
        argv = ["-i", "/x.mkv", "-codec:v:0", "h264_v4l2m2m", "-y", "/o.mp4"]
        self.assertIsNone(run(argv, stub_probe()))

    def test_deinterlace_in_vf_passthrough(self):
        argv = jf_cmd("yadif=0:-1:0," + jf_vf(1280, 720))
        self.assertIsNone(run(argv, stub_probe()))

    def test_capability_probe_passthrough(self):
        for argv in (["-version"], ["-encoders"], ["-hwaccels"],
                     ["-f", "lavfi", "-i", "nullsrc", "-f", "null", "-"]):
            self.assertIsNone(run(argv, stub_probe()), argv)

    def test_no_numeric_box_passthrough(self):
        # scale with no resolution-plausible integer -> no box -> passthrough
        argv = jf_cmd("scale=iw/2:ih/2,format=yuv420p")
        self.assertIsNone(run(argv, stub_probe()))


class TestRules(unittest.TestCase):
    def _graph(self, res):
        self.assertIsNotNone(res)
        new_argv, _ = res
        return new_argv[new_argv.index("-vf") + 1], new_argv

    def test_4k_hdr_large_downscale_uses_half(self):
        argv = jf_cmd(jf_vf(1280, 720, tonemap=True))
        graph, new = self._graph(run(argv, stub_probe(), slot=True))
        self.assertEqual(
            graph,
            "sand_to_yuv420p_drm=tm=%s:out=half,scale_v4l2m2m=1280:720" % TM)
        # hwaccel inserted immediately before -i
        i = new.index("-i")
        self.assertEqual(new[i - 4:i], ["-hwaccel", "drm",
                                        "-hwaccel_output_format", "drm_prime"])

    def test_1080p_plain_hw_rule(self):
        argv = jf_cmd(jf_vf(1280, 720))  # no tonemap
        graph, _ = self._graph(
            run(argv, stub_probe(w=1920, h=1080, bit_depth=8,
                                 pix_fmt="yuv420p", hdr=False)))
        self.assertEqual(graph, "sand_to_yuv420p_drm=tm=none,scale_v4l2m2m=1280:720")

    def test_444_source_software_rule(self):
        argv = jf_cmd(jf_vf(1280, 720))
        graph, new = self._graph(
            run(argv, stub_probe(w=1920, h=1080, chroma="444",
                                 pix_fmt="yuv444p", bit_depth=8, hdr=False)))
        self.assertEqual(graph, "sand_to_yuv420p_drm=tm=none,scale_v4l2m2m=1280:720")
        self.assertNotIn("-hwaccel", new)  # software rule adds no hwaccel

    def test_admission_fallthrough_to_software(self):
        # hw-decodable, but no slot free -> both hw rules skip -> software rule,
        # which must NOT carry hwaccel args.
        argv = jf_cmd(jf_vf(1280, 720))
        res = run(argv, stub_probe(), slot=False)
        self.assertIsNotNone(res)
        new_argv, _ = res
        self.assertNotIn("-hwaccel", new_argv)
        # tm follows the SOURCE (stub is HDR) -> tm_hdr tier, even though
        # Jellyfin's vf carries no tonemap filter.
        self.assertEqual(new_argv[new_argv.index("-vf") + 1],
                         "sand_to_yuv420p_drm=tm=%s,scale_v4l2m2m=1280:720" % TM)

    def test_everything_else_preserved(self):
        argv = jf_cmd(jf_vf(1280, 720))
        _, new = self._graph(run(argv, stub_probe()))
        for tok in ("-analyzeduration", "200M", "-b:v", "3M", "-maxrate",
                    "-map_metadata", "/cache/out.mp4", "-codec:v:0",
                    "h264_v4l2m2m"):
            self.assertIn(tok, new)

    def test_libx264_redirected_to_hw_encoder(self):
        # Jellyfin's real software-H.264 command (width-only scale, x264 opts) must
        # engage, redirect the encoder to h264_v4l2m2m, strip x264-only options,
        # and carry -maxrate over as -b:v. 1080p -> plain "hw decode" rule.
        _, new = self._graph(
            run(jf_x264_cmd(1280),
                stub_probe(w=1920, h=1080, bit_depth=8,
                           pix_fmt="yuv420p", hdr=False)))
        self.assertEqual(new[new.index("-vf") + 1],
                         "sand_to_yuv420p_drm=tm=none,scale_v4l2m2m=1280:720")
        self.assertEqual(new[new.index("-codec:v:0") + 1], "h264_v4l2m2m")
        self.assertIn("-b:v", new)
        self.assertEqual(new[new.index("-b:v") + 1], "1116000")   # from -maxrate
        for gone in ("-preset", "-crf", "-x264opts:0", "-profile:v:0",
                     "-level", "-sc_threshold:v:0", "libx264"):
            self.assertNotIn(gone, new)
        # non-encoder options survive
        for keep in ("-map", "0:1", "-codec:a:0", "aac", "-f", "hls",
                     "-force_key_frames:0"):
            self.assertIn(keep, new)

    def test_width_only_box_fits_by_aspect(self):
        # width-only scale (single integer) -> height follows source aspect
        _, new = self._graph(
            run(jf_x264_cmd(1280),
                stub_probe(w=1920, h=1080, bit_depth=8,
                           pix_fmt="yuv420p", hdr=False)))
        self.assertIn("scale_v4l2m2m=1280:720", new[new.index("-vf") + 1])

    def test_4k_libx264_large_downscale_half_and_hw_encoder(self):
        # 4K HDR + libx264 + big downscale (720p): half-rule AND encoder redirect
        # together, and HDR source -> tm_hdr tier despite no tonemap in the vf.
        _, new = self._graph(run(jf_x264_cmd(1280), stub_probe(), slot=True))
        self.assertEqual(
            new[new.index("-vf") + 1],
            "sand_to_yuv420p_drm=tm=%s:out=half,scale_v4l2m2m=1280:720" % TM)
        self.assertEqual(new[new.index("-codec:v:0") + 1], "h264_v4l2m2m")
        self.assertIn("-hwaccel", new)

    def test_4k_to_1080p_exact_half_skips_isp_scale(self):
        # 4K -> 1080p: out=half already hits the target, so no scale_v4l2m2m
        # (redundant identity ISP pass). libx264 still redirected to HW.
        _, new = self._graph(run(jf_x264_cmd(1920), stub_probe(), slot=True))
        graph = new[new.index("-vf") + 1]
        self.assertEqual(graph, "sand_to_yuv420p_drm=tm=%s:out=half" % TM)
        self.assertNotIn("scale_v4l2m2m", graph)
        self.assertEqual(new[new.index("-codec:v:0") + 1], "h264_v4l2m2m")


class TestHdrDetection(unittest.TestCase):
    """_is_hdr must catch Dolby Vision, whose base stream carries no PQ transfer
    tag (color_transfer is null — the PQ is in the RPU)."""

    def _dovi(self, compat):
        return {"color_transfer": None, "side_data_list": [
            {"side_data_type": "DOVI configuration record",
             "dv_profile": 5 if compat == 0 else 8,
             "dv_bl_signal_compatibility_id": compat}]}

    def test_hdr10_transfer(self):
        self.assertTrue(shim._is_hdr({"color_transfer": "smpte2084"}))

    def test_hlg_transfer(self):
        self.assertTrue(shim._is_hdr({"color_transfer": "arib-std-b67"}))

    def test_sdr(self):
        self.assertFalse(shim._is_hdr({"color_transfer": "bt709"}))
        self.assertFalse(shim._is_hdr({}))

    def test_dv_p5_no_transfer_tag_is_hdr(self):
        # The real Agatha S01E05 case: color_transfer null, DOVI P5, compat 0.
        st = self._dovi(0)
        self.assertTrue(shim._is_hdr(st))
        self.assertEqual(shim._dv_profile(st), 5)

    def test_dv_hdr10_compatible_is_hdr(self):
        self.assertTrue(shim._is_hdr(self._dovi(1)))   # P8.1 HDR10 base

    def test_dv_sdr_compatible_is_not_hdr(self):
        self.assertFalse(shim._is_hdr(self._dovi(2)))  # P8.2 SDR base -> no tonemap

    def test_dv_hlg_compatible_is_hdr(self):
        self.assertTrue(shim._is_hdr(self._dovi(4)))   # P8.4 HLG base


class TestH264HwDecode(unittest.TestCase):
    """<=1080p 8-bit H.264 routes to the zero-copy hardware decoder
    (-no_cvt_hw -c:v h264_v4l2m2m -> scale_v4l2m2m -> h264_v4l2m2m), no sand
    bridge. Anything the /dev/video10 decoder can't take falls to software."""

    def _h264(self, **kw):
        base = dict(codec="h264", w=1920, h=1080, pix_fmt="yuv420p",
                    bit_depth=8, chroma="420", hdr=False, dv=None,
                    field_order="progressive")
        base.update(kw)
        return stub_probe(**base)

    def test_1080p_h264_zero_copy_hw_decode(self):
        argv = jf_cmd(jf_vf(1280, 720), codec="libx264")
        res = run(argv, self._h264())
        self.assertIsNotNone(res)
        new, _ = res
        # HW decoder selected before -i, with -no_cvt_hw (the DRM_PRIME trigger)
        i = new.index("-i")
        self.assertIn("-no_cvt_hw", new[:i])
        self.assertEqual(new[i - 2:i], ["-c:v", "h264_v4l2m2m"])
        # graph is just the ISP scale — no sand bridge, no tone-map
        self.assertEqual(new[new.index("-vf") + 1], "scale_v4l2m2m=1280:720")
        self.assertNotIn("sand_to_yuv420p_drm", new[new.index("-vf") + 1])
        # output encoder redirected to the HW encoder
        self.assertEqual(new[new.index("-codec:v:0") + 1], "h264_v4l2m2m")
        # no rpivid/HEVC hwaccel on the H.264 path
        self.assertNotIn("-hwaccel", new)

    def _falls_to_software(self, probe):
        new, _ = run(jf_cmd(jf_vf(1280, 720), codec="libx264"), probe)
        vf = new[new.index("-vf") + 1]
        self.assertTrue(vf.startswith("sand_to_yuv420p_drm="), vf)  # software rule
        self.assertNotIn("-no_cvt_hw", new)
        self.assertNotIn("-c:v", new[:new.index("-i")])  # no HW decoder selected

    def test_4k_h264_falls_to_software(self):
        self._falls_to_software(self._h264(w=3840, h=2160))

    def test_hi10p_h264_falls_to_software(self):
        self._falls_to_software(self._h264(bit_depth=10, pix_fmt="yuv420p10le"))

    def test_422_h264_falls_to_software(self):
        self._falls_to_software(self._h264(chroma="422", pix_fmt="yuv422p"))

    def test_interlaced_h264_falls_to_software(self):
        self._falls_to_software(self._h264(field_order="tt"))


class TestUnknownEncoderIsNotAbsorbed(unittest.TestCase):
    """The shim must not quietly swallow a fork build defect. If the fork can't
    provide an encoder the command names (this bit us with libmp3lame), the
    transcode should fail loudly so the fork/rules get fixed — the shim has no
    capability check, and must not grow one. Deploy-time detection
    lives in check-capabilities.sh, which install.sh runs and refuses to deploy
    on."""

    def test_audio_encoder_is_passed_through_untouched(self):
        argv = jf_x264_cmd()
        argv[argv.index("-codec:a:0") + 1] = "libmp3lame"
        new, _ = run(argv, stub_probe())
        self.assertEqual(new[new.index("-codec:a:0") + 1], "libmp3lame")

    def test_engages_regardless_of_audio_encoder(self):
        for acodec in ("aac", "libmp3lame", "libopus", "flac", "copy"):
            argv = jf_x264_cmd()
            argv[argv.index("-codec:a:0") + 1] = acodec
            self.assertIsNotNone(run(argv, stub_probe()), acodec)


def jf_burnin_cmd(max_w=1920, max_h=1080, sub="subtitles=f='/subs/0c/2.ass':fontsdir='/att/0c'",
                  codec="libx264"):
    """Jellyfin's text-subtitle burn-in shape, as captured from a live log:
    the subtitles filter is the last filter of the software chain."""
    vf = (r"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
          r"scale=trunc(min(max(iw\,ih*a)\,min(%d\,%d*a))/2)*2:"
          r"trunc(min(max(iw/a\,ih)\,min(%d/a\,%d))/2)*2,format=yuv420p,%s"
          % (max_w, max_h, max_w, max_h, sub))
    return ["-analyzeduration", "200M", "-probesize", "1G", "-f", "matroska",
            "-i", "file:/anime/x.mkv", "-map_metadata", "-1", "-threads", "0",
            "-map", "0:0", "-map", "0:1", "-codec:v:0", codec,
            "-preset", "veryfast", "-crf", "23", "-maxrate", "7364903",
            "-bufsize", "14729806", "-vf", vf, "-codec:a:0", "copy",
            "-f", "hls", "-y", "/cache/out.m3u8"]


def sdr1080(**kw):
    base = dict(w=1920, h=1080, bit_depth=10, pix_fmt="yuv420p10le", hdr=False)
    base.update(kw)
    return stub_probe(**base)


class Logged:
    """Capture the shim's log lines for the duration of a with-block."""
    def __enter__(self):
        self.lines, self._orig = [], shim.log
        shim.log = self.lines.append
        return self

    def __exit__(self, *exc):
        shim.log = self._orig


class TestSubtitleBurnIn(unittest.TestCase):
    SUB = "subtitles=f='/subs/0c/2.ass':fontsdir='/att/0c'"

    def _vf(self, res):
        self.assertIsNotNone(res)
        new, decision = res
        return new[new.index("-vf") + 1], new, decision

    def test_production_case_1080p_sdr_same_size(self):
        # The session that exposed this: 1080p 10-bit SDR HEVC, 1080p target,
        # ASS burn-in, libx264 requested. Used to pass through to an all-software
        # transcode at 0.83x.
        vf, new, decision = self._vf(run(jf_burnin_cmd(), sdr1080()))
        self.assertEqual(vf, self.SUB)                         # NEON-download rule
        i = new.index("-i")
        self.assertEqual(new[i - 4:i], ["-hwaccel", "drm",
                                        "-hwaccel_output_format", "yuv420p"])
        self.assertEqual(new[new.index("-codec:v:0") + 1], "h264_v4l2m2m")
        self.assertIn("subs=burn-in", decision)

    def test_hdr_same_size_keeps_the_tonemap(self):
        vf, _, _ = self._vf(run(jf_burnin_cmd(), sdr1080(hdr=True)))
        self.assertEqual(vf, "sand_to_yuv420p_drm=tm=%s,hwdownload,format=yuv420p,%s"
                         % (TM, self.SUB))

    def test_4k_hdr_downscale_burns_in_after_the_isp(self):
        vf, _, _ = self._vf(run(jf_burnin_cmd(1280, 720), stub_probe()))
        self.assertEqual(vf, "sand_to_yuv420p_drm=tm=%s:out=half,"
                         "scale_v4l2m2m=1280:720,hwdownload,format=yuv420p,%s"
                         % (TM, self.SUB))

    def test_tail_is_verbatim_commas_in_path_and_setpts(self):
        sub = r"subtitles=f='/subs/a\,b, c/2.ass':charenc=UTF-8:fontsdir='/f',setpts=PTS -12/TB"
        vf, _, _ = self._vf(run(jf_burnin_cmd(1280, 720, sub=sub), sdr1080()))
        self.assertTrue(vf.endswith(",hwdownload,format=yuv420p," + sub), vf)

    def test_h264_source_burns_in_after_the_isp(self):
        probe = stub_probe(codec="h264", w=1920, h=1080, pix_fmt="yuv420p",
                           bit_depth=8, hdr=False)
        vf, new, _ = self._vf(run(jf_burnin_cmd(1280, 720), probe))
        self.assertEqual(vf, "scale_v4l2m2m=1280:720,hwdownload,format=yuv420p,"
                         + self.SUB)
        self.assertIn("-no_cvt_hw", new)

    def test_no_subs_same_size_skips_the_isp(self):
        vf, _, _ = self._vf(run(jf_cmd(jf_vf(1920, 1080)),
                                sdr1080(bit_depth=8, pix_fmt="yuv420p")))
        self.assertEqual(vf, "sand_to_yuv420p_drm=tm=none")


class TestEncoderOnlyRedirect(unittest.TestCase):
    """Graphs the hardware graph can't take keep Jellyfin's software filters,
    but a software H.264 encode still moves to the hardware encoder."""

    def test_graphical_subtitle_overlay(self):
        fc = ("[0:3]scale=1920:1080[sub];[0:0]setparams=colorspace=bt709,"
              "scale=1920:1080,format=yuv420p[main];[main][sub]overlay=eof_action=pass")
        argv = ["-i", "/x.mkv", "-filter_complex", fc, "-codec:v:0", "libx264",
                "-preset", "veryfast", "-crf", "23", "-maxrate", "8M", "-y", "/o.ts"]
        res = run(argv, sdr1080())
        self.assertIsNotNone(res)
        new, decision = res
        self.assertEqual(new[new.index("-filter_complex") + 1], fc)   # untouched
        self.assertEqual(new[new.index("-codec:v:0") + 1], "h264_v4l2m2m")
        self.assertEqual(new[new.index("-b:v") + 1], "8M")
        self.assertNotIn("-crf", new)
        self.assertNotIn("-hwaccel", new)
        self.assertIn("encoder-only", decision)

    def test_deinterlacer(self):
        vf = "yadif=0:-1:0," + jf_vf(1280, 720)
        new, _ = run(jf_cmd(vf, codec="libx264"), sdr1080())
        self.assertEqual(new[new.index("-vf") + 1], vf)
        self.assertEqual(new[new.index("-codec:v:0") + 1], "h264_v4l2m2m")

    def test_output_over_the_encoder_limit_passes_through(self):
        argv = ["-i", "/x.mkv", "-filter_complex", "[0:0][0:3]overlay",
                "-codec:v:0", "libx264", "-y", "/o.ts"]
        with Logged() as lg:
            self.assertIsNone(run(argv, stub_probe(w=3840, h=2160)))
        self.assertTrue(any("exceeds" in l for l in lg.lines), lg.lines)

    def test_foreign_hardware_graph_is_left_alone(self):
        vf = "hwupload=derive_device=vaapi," + jf_vf(1280, 720)
        with Logged() as lg:
            self.assertIsNone(run(jf_cmd(vf, codec="libx264"), sdr1080()))
        self.assertTrue(any("passthrough:" in l and "hwupload" in l
                            for l in lg.lines), lg.lines)


class TestEveryPassthroughSaysWhy(unittest.TestCase):
    def test_transcode_passthroughs_are_logged(self):
        cases = [
            jf_cmd(jf_vf(1280, 720)) + ["-filter_complex", "[0:v][0:s]overlay"],
            ["-i", "/x.mkv", "-codec:v:0", "h264_v4l2m2m", "-y", "/o.mp4"],
            jf_cmd("yadif=0:-1:0," + jf_vf(1280, 720)),
        ]
        for argv in cases:
            with Logged() as lg:
                self.assertIsNone(run(argv, stub_probe()))
            self.assertTrue(any(l.startswith("passthrough: ") for l in lg.lines),
                            (argv, lg.lines))

    def test_non_transcodes_stay_silent(self):
        # Jellyfin parses the output of its capability probes; keep them clean.
        for argv in (["-version"], ["-encoders"], ["-hwaccels"],
                     ["-f", "lavfi", "-i", "nullsrc", "-c:v", "libx264", "-f", "null", "-"],
                     jf_cmd(jf_vf(1280, 720), codec="libx265")):
            with Logged() as lg:
                self.assertIsNone(run(argv, stub_probe()))
            self.assertEqual(lg.lines, [], argv)


class TestNorefMeasurement(unittest.TestCase):
    """noref_kept() = packets surviving the noref filter_units / all packets."""

    def test_ratio_of_surviving_packets(self):
        def fake(cmd, **_k):
            n = 41 if any("filter_units" in c for c in cmd) else 121
            out = "#tb 0: 1/90000\n" + "0, 1, 1, 1, 9, 0x0\n" * n
            return type("CP", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        orig = shim.subprocess.run
        shim.subprocess.run = fake
        try:
            self.assertAlmostEqual(shim.noref_kept("ffmpeg", "/x.mkv"), 41 / 121)
        finally:
            shim.subprocess.run = orig

    def test_failure_is_none_and_logged(self):
        def fake(cmd, **_k):
            return type("CP", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        orig = shim.subprocess.run
        shim.subprocess.run = fake
        try:
            with Logged() as lg:
                self.assertIsNone(shim.noref_kept("ffmpeg", "/x.mkv"))
        finally:
            shim.subprocess.run = orig
        self.assertTrue(any("noref measurement failed" in l for l in lg.lines))


class TestHighFrameRate(unittest.TestCase):
    """4K above 30 fps is decode+unpack bound (0.54-0.59x measured), so the
    hardware-decode path halves it: skip non-reference frames, regularise with
    fps= before the unpack. 1080p50/60 is NOT halved (the fork's encoder raises
    its H.264 level for it and it runs in real time)."""

    def _run(self, argv=None, kept=1.0, **probe):
        argv = argv or jf_cmd(jf_vf(1920, 1080), codec="libx264")
        with Logged() as lg:
            res = run(argv, stub_probe(**probe), kept=kept)
        self.assertIsNotNone(res)
        new, decision = res
        return new, new[new.index("-vf") + 1], decision, lg.lines

    def test_4k60_halved_with_noref_skip(self):
        new, vf, decision, _ = self._run(fps=Fraction(60))
        self.assertTrue(vf.startswith("fps=30,sand_to_yuv420p_drm="), vf)
        i = new.index("-i")
        self.assertEqual(new[i - 6:i], ["-skip_frame", "noref", "-hwaccel", "drm",
                                        "-hwaccel_output_format", "drm_prime"])
        self.assertIn("hfr=60->30 fps", decision)

    def test_4k_5994_keeps_the_ntsc_rational(self):
        _, vf, decision, _ = self._run(fps=Fraction(60000, 1001))
        self.assertTrue(vf.startswith("fps=30000/1001,"), vf)
        self.assertIn("hfr=59.94->29.97 fps", decision)

    def test_4k50_to_25(self):
        _, vf, _, _ = self._run(fps=Fraction(50))
        self.assertTrue(vf.startswith("fps=25,"), vf)

    def test_4k120_halved_until_it_fits(self):
        _, vf, _, _ = self._run(fps=Fraction(120))
        self.assertTrue(vf.startswith("fps=30,"), vf)

    def test_4k24_and_4k30_untouched(self):
        for fps in (Fraction(24000, 1001), Fraction(30)):
            new, vf, decision, _ = self._run(fps=fps)
            self.assertNotIn("fps=", vf)
            self.assertNotIn("-skip_frame", new)
            self.assertNotIn("hfr=", decision)

    def test_1080p60_untouched(self):
        new, vf, decision, _ = self._run(fps=Fraction(60), w=1920, h=1080)
        self.assertNotIn("fps=", vf)
        self.assertNotIn("-skip_frame", new)

    def test_client_cap_below_target_wins(self):
        argv = jf_cmd(jf_vf(1920, 1080), codec="libx264")
        argv[argv.index("-y"):argv.index("-y")] = ["-r", "24"]
        new, vf, _, _ = self._run(argv, fps=Fraction(60))
        self.assertTrue(vf.startswith("fps=24,"), vf)
        self.assertEqual(new[new.index("-r") + 1], "24")

    def test_client_rate_above_target_is_lowered(self):
        argv = jf_cmd(jf_vf(1920, 1080), codec="libx264")
        argv[argv.index("-y"):argv.index("-y")] = ["-r", "60"]
        new, vf, _, _ = self._run(argv, fps=Fraction(60))
        self.assertEqual(new[new.index("-r") + 1], "30")   # else frames re-duplicated

    def test_too_few_survivors_halves_without_noref(self):
        # 33% kept -> noref would leave 20 fps, below the 30 target: fps= would
        # pad with repeats. Halve evenly with fps= alone, and say it's slow.
        new, vf, decision, _ = self._run(fps=Fraction(60), kept=130 / 390)
        self.assertTrue(vf.startswith("fps=30,"), vf)
        self.assertNotIn("-skip_frame", new)
        self.assertIn("noref would keep only 20 fps", decision)
        self.assertIn("likely below real time", decision)

    def test_just_enough_survivors_uses_noref(self):
        new, _, decision, _ = self._run(fps=Fraction(60), kept=0.54)   # 32.4 fps
        self.assertIn("-skip_frame", new)
        self.assertIn("noref keeps 54%", decision)

    def test_unmeasurable_share_halves_without_noref(self):
        new, vf, decision, _ = self._run(fps=Fraction(60), kept=None)
        self.assertTrue(vf.startswith("fps=30,"), vf)
        self.assertNotIn("-skip_frame", new)
        self.assertIn("share unknown", decision)

    def test_disabled_keeps_rate_but_says_so(self):
        cfg = dict(CFG, hfr_max_fps=0)
        with Logged() as lg:
            new, _ = run(jf_cmd(jf_vf(1920, 1080), codec="libx264"),
                         stub_probe(fps=Fraction(60)), cfg=cfg)
        self.assertNotIn("fps=", new[new.index("-vf") + 1])
        self.assertTrue(any("full rate" in l for l in lg.lines), lg.lines)

    def test_software_fallback_is_not_halved(self):
        # no hw slot -> software-decode rule: no -hwaccel, so no -skip_frame
        with Logged():
            new, _ = run(jf_cmd(jf_vf(1920, 1080), codec="libx264"),
                         stub_probe(fps=Fraction(60)), slot=False)
        self.assertNotIn("-skip_frame", new)


if __name__ == "__main__":
    unittest.main(verbosity=2)
