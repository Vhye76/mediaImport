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


def attributes(container, path=None):
    video = container.get("video") or {}
    return {
        "path": str(path) if path else None,
        "codec": video.get("codec"),
        "hdr": bool(video.get("hdr")),
        "dolby_vision": bool(video.get("dolby_vision")),
        "display_width": int(video.get("display_width") or 0),
        "display_height": int(video.get("display_height") or 0),
        "display_pixels": int(video.get("display_pixels") or 0),
        "bit_depth": int(video.get("bit_depth") or 8),
        "audio_channels_max": int(container.get("audio_channels_max") or 0),
        "picture_pixels": None,
        "pedigree": pedigree(path) if path else None,
        "size_bytes": int(container.get("size_bytes") or 0),
    }


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

    def table(self):
        rows = []
        for key in (
            "codec",
            "dolby_vision",
            "hdr",
            "display_width",
            "display_height",
            "display_pixels",
            "picture_pixels",
            "bit_depth",
            "audio_channels_max",
            "pedigree",
            "size_bytes",
        ):
            rows.append(
                {
                    "attribute": key,
                    "incoming": self.incoming.get(key),
                    "incumbent": self.incumbent.get(key),
                }
            )
        return rows

    def as_dict(self):
        return {
            "verdict": self.verdict,
            "gate": self.gate,
            "reason": self.reason,
            "notes": self.notes,
            "table": self.table(),
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
    if new_px and old_px:
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
