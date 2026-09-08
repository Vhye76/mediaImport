import logging
import os
import re
import subprocess
import threading

from . import probe as probemod

log = logging.getLogger("media")

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
MKVMERGE = os.environ.get("MKVMERGE", "mkvmerge")
MKVPROPEDIT = os.environ.get("MKVPROPEDIT", "mkvpropedit")

KEEP_LANGS = ("eng", "en", "und")

AVI_DURATION_TOLERANCE_S = 2.0
REMUX_DURATION_TOLERANCE_S = 2.0

GRAIN_SAMPLE_SECONDS = 20
GRAIN_SAMPLE_POSITION = 0.45
GRAIN_THRESHOLD = float(os.environ.get("GRAIN_THRESHOLD", "0.18"))
GRAIN_PROBE_CRF = "20"
GRAIN_PROBE_PRESET = "ultrafast"

CROP_SAMPLE_POSITIONS = (0.25, 0.45, 0.65)
CROP_SAMPLE_FRAMES = 40
CROP_MIN_BARS_PX = 10

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


class MediaError(RuntimeError):
    pass


#----- Process execution
def run(cmd, timeout=None):
    log.debug("running %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_cancellable(cmd, register=None, unregister=None, on_progress=None):
    log.debug("running %s", " ".join(str(c) for c in cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if register:
        register(proc)

    errors = []

    def drain_stderr():
        for line in proc.stderr:
            errors.append(line)

    #----- stderr must be drained or the child blocks once its pipe fills.
    drainer = threading.Thread(target=drain_stderr, name="stderr-drain", daemon=True)
    drainer.start()

    fields = {}
    try:
        for line in proc.stdout:
            key, sep, value = line.strip().partition("=")
            if not sep:
                continue
            fields[key] = value
            if key == "progress":
                if on_progress:
                    try:
                        on_progress(dict(fields))
                    except Exception:
                        log.exception("progress callback failed")
                fields.clear()
        proc.wait()
    finally:
        drainer.join(timeout=10)
        if unregister:
            unregister(proc)
    return Result(proc.returncode, "", "".join(errors))


#----- Stream counting
def chapter_count(path):
    data = probemod.ffprobe_json(path, ["-show_chapters"])
    return len(data.get("chapters") or [])


def packet_count(path, stream="v:0"):
    data = probemod.ffprobe_json(
        path, ["-select_streams", stream, "-count_packets", "-show_entries", "stream=nb_read_packets"]
    )
    streams = data.get("streams") or []
    if not streams:
        return 0
    try:
        return int(streams[0].get("nb_read_packets") or 0)
    except (TypeError, ValueError):
        return 0


#----- Container conversion
def to_matroska(src, dst, source_format=None):
    src = str(src)
    fmt = (source_format or os.path.splitext(src)[1].lstrip(".")).lower()
    log.debug("container conversion of %s, detected format %r", os.path.basename(src), fmt)
    if fmt in ("avi", "divx", "xvid"):
        return _avi_to_mkv(src, dst)
    return _mp4_to_mkv(src, dst)


def _mp4_to_mkv(src, dst):
    chapters_in = chapter_count(src)
    tmp = str(dst) + ".part"
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
        "-map", "0:v", "-map", "0:a", "-map", "0:s?", "-map_chapters", "0",
        "-c:v", "copy", "-c:a", "copy", "-c:s", "srt",
    #----- a .part temp name gives ffmpeg no muxer to infer, so it is named explicitly.
        "-f", "matroska", tmp,
    ]
    proc = run(cmd)
    if proc.returncode != 0:
        _unlink(tmp)
        raise MediaError("mp4 to mkv remux failed: %s" % (proc.stderr or "").strip()[-400:])
    chapters_out = chapter_count(tmp)
    if chapters_in and chapters_out != chapters_in:
        _unlink(tmp)
        raise MediaError(
            "chapter count changed in remux: %d in, %d out" % (chapters_in, chapters_out)
        )
    _verify_duration(src, tmp, REMUX_DURATION_TOLERANCE_S)
    os.replace(tmp, dst)
    log.info("remuxed mp4 to mkv, %d chapter(s) preserved", chapters_out)
    log.debug("chapters in %d, out %d", chapters_in, chapters_out)
    return {"method": "mp4_to_mkv", "chapters": chapters_out}


def _avi_to_mkv(src, dst):
    packets_in = packet_count(src)
    tmp = str(dst) + ".part"
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts", "-i", src,
        "-map", "0:v", "-map", "0:a", "-map", "0:s?",
        "-c:v", "copy", "-c:a", "copy", "-c:s", "copy",
        "-bsf:v", "mpeg4_unpack_bframes",
        "-f", "matroska", tmp,
    ]
    proc = run(cmd)
    if proc.returncode != 0:
        _unlink(tmp)
        raise MediaError("avi to mkv remux failed: %s" % (proc.stderr or "").strip()[-400:])
    packets_out = packet_count(tmp)
    result = {"method": "avi_to_mkv", "packets_in": packets_in, "packets_out": packets_out}
    if packets_in and packets_out != packets_in:
        _unlink(tmp)
        raise MediaError(
            "packet count changed in remux: %d in, %d out" % (packets_in, packets_out)
        )
    try:
        _verify_duration(src, tmp, AVI_DURATION_TOLERANCE_S)
    except MediaError as exc:
        result["duration_note"] = str(exc)
        log.info("avi remux duration note: %s", exc)
    os.replace(tmp, dst)
    log.info("remuxed avi to mkv, %d packet(s) preserved", packets_out)
    log.debug("packets in %d, out %d", packets_in, packets_out)
    return result


def _verify_duration(src, out, tolerance):
    a = probemod.video_duration(src)
    b = probemod.video_duration(out)
    log.debug("video stream duration in %s, out %s, tolerance %ss", a, b, tolerance)
    if a and b and abs(a - b) > tolerance:
        raise MediaError(
            "video stream duration moved by %.1fs (source %.1f, output %.1f)" % (b - a, a, b)
        )


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


#----- Language policy and track flags
def strip_foreign(src, dst):
    rows, data = probemod.track_selectors(src)
    keep_audio = []
    keep_subs = []
    dropped = []
    counters = {"audio": 0, "subtitles": 0}
    for track in data.get("tracks") or []:
        kind = track.get("type")
        if kind not in counters:
            continue
        props = track.get("properties") or {}
        lang = (props.get("language") or "und").lower()
        target = keep_audio if kind == "audio" else keep_subs
        if lang in KEEP_LANGS:
            target.append(str(track.get("id")))
        else:
            dropped.append("%s:%s" % (kind, lang))

    if not dropped:
        log.info("language strip: nothing to drop, all tracks are eng or und")
        return {"stripped": 0, "dropped": [], "output": str(src)}

    tmp = str(dst) + ".part"
    cmd = [MKVMERGE, "-o", tmp]
    cmd += ["-a", ",".join(keep_audio)] if keep_audio else ["-A"]
    cmd += ["-s", ",".join(keep_subs)] if keep_subs else ["-S"]
    cmd += [str(src)]
    proc = run(cmd)
    if proc.returncode not in (0, 1):
        _unlink(tmp)
        raise MediaError("language strip failed: %s" % (proc.stdout or "").strip()[-400:])
    _verify_duration(src, tmp, REMUX_DURATION_TOLERANCE_S)
    os.replace(tmp, dst)
    log.info("language strip dropped %d track(s): %s", len(dropped), ", ".join(dropped))
    log.debug("kept audio ids %s, subtitle ids %s", keep_audio or "none", keep_subs or "none")
    return {"stripped": len(dropped), "dropped": dropped, "output": str(dst)}


def fix_flags_and_language(path):
    rows, _ = probemod.track_selectors(path)
    args = []
    first_audio = None
    for row in rows:
        if row["type"] == "video":
            if "V_MJPEG" not in (row["codec_id"] or "").upper():
                args += ["--edit", "track:%s" % row["selector"], "--set", "language=eng"]
        elif row["type"] == "audio":
            if first_audio is None:
                first_audio = row["selector"]
            wanted = 1 if row["selector"] == first_audio else 0
            args += [
                "--edit", "track:%s" % row["selector"],
                "--set", "flag-default=%d" % wanted,
            ]
        elif row["type"] == "subtitles" and not row["forced"]:
            args += ["--edit", "track:%s" % row["selector"], "--set", "flag-default=0"]

    if not args:
        log.info("track flags and languages already correct, no edit needed")
        return {"edits": 0}

    proc = run([MKVPROPEDIT, str(path)] + args)
    if proc.returncode != 0:
        raise MediaError(
            "mkvpropedit flag fix failed rc=%d: %s"
            % (proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[-400:])
        )
    log.info(
        "track flags repaired, %d edit(s), default audio is %s",
        len(args) // 4, first_audio,
    )
    log.debug("mkvpropedit args: %s", " ".join(args))
    return {"edits": len(args) // 4}


#----- Crop detection
def detect_crop(path, video, container=None):
    depth = int(video.get("bit_depth") or 8)
    limit = probemod.cropdetect_limit(depth)
    height = int(video.get("height") or 0)
    duration = probemod.usable_duration(video, container)
    if not duration or not height:
        log.warning(
            "cropdetect could not run on %s, no usable duration (%s) or height (%s)",
            os.path.basename(str(path)), duration, height,
        )
        return None

    best = None
    for fraction in CROP_SAMPLE_POSITIONS:
        offset = int(duration * fraction)
        proc = run(
            [
                FFMPEG, "-hide_banner", "-nostdin", "-ss", str(offset), "-i", str(path),
                "-vf", "cropdetect=limit=%d:round=2:reset=0" % limit,
                "-frames:v", str(CROP_SAMPLE_FRAMES), "-f", "null", "-",
            ]
        )
        found = _CROP_RE.findall(proc.stderr or "")
        if not found:
            continue
        cw, ch, cx, cy = (int(v) for v in found[-1])
        if best is None or ch > best[1]:
            best = (cw, ch, cx, cy)

    if best is None:
        return None

    cw, ch, cx, cy = best
    bars = height - ch
    if bars < CROP_MIN_BARS_PX:
        log.info("cropdetect found %d px of bars, below the %d px floor, no crop applied",
                 bars, CROP_MIN_BARS_PX)
        return None
    log.info("cropdetect: %d px of bars, cropping to %dx%d", bars, cw, ch)
    log.debug("crop offsets x=%d y=%d, limit %d at %d-bit", cx, cy, limit, depth)
    return {
        "filter": "crop=%d:%d:%d:%d" % (cw, ch, cx, cy),
        "width": cw,
        "height": ch,
        "bars_px": bars,
        "limit": limit,
        "bit_depth": depth,
        "picture_pixels": int(round(cw * float(video.get("sar") or 1.0))) * ch,
    }


#----- Grain measurement
def grain_probe(path, video, workdir, threshold=None, container=None):
    threshold = GRAIN_THRESHOLD if threshold is None else threshold
    duration = probemod.usable_duration(video, container)
    if not duration:
        log.warning(
            "grain probe could not run on %s, no usable duration",
            os.path.basename(str(path)),
        )
        return {"grain": False, "ratio": None, "reason": "no usable duration"}
    if duration < GRAIN_SAMPLE_SECONDS * 2:
        log.info("grain probe skipped, clip is %.1fs, shorter than twice the sample", duration)
        return {"grain": False, "ratio": None, "reason": "clip too short to sample"}

    offset = int(duration * GRAIN_SAMPLE_POSITION)
    clean = os.path.join(workdir, "grain_clean.mkv")
    denoised = os.path.join(workdir, "grain_denoised.mkv")

    sizes = {}
    for label, target, filters in (
        ("clean", clean, None),
        ("denoised", denoised, "hqdn3d=4:3:6:4.5"),
    ):
        cmd = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(offset), "-t", str(GRAIN_SAMPLE_SECONDS), "-i", str(path),
            "-map", "0:v:0", "-an", "-sn",
        ]
        if filters:
            cmd += ["-vf", filters]
        cmd += [
            "-c:v", "libx265", "-preset", GRAIN_PROBE_PRESET, "-crf", GRAIN_PROBE_CRF,
            "-x265-params", "log-level=none",
            "-f", "matroska", target,
        ]
        proc = run(cmd)
        if proc.returncode != 0:
            _unlink(clean)
            _unlink(denoised)
            return {
                "grain": False,
                "ratio": None,
                "reason": "probe encode failed: %s" % (proc.stderr or "").strip()[-200:],
            }
        sizes[label] = os.path.getsize(target)

    _unlink(clean)
    _unlink(denoised)

    if not sizes.get("clean"):
        return {"grain": False, "ratio": None, "reason": "probe produced an empty sample"}

    ratio = (sizes["clean"] - sizes["denoised"]) / float(sizes["clean"])
    log.info("grain probe ratio %.4f against threshold %s: %s",
             ratio, threshold, "grainy" if ratio >= threshold else "clean")
    log.debug("sample sizes clean %d bytes, denoised %d bytes",
              sizes["clean"], sizes["denoised"])
    return {
        "grain": ratio >= threshold,
        "ratio": round(ratio, 4),
        "threshold": threshold,
        "clean_bytes": sizes["clean"],
        "denoised_bytes": sizes["denoised"],
        "reason": "denoise removed %.1f%% of the encoded size" % (ratio * 100),
    }
