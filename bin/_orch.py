"""Shared helpers for the rpi-ffmpeg transcode orchestrator shim.

Kept dependency-free (stdlib only, tomllib is 3.11+). Both bin/ffmpeg and
bin/ffprobe import this by adding their own directory to sys.path, so it works
regardless of where Jellyfin invokes the shim from.
"""
import os
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None


def _candidate_config_paths():
    env = os.environ.get("RPI_ORCH_RULES")
    if env:
        yield env
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, "..", "rules.toml")
    yield "/etc/rpi-ffmpeg-orchestrator/rules.toml"


def load_config():
    """Return the parsed rules.toml, or None if it can't be found/parsed.

    A None return means the shim must degrade to pure passthrough — never a
    hard failure, so playback can't break because of a bad config.
    """
    if tomllib is None:
        return None
    for path in _candidate_config_paths():
        try:
            with open(path, "rb") as fh:
                cfg = tomllib.load(fh)
            cfg.setdefault("_path", os.path.abspath(path))
            return cfg
        except FileNotFoundError:
            continue
        except Exception as exc:  # malformed toml, permissions, ...
            log("config load failed (%s): %s" % (path, exc))
            return None
    return None


def real_binary(cfg, which, fallback_name):
    """Resolve the real ffmpeg/ffprobe path from config, then PATH, then a
    known dev-build location. `which` is 'ffmpeg' or 'ffprobe'."""
    if cfg:
        p = cfg.get(which)
        if p and os.path.exists(p):
            return p
    # dev fallback: the fork build in this repo
    dev = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "ffmpeg-jellyfin", fallback_name)
    if os.path.exists(dev):
        return os.path.abspath(dev)
    # last resort: PATH
    from shutil import which as _which
    found = _which(fallback_name)
    return found or fallback_name


def log(msg):
    """One-line diagnostic to stderr; lands in Jellyfin's per-job ffmpeg log."""
    try:
        sys.stderr.write("[rpi-orch] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def exec_real(binary, argv):
    """Replace this process with the real binary. Never returns on success."""
    os.execv(binary, [binary] + list(argv))
