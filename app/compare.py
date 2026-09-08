import logging
import os
import re

log = logging.getLogger("compare")

WIN = "win"
LOSS = "loss"
AMBIGUOUS = "ambiguous"

PIXEL_TOLERANCE = 0.05

PEDIGREE_ORDER = ("web", "encode", "remux")
PEDIGREE_PATTERNS = (
    ("remux", re.compile(r"\bremux\b", re.I)),
    ("web", re.compile(r"\b(?:web-?dl|web-?rip|amzn|nf|dsnp|hmax|atvp)\b", re.I)),
    ("encode", re.compile(r"\b(?:blu-?ray|bdrip|brrip|dvdrip|hdtv|[xh]26[45]|hevc|avc)\b", re.I)),
)


def pedigree(name):
    base = os.path.basename(str(name))
    for label, pattern in PEDIGREE_PATTERNS:
        if pattern.search(base):
            return label
    return None


def _pedigree_rank(label):
    try:
        return PEDIGREE_ORDER.index(label)
    except ValueError:
        return -1


MEASURED = (
    ("path", "file", None),
    ("codec", "video codec", None),
    ("profile", "video profile", None),
    ("dolby_vision", "Dolby Vision", 1),
    ("hdr", "HDR", 1),
    ("display_width", "display width", 2),
    ("display_height", "display height", 2),
    ("display_pixels", "display pixels", 2),
    ("display_aspect", "display aspect", None),
    ("stored_width", "stored width", None),
    ("stored_height", "stored height", None),
    ("sar", "sample aspect", None),
    ("picture_pixels", "picture pixels after bars", 3),
    ("letterbox_px", "baked-in letterbox px", 3),
    ("audio_channels_max", "audio channels", 4),
    ("audio_codecs", "audio codecs", None),
    ("audio_tracks", "audio tracks", None),
    ("audio_default_count", "default audio tracks", None),
    ("bit_depth", "bit depth", 5),
    ("pix_fmt", "pixel format", None),
    ("pedigree", "source pedigree", 6),
    ("frame_rate", "frame rate", None),
    ("frame_count", "frame count", None),
    ("duration_s", "runtime seconds", None),
    ("colour_primaries", "colour primaries", None),
    ("colour_transfer", "colour transfer", None),
    ("colour_space", "colour space", None),
    ("colour_tagged", "colour tagged", None),
    ("subtitle_codecs", "subtitle codecs", None),
    ("subtitle_tracks", "subtitle tracks", None),
    ("subtitle_default_count", "default subtitle tracks", None),
    ("foreign_tracks", "foreign language tracks", None),
    ("chapters", "chapters", None),
    ("cover_art", "cover art tracks", None),
    ("segment_title", "segment title", None),
    ("tag_structure", "tag structure", None),
    ("statistics_ratio", "statistics byte-sum ratio", None),
    ("size_bytes", "file size", None),
)

GATE_BY_ATTRIBUTE = {key: gate for key, _, gate in MEASURED if gate}


def _joined(values):
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return ", ".join(seen) or None


def attributes(container, path=None, crop=None, tag_structure=None, statistics_ratio=None):
    video = container.get("video") or {}
    audio = container.get("audio") or []
    subtitles = container.get("subtitles") or []
    duration = video.get("duration") or container.get("container_duration")
    return {
        "path": str(path) if path else None,
        "codec": video.get("codec"),
        "profile": video.get("profile"),
        "hdr": bool(video.get("hdr")),
        "dolby_vision": bool(video.get("dolby_vision")),
        "display_width": int(video.get("display_width") or 0),
        "display_height": int(video.get("display_height") or 0),
        "display_pixels": int(video.get("display_pixels") or 0),
        "display_aspect": round(float(video.get("display_aspect") or 0.0), 3) or None,
        "stored_width": int(video.get("width") or 0),
        "stored_height": int(video.get("height") or 0),
        "sar": round(float(video.get("sar") or 0.0), 4) or None,
        "bit_depth": int(video.get("bit_depth") or 8),
        "pix_fmt": video.get("pix_fmt"),
        "frame_rate": round(float(video.get("frame_rate") or 0.0), 5) or None,
        "frame_count": int(video.get("frame_count") or 0) or None,
        "duration_s": round(float(duration), 3) if duration else None,
        "colour_primaries": video.get("color_primaries"),
        "colour_transfer": video.get("color_transfer"),
        "colour_space": video.get("color_space"),
        "colour_tagged": bool(video.get("colour_tagged")),
        "audio_channels_max": int(container.get("audio_channels_max") or 0),
        "audio_codecs": _joined([a.get("codec") for a in audio]),
        "audio_tracks": len(audio),
        "audio_default_count": int(container.get("audio_default_count") or 0),
        "subtitle_codecs": _joined([s.get("codec") for s in subtitles]),
        "subtitle_tracks": len(subtitles),
        "subtitle_default_count": int(container.get("subtitle_default_count") or 0),
        "foreign_tracks": _joined(container.get("foreign_tracks") or []),
        "chapters": int(container.get("chapters") or 0),
        "cover_art": len(container.get("cover_art") or []),
        "segment_title": container.get("segment_title"),
        "picture_pixels": (crop or {}).get("picture_pixels"),
        "letterbox_px": (crop or {}).get("bars_px"),
        "tag_structure": tag_structure,
        "statistics_ratio": statistics_ratio,
        "pedigree": pedigree(path) if path else None,
        "size_bytes": int(container.get("size_bytes") or 0),
    }


def measure(path, crop=None):
    from . import probe as probemod, tags as tagsmod

    container = probemod.probe(path).container
    video = container.get("video") or {}
    if not video.get("frame_count"):
        try:
            video["frame_count"] = probemod.count_video_frames(path)
        except probemod.ProbeError as exc:
            log.debug("frame count unavailable on %s: %s", path, exc)
    structure = None
    ratio = None
    try:
        structure = "flattened" if tagsmod.is_flattened(tagsmod.read_tags(path)) else "targeted"
    except Exception as exc:
        log.debug("tag structure unreadable on %s: %s", path, exc)
    try:
        ratio = round(tagsmod.byte_sum_ratio(path), 4)
    except Exception as exc:
        log.debug("byte-sum ratio unreadable on %s: %s", path, exc)
    return attributes(container, path, crop=crop, tag_structure=structure, statistics_ratio=ratio)


def speed_mismatch(incoming, incumbent):
    new_frames = incoming.get("frame_count")
    old_frames = incumbent.get("frame_count")
    new_rate = incoming.get("frame_rate")
    old_rate = incumbent.get("frame_rate")
    if not (new_frames and old_frames and new_rate and old_rate):
        return None
    if abs(new_frames - old_frames) > 1:
        return None
    if abs(new_rate - old_rate) < 0.01:
        return None
    faster, slower = (
        ("incoming", "incumbent") if new_rate > old_rate else ("incumbent", "incoming")
    )
    ratio = max(new_rate, old_rate) / min(new_rate, old_rate)
    return (
        "the %s carries the same frame count at %.3f fps against %.3f, a %.1f%% speed-up;"
        " the %s runs at the correct rate"
        % (
            faster,
            max(new_rate, old_rate),
            min(new_rate, old_rate),
            (ratio - 1.0) * 100.0,
            slower,
        )
    )


class Comparison:
    def __init__(self, verdict, gate, reason, incoming, incumbent, notes=None):
        self.verdict = verdict
        self.gate = gate
        self.reason = reason
        self.incoming = incoming
        self.incumbent = incumbent
        self.notes = list(notes or [])

    @property
    def is_win(self):
        return self.verdict == WIN

    @property
    def is_loss(self):
        return self.verdict == LOSS

    def table(self, output=None):
        rows = []
        for key, label, gate in MEASURED:
            new = self.incoming.get(key)
            old = self.incumbent.get(key)
            row = {
                "attribute": key,
                "label": label,
                "gate": gate,
                "decided": gate is not None and gate == self.gate,
                "incoming": new,
                "incumbent": old,
                "differs": new != old,
                "measurable": new is not None and old is not None,
            }
            if output is not None:
                row["output"] = output.get(key)
            rows.append(row)
        return rows

    def as_dict(self, output=None):
        return {
            "verdict": self.verdict,
            "gate": self.gate,
            "reason": self.reason,
            "notes": self.notes,
            "incoming_path": self.incoming.get("path"),
            "incumbent_path": self.incumbent.get("path"),
            "output_path": (output or {}).get("path"),
            "table": self.table(output),
        }

    def __repr__(self):
        return "<Comparison %s gate=%s>" % (self.verdict, self.gate)


def _decide(gate, reason, new, old, incoming, incumbent, notes):
    log.debug("gate %d decided: incoming %s versus incumbent %s", gate, new, old)
    if new > old:
        result = Comparison(WIN, gate, reason % "incoming", incoming, incumbent, notes)
    else:
        result = Comparison(LOSS, gate, reason % "incumbent", incoming, incumbent, notes)
    log.info("comparison %s at gate %d: %s", result.verdict, gate, result.reason)
    return result


def compare(incoming, incumbent):
    notes = []
    log.debug(
        "comparing %s against %s",
        incoming.get("path"), incumbent.get("path"),
    )

    speed = speed_mismatch(incoming, incumbent)
    if speed:
        notes.append(speed)
        log.info("comparison speed check: %s", speed)

    new_hv = incoming["dolby_vision"] or incoming["hdr"]
    old_hv = incumbent["dolby_vision"] or incumbent["hdr"]
    if new_hv != old_hv:
        return _decide(
            1,
            "only the %s carries HDR or Dolby Vision, losing it is never an upgrade",
            int(new_hv),
            int(old_hv),
            incoming,
            incumbent,
            notes,
        )

    new_px = incoming["display_pixels"]
    old_px = incumbent["display_pixels"]
    new_bars = incoming.get("letterbox_px") or 0
    old_bars = incumbent.get("letterbox_px") or 0
    if new_bars or old_bars:
        notes.append(
            "baked-in bars present, incoming %d px and incumbent %d px, "
            "gate 2 deferred to gate 3" % (new_bars, old_bars)
        )
        log.debug(
            "gate 2 deferred, bars incoming %d px, incumbent %d px", new_bars, old_bars
        )
    elif new_px and old_px:
        larger = max(new_px, old_px)
        if abs(new_px - old_px) / float(larger) > PIXEL_TOLERANCE:
            return _decide(
                2,
                "the %s has the larger display resolution",
                new_px,
                old_px,
                incoming,
                incumbent,
                notes,
            )
    else:
        notes.append("display pixel count unavailable on one side, gate 2 skipped")

    new_pic = incoming.get("picture_pixels")
    old_pic = incumbent.get("picture_pixels")
    if new_pic and old_pic:
        larger = max(new_pic, old_pic)
        if abs(new_pic - old_pic) / float(larger) > PIXEL_TOLERANCE:
            return _decide(
                3,
                "the %s has the larger real picture area once baked-in bars are discounted",
                new_pic,
                old_pic,
                incoming,
                incumbent,
                notes,
            )
    else:
        notes.append("cropdetect not run on both sides, gate 3 skipped")

    new_ch = incoming["audio_channels_max"]
    old_ch = incumbent["audio_channels_max"]
    if new_ch != old_ch and new_ch and old_ch:
        return _decide(
            4,
            "the %s has the higher audio channel count",
            new_ch,
            old_ch,
            incoming,
            incumbent,
            notes,
        )

    new_depth = incoming["bit_depth"]
    old_depth = incumbent["bit_depth"]
    if new_depth != old_depth:
        return _decide(
            5,
            "the %s has the greater bit depth",
            new_depth,
            old_depth,
            incoming,
            incumbent,
            notes,
        )

    new_rank = _pedigree_rank(incoming.get("pedigree"))
    old_rank = _pedigree_rank(incumbent.get("pedigree"))
    if new_rank >= 0 and old_rank >= 0 and new_rank != old_rank:
        notes.append("decided on release naming, which is a weak signal")
        return _decide(
            6,
            "the %s has the better source pedigree",
            new_rank,
            old_rank,
            incoming,
            incumbent,
            notes,
        )

    log.info("comparison ambiguous, no gate produced a clear difference")
    return Comparison(
        AMBIGUOUS,
        None,
        "no gate produced a clear difference, a human decision is required",
        incoming,
        incumbent,
        notes,
    )
