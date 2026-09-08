import logging
import os
import re

from . import probe as probemod

log = logging.getLogger("standards")

MOVIE_MIN_DISPLAY_WIDTH = 1920
MOVIE_MIN_DISPLAY_HEIGHT = 800
MOVIE_MIN_RUNTIME_S = 40 * 60
TV_MIN_RUNTIME_S = 15 * 60

LETTERBOX_MAX_BARS_PX = 20

SAMPLE_PATTERNS = (
    re.compile(r"(?:^|[^a-z])sample(?:$|[^a-z])", re.I),
    re.compile(r"\btrailer\b", re.I),
    re.compile(r"\bfeaturette\b", re.I),
    re.compile(r"\bbehind[. _-]the[. _-]scenes\b", re.I),
    re.compile(r"\bdeleted[. _-]scenes?\b", re.I),
)

KEEP_LANGS = ("eng", "en", "und")

PAL_HEIGHTS = (576, 288)
PAL_RATE_TOLERANCE = 0.01

LETTERBOX_CANDIDATE_ASPECTS = ((16.0 / 9.0), (4.0 / 3.0))
ASPECT_TOLERANCE = 0.02


class Verdict:
    def __init__(self, ok, problems=None, warnings=None):
        self.ok = ok
        self.problems = list(problems or [])
        self.warnings = list(warnings or [])

    def as_dict(self):
        return {"ok": self.ok, "problems": self.problems, "warnings": self.warnings}

    def __repr__(self):
        return "<Verdict ok=%s problems=%r>" % (self.ok, self.problems)


#----- Individual checks
def looks_like_sample(path):
    name = os.path.basename(str(path))
    return any(p.search(name) for p in SAMPLE_PATTERNS)


def is_pal_speedup_suspect(video):
    rate = float(video.get("frame_rate") or 0)
    if abs(rate - 25.0) > PAL_RATE_TOLERANCE:
        return False
    return int(video.get("height") or 0) in PAL_HEIGHTS


def is_letterbox_candidate(video):
    aspect = float(video.get("display_aspect") or 0)
    if not aspect:
        return False
    return any(abs(aspect - a) < ASPECT_TOLERANCE for a in LETTERBOX_CANDIDATE_ASPECTS)


def _human_runtime(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    return "%d min" % (seconds // 60)


def runtime_seconds(container):
    return probemod.usable_duration(container.get("video") or {}, container)


#----- The gate
def screen(container, kind, path=None, crop=None):
    problems = []
    warnings = []
    log.debug("screening as %s, crop=%s, path=%s", kind, crop, path)

    video = container.get("video") or {}
    audio = container.get("audio") or []

    if path and looks_like_sample(path):
        problems.append("looks like a sample or extras file: %s" % os.path.basename(str(path)))

    if not audio:
        problems.append("no audio streams")
    elif not any((a.get("language") or "und").lower() in KEEP_LANGS for a in audio):
        problems.append(
            "no audio track tagged eng or und, found %s"
            % ", ".join(sorted({(a.get("language") or "und") for a in audio}))
        )

    if is_pal_speedup_suspect(video):
        problems.append(
            "25 fps at %dx%d, likely a PAL speed-up of film material"
            % (video.get("width") or 0, video.get("height") or 0)
        )

    if crop:
        bars = crop.get("bars_px") or 0
        if bars > LETTERBOX_MAX_BARS_PX:
            problems.append(
                "baked-in letterbox of %d px exceeds the %d px limit"
                % (bars, LETTERBOX_MAX_BARS_PX)
            )
    elif is_letterbox_candidate(video):
        warnings.append(
            "display aspect %.3f can hide baked-in bars, cropdetect required"
            % float(video.get("display_aspect") or 0)
        )

    runtime = runtime_seconds(container)

    if kind == "movie":
        dw = int(video.get("display_width") or 0)
        dh = int(video.get("display_height") or 0)
        if dw < MOVIE_MIN_DISPLAY_WIDTH or dh < MOVIE_MIN_DISPLAY_HEIGHT:
            problems.append(
                "movie display resolution %dx%d is below the %dx%d floor"
                % (dw, dh, MOVIE_MIN_DISPLAY_WIDTH, MOVIE_MIN_DISPLAY_HEIGHT)
            )
        if runtime and runtime < MOVIE_MIN_RUNTIME_S:
            problems.append(
                "movie runtime %s is below the %d min floor"
                % (_human_runtime(runtime), MOVIE_MIN_RUNTIME_S / 60)
            )
    elif kind == "tv":
        if runtime and runtime < TV_MIN_RUNTIME_S:
            problems.append(
                "episode runtime %s is below the %d min floor"
                % (_human_runtime(runtime), TV_MIN_RUNTIME_S / 60)
            )
    else:
        problems.append("unknown kind %r" % kind)

    if not runtime:
        warnings.append("no usable duration reported, runtime floor not applied")

    verdict = Verdict(not problems, problems, warnings)
    if problems:
        log.info("standards failed: %s", "; ".join(problems))
    else:
        log.info("standards passed%s", ", warnings: " + "; ".join(warnings) if warnings else "")
    log.debug(
        "runtime %ss, display %sx%s, %d audio track(s)",
        runtime, video.get("display_width"), video.get("display_height"), len(audio),
    )
    return verdict
