import json
import logging
import os
import subprocess

log = logging.getLogger("probe")

FFPROBE = os.environ.get("FFPROBE", "ffprobe")
MKVMERGE = os.environ.get("MKVMERGE", "mkvmerge")

KEEP_LANGS = ("eng", "en", "und")
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv")

BITSTREAM_SAMPLE_FRAMES = 12
MASTERING_SIDE_DATA = "Mastering display metadata"
CONTENT_LIGHT_SIDE_DATA = "Content light level metadata"
DOVI_SIDE_DATA = "DOVI configuration record"
CHROMATICITY_TOLERANCE = 1e-4
LUMINANCE_TOLERANCE = 1e-3
HDR_DECLARATIONS = ("mastering_display", "content_light", "dolby_vision")
BITSTREAM_KEY = {
    "mastering_display": "mastering_bitstream",
    "content_light": "content_light_bitstream",
    "dolby_vision": "dv_in_bitstream",
}
MASTERING_KEYS = (
    "red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y",
    "white_point_x", "white_point_y", "min_luminance", "max_luminance",
)

DEPTH_BY_PIX_FMT_SUFFIX = (
    ("12le", 12),
    ("12be", 12),
    ("10le", 10),
    ("10be", 10),
)


class ProbeError(RuntimeError):
    pass


#----- Tool invocation
def _run(cmd, timeout=600):
    log.debug("running %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    log.debug("exit %d from %s", proc.returncode, cmd[0])
    return proc.returncode, proc.stdout, proc.stderr


def ffprobe_json(path, extra=None, timeout=600):
    cmd = [FFPROBE, "-v", "error", "-of", "json"]
    if extra:
        cmd += list(extra)
    cmd.append(str(path))
    rc, out, err = _run(cmd, timeout=timeout)
    if rc != 0:
        raise ProbeError("ffprobe failed on %s: %s" % (path, (err or "").strip()[-400:]))
    try:
        return json.loads(out)
    except ValueError as exc:
        raise ProbeError("ffprobe returned unparseable JSON for %s: %s" % (path, exc))


def mkvmerge_json(path, timeout=600):
    rc, out, err = _run([MKVMERGE, "-J", str(path)], timeout=timeout)
    if rc not in (0, 1):
        raise ProbeError("mkvmerge -J failed on %s: %s" % (path, (out or err or "").strip()[-400:]))
    try:
        return json.loads(out)
    except ValueError as exc:
        raise ProbeError("mkvmerge returned unparseable JSON for %s: %s" % (path, exc))


#----- Pixel format and crop geometry
def bit_depth(pix_fmt):
    pix_fmt = (pix_fmt or "").lower()
    for suffix, depth in DEPTH_BY_PIX_FMT_SUFFIX:
        if pix_fmt.endswith(suffix):
            return depth
    return 8


def cropdetect_limit(depth):
    #----- cropdetect reads limit in the source's native bit depth, not normalised to 8-bit.
    limit = 24 * (2 ** (int(depth) - 8))
    log.debug("cropdetect limit %d for %d-bit source", limit, int(depth))
    return limit


def _ratio(text, default=1.0):
    if not text or text in ("N/A", "0:1", "0/1"):
        return default
    sep = ":" if ":" in text else "/"
    try:
        num, den = text.split(sep, 1)
        num = float(num)
        den = float(den)
    except ValueError:
        return default
    if den == 0:
        return default
    return num / den


#----- Duration
def usable_duration(video, container=None):
    value = _float_or_none((video or {}).get("duration"))
    if value:
        return value
    value = _float_or_none((container or {}).get("container_duration"))
    if value:
        log.debug("video stream reports no duration, using the container figure %.3f", value)
        return value
    return 0.0


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_cover_art(stream):
    if (stream.get("codec_name") or "").lower() in ("mjpeg", "png", "bmp", "gif"):
        return True
    return bool((stream.get("disposition") or {}).get("attached_pic"))


#----- The single probe pass
class Probe:
    def __init__(self, path, data, container):
        self.path = str(path)
        self.raw = data
        self.container = container

    @property
    def video(self):
        return self.container.get("video")

    def as_dict(self):
        d = dict(self.container)
        d["path"] = self.path
        return d


def probe(path):
    log.debug("probing %s", path)
    data = ffprobe_json(
        path, ["-show_streams", "-show_format", "-show_chapters"]
    )
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video = None
    cover_art = []
    audio = []
    subtitles = []
    other = []

    for s in streams:
        kind = s.get("codec_type")
        if kind == "video":
            if _is_cover_art(s):
                cover_art.append(_cover_summary(s))
            elif video is None:
                video = _video_summary(s)
            else:
                other.append(_stream_summary(s))
        elif kind == "audio":
            audio.append(_audio_summary(s))
        elif kind == "subtitle":
            subtitles.append(_subtitle_summary(s))
        else:
            other.append(_stream_summary(s))

    if video is None:
        raise ProbeError("no decodable video stream in %s" % path)

    if video["hdr"]:
        video.update(bitstream_hdr(path))
        video["hdr_declaration_gap"] = hdr_declaration_gap(video)

    container = {
        "video": video,
        "cover_art": cover_art,
        "audio": audio,
        "subtitles": subtitles,
        "other": other,
        "chapters": len(data.get("chapters") or []),
        "container_duration": _float_or_none(fmt.get("duration")),
        "format_name": fmt.get("format_name"),
        "segment_title": ((fmt.get("tags") or {}).get("title") or "").strip() or None,
        "size_bytes": int(fmt.get("size") or 0) or _size_on_disk(path),
        "audio_channels_max": max([a["channels"] for a in audio], default=0),
        "audio_default_count": sum(1 for a in audio if a["default"]),
        "subtitle_default_count": sum(
            1 for s in subtitles if s["default"] and not s["forced"]
        ),
        "foreign_tracks": [
            t["language"]
            for t in audio + subtitles
            if (t["language"] or "und").lower() not in KEEP_LANGS
        ],
    }
    log.info(
        "probed %s: %s %sx%s, %ss, %d audio, %d subtitle",
        os.path.basename(str(path)), video.get("codec"),
        video.get("display_width"), video.get("display_height"),
        container["container_duration"], len(audio), len(subtitles),
    )
    log.debug(
        "sar %s, bit depth %s, hdr=%s dv=%s, %d chapter(s), foreign %s",
        video.get("sar"), video.get("bit_depth"), video.get("hdr"),
        video.get("dolby_vision"), container["chapters"],
        container["foreign_tracks"] or "none",
    )
    if video.get("hdr_declaration_gap"):
        log.info(
            "%s under-declares its HDR metadata: container lacks %s carried in the bitstream",
            os.path.basename(str(path)), ", ".join(video["hdr_declaration_gap"]),
        )
    return Probe(path, data, container)


def _size_on_disk(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


#----- Per-stream summaries
def _video_summary(s):
    width = int(s.get("width") or 0)
    height = int(s.get("height") or 0)
    sar = _ratio(s.get("sample_aspect_ratio"), 1.0)
    display_width = int(round(width * sar)) if width else 0
    pix_fmt = s.get("pix_fmt") or ""
    depth = bit_depth(pix_fmt)
    dv = _dolby_vision(s)
    return {
        "codec": (s.get("codec_name") or "").lower(),
        "profile": s.get("profile"),
        "width": width,
        "height": height,
        "sar": sar,
        "display_width": display_width,
        "display_height": height,
        "display_pixels": display_width * height,
        "display_aspect": (display_width / height) if height else 0.0,
        "pix_fmt": pix_fmt,
        "bit_depth": depth,
        "cropdetect_limit": cropdetect_limit(depth),
        "frame_rate": _ratio(s.get("r_frame_rate"), 0.0),
        "avg_frame_rate": _ratio(s.get("avg_frame_rate"), 0.0),
        "frame_count": _frame_count(s),
        "bitrate": _video_bitrate(s),
        "duration": _float_or_none(s.get("duration")),
        "color_primaries": s.get("color_primaries"),
        "color_transfer": s.get("color_transfer"),
        "color_space": s.get("color_space"),
        "language": ((s.get("tags") or {}).get("language") or "und").lower(),
        "hdr": _is_hdr(s),
        "hdr_format": _hdr_format(s),
        "colour_tagged": _is_colour_tagged(s),
        "dolby_vision": dv is not None,
        "dv": dv,
        "mastering_display": _mastering_display(s),
        "content_light": _content_light(s),
        "mastering_bitstream": None,
        "content_light_bitstream": None,
        "dv_in_bitstream": False,
        "hdr_declaration_gap": [],
    }


def _frame_count(s):
    tags = s.get("tags") or {}
    for key in ("NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng", "nb_frames"):
        value = tags.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    try:
        return int(s.get("nb_frames"))
    except (TypeError, ValueError):
        return None


def _hms_to_seconds(value):
    try:
        hours, minutes, seconds = str(value).split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def _video_bitrate(s):
    tags = {str(k).upper(): v for k, v in (s.get("tags") or {}).items()}
    for key in ("BPS", "BPS-ENG"):
        value = tags.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    duration = _float_or_none(s.get("duration")) or _hms_to_seconds(tags.get("DURATION"))
    if duration:
        for key in ("NUMBER_OF_BYTES", "NUMBER_OF_BYTES-ENG"):
            value = tags.get(key)
            if value:
                try:
                    return int(round(int(value) * 8 / duration))
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
    try:
        return int(s.get("bit_rate"))
    except (TypeError, ValueError):
        return None


def count_video_frames(path):
    data = ffprobe_json(
        path,
        ["-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets"],
    )
    streams = data.get("streams") or []
    if not streams:
        return None
    try:
        return int(streams[0].get("nb_read_packets"))
    except (TypeError, ValueError):
        return None


def _dolby_vision(s):
    for side in s.get("side_data_list") or []:
        if "dv_profile" in side or side.get("side_data_type") == "DOVI configuration record":
            return {
                "dv_profile": side.get("dv_profile"),
                "dv_level": side.get("dv_level"),
                "rpu_present_flag": side.get("rpu_present_flag"),
                "bl_present_flag": side.get("bl_present_flag"),
                "el_present_flag": side.get("el_present_flag"),
            }
    return None


def _is_hdr(s):
    prim = (s.get("color_primaries") or "").lower()
    trc = (s.get("color_transfer") or "").lower()
    spc = (s.get("color_space") or "").lower()
    return (
        prim.startswith("bt2020")
        or spc.startswith("bt2020")
        or trc in ("smpte2084", "arib-std-b67")
    )


def _hdr_format(s):
    trc = (s.get("color_transfer") or "").lower()
    if trc == "smpte2084":
        return "hdr10"
    if trc == "arib-std-b67":
        return "hlg"
    if _is_hdr(s):
        return "bt2020"
    return "none"


def _side_data(s, side_data_type):
    for side in s.get("side_data_list") or []:
        if side.get("side_data_type") == side_data_type:
            return side
    return None


def _mastering_from(side):
    if not side:
        return None
    return {key: _ratio(str(side.get(key) or ""), 0.0) for key in MASTERING_KEYS}


def _content_light_from(side):
    if not side:
        return None
    try:
        return {
            "max_content": int(side.get("max_content") or 0),
            "max_average": int(side.get("max_average") or 0),
        }
    except (TypeError, ValueError):
        return None


def _mastering_display(s):
    return _mastering_from(_side_data(s, MASTERING_SIDE_DATA))


def _content_light(s):
    return _content_light_from(_side_data(s, CONTENT_LIGHT_SIDE_DATA))


def _same_mastering(a, b):
    if not a or not b:
        return False
    for key in MASTERING_KEYS:
        tolerance = LUMINANCE_TOLERANCE if "luminance" in key else CHROMATICITY_TOLERANCE
        if "luminance" in key:
            scale = max(abs(a.get(key) or 0.0), abs(b.get(key) or 0.0), 1e-9)
            if abs((a.get(key) or 0.0) - (b.get(key) or 0.0)) / scale > tolerance:
                return False
        elif abs((a.get(key) or 0.0) - (b.get(key) or 0.0)) > tolerance:
            return False
    return True


def hdr_declaration_gap(video):
    gap = []
    mastering = video.get("mastering_bitstream")
    if mastering and not _same_mastering(mastering, video.get("mastering_display")):
        gap.append("mastering_display")
    light = video.get("content_light_bitstream")
    if light and light != video.get("content_light"):
        gap.append("content_light")
    if video.get("dv_in_bitstream") and not video.get("dolby_vision"):
        gap.append("dolby_vision")
    return gap


def _is_colour_tagged(s):
    prim = (s.get("color_primaries") or "").lower()
    return bool(prim) and prim not in ("unknown", "unspecified", "n/a")


def _cover_summary(s):
    return {
        "codec": (s.get("codec_name") or "").lower(),
        "index": s.get("index"),
        "language": ((s.get("tags") or {}).get("language") or "und").lower(),
    }


def _audio_summary(s):
    disp = s.get("disposition") or {}
    tags = s.get("tags") or {}
    return {
        "index": s.get("index"),
        "codec": (s.get("codec_name") or "").lower(),
        "channels": int(s.get("channels") or 0),
        "channel_layout": s.get("channel_layout"),
        "language": (tags.get("language") or "und").lower(),
        "title": tags.get("title"),
        "default": bool(disp.get("default")),
        "forced": bool(disp.get("forced")),
        "comment": bool(disp.get("comment")),
    }


def _subtitle_summary(s):
    disp = s.get("disposition") or {}
    tags = s.get("tags") or {}
    return {
        "index": s.get("index"),
        "codec": (s.get("codec_name") or "").lower(),
        "language": (tags.get("language") or "und").lower(),
        "title": tags.get("title"),
        "default": bool(disp.get("default")),
        "forced": bool(disp.get("forced")),
    }


def _stream_summary(s):
    return {
        "index": s.get("index"),
        "codec_type": s.get("codec_type"),
        "codec": (s.get("codec_name") or "").lower(),
    }


#----- Direct stream queries
def bitstream_hdr(path, frames=BITSTREAM_SAMPLE_FRAMES):
    data = ffprobe_json(
        path,
        [
            "-select_streams", "v:0",
            "-read_intervals", "%%+#%d" % int(frames),
            "-show_entries", "frame=pts:frame_side_data",
        ],
    )
    mastering = None
    light = None
    dv = False
    for frame in data.get("frames") or []:
        for side in frame.get("side_data_list") or []:
            kind = side.get("side_data_type") or ""
            if kind == MASTERING_SIDE_DATA and mastering is None:
                mastering = _mastering_from(side)
            elif kind == CONTENT_LIGHT_SIDE_DATA and light is None:
                light = _content_light_from(side)
            elif "Dolby Vision" in kind:
                dv = True
    log.debug(
        "bitstream of %s: mastering=%s content_light=%s dolby_vision=%s over %d frame(s)",
        os.path.basename(str(path)), mastering is not None, light is not None, dv,
        len(data.get("frames") or []),
    )
    return {
        "mastering_bitstream": mastering,
        "content_light_bitstream": light,
        "dv_in_bitstream": dv,
    }


def video_duration(path):
    data = ffprobe_json(path, ["-select_streams", "v:0", "-show_entries", "stream=duration"])
    streams = data.get("streams") or []
    if streams:
        value = _float_or_none(streams[0].get("duration"))
        if value:
            return value
    data = ffprobe_json(path, ["-show_entries", "format=duration"])
    return _float_or_none((data.get("format") or {}).get("duration"))


def track_selectors(path):
    data = mkvmerge_json(path)
    log.debug("deriving type-relative track selectors for %s", os.path.basename(str(path)))
    counters = {}
    rows = []
    for track in data.get("tracks") or []:
        kind = track.get("type")
        prefix = {"video": "v", "audio": "a", "subtitles": "s"}.get(kind)
        if prefix is None:
            continue
        counters[prefix] = counters.get(prefix, 0) + 1
        props = track.get("properties") or {}
        rows.append(
            {
                "selector": "%s%d" % (prefix, counters[prefix]),
                "type": kind,
                "id": track.get("id"),
                "codec_id": props.get("codec_id", ""),
                "language": (props.get("language") or "und").lower(),
                "default": bool(props.get("default_track")),
                "forced": bool(props.get("forced_track")),
                "name": props.get("track_name") or "",
            }
        )
    for row in rows:
        log.debug(
            "selector %s is mkvmerge id %s, %s, lang %s, default=%s forced=%s",
            row["selector"], row["id"], row["type"],
            row["language"], row["default"], row["forced"],
        )
    return rows, data
