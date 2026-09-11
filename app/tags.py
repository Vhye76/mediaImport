import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import probe as probemod

log = logging.getLogger("tags")

MKVEXTRACT = os.environ.get("MKVEXTRACT", "mkvextract")
MKVPROPEDIT = os.environ.get("MKVPROPEDIT", "mkvpropedit")

CANONICAL_MOVIE = ("TITLE", "TMDB", "IMDB", "DATE_RELEASED")
DROPPABLE = {
    "ENCODER", "COMMENT", "MAJOR_BRAND", "MINOR_VERSION", "COMPATIBLE_BRANDS",
    "HANDLER_NAME", "VENDOR_ID", "CREATION_TIME", "SOFTWARE",
}
STAT_PREFIXES = ("BPS", "DURATION", "NUMBER_OF_", "_STATISTICS")

COLLECTION = "COLLECTION"
SEASON = "SEASON"
EPISODE = "EPISODE"
MOVIE = "MOVIE"

KEEP_LANGS = ("eng", "en", "und")
STATS_RATIO_FLOOR = 0.5
COLOUR_TAGS = ("color_primaries", "color_transfer", "color_space")


class TagError(RuntimeError):
    pass


def _run(cmd, timeout=3600):
    log.debug("running %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


#----- Reading
def read_tags(path):
    proc = _run([MKVEXTRACT, str(path), "tags", "-"])
    if proc.returncode != 0:
        raise TagError(
            "mkvextract failed rc=%d: %s"
            % (proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[-400:])
        )
    body = (proc.stdout or "").strip()
    if not body:
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise TagError("tag XML did not parse: %s" % exc)


#----- Tag structure
def _target_type(tag):
    targets = tag.find("Targets")
    if targets is None:
        return ""
    return (targets.findtext("TargetType") or "").strip().upper()


def _is_track_targeted(tag):
    targets = tag.find("Targets")
    return targets is not None and targets.findtext("TrackUID") is not None


def _simples(tag):
    return {
        (s.findtext("Name") or "").strip(): (s.findtext("String") or "")
        for s in tag.findall("Simple")
    }


def slash_named_simples(root):
    if root is None:
        return []
    found = []
    for tag in root.findall("Tag"):
        for simple in tag.findall("Simple"):
            name = (simple.findtext("Name") or "").strip()
            if "/" in name:
                found.append(name)
    return found


def target_types_present(root):
    if root is None:
        return set()
    return {_target_type(tag) for tag in root.findall("Tag") if _target_type(tag)}


def is_flattened(root):
    return bool(slash_named_simples(root))


def carry_forward(root):
    log.debug("carrying forward untargeted scraped keys")
    carry = {}
    if root is None:
        return carry
    for tag in root.findall("Tag"):
        if _target_type(tag) or _is_track_targeted(tag):
            continue
        for name, value in _simples(tag).items():
            upper = name.upper()
            if not upper or "/" in upper:
                continue
            if upper in CANONICAL_MOVIE or upper in DROPPABLE:
                continue
            if upper.startswith(STAT_PREFIXES):
                continue
            carry[name] = value
    return carry


#----- XML construction
def _simple_xml(name, value):
    return "    <Simple><Name>%s</Name><String>%s</String></Simple>" % (
        escape(str(name)),
        escape(str(value)),
    )


def _tag_xml(target_type, target_value, pairs):
    lines = [
        "  <Tag>",
        "    <Targets>",
        "      <TargetTypeValue>%d</TargetTypeValue>" % target_value,
        "      <TargetType>%s</TargetType>" % target_type,
        "    </Targets>",
    ]
    lines += [_simple_xml(k, v) for k, v in pairs if v is not None and v != ""]
    lines.append("  </Tag>")
    return "\n".join(lines)


def build_movie_xml(title, year, tmdb, imdb, carry=None):
    pairs = [("TITLE", title), ("TMDB", tmdb), ("IMDB", imdb), ("DATE_RELEASED", year)]
    pairs += sorted((carry or {}).items())
    return '<?xml version="1.0"?>\n<Tags>\n%s\n</Tags>\n' % _tag_xml(MOVIE, 50, pairs)


def build_unidentified_xml(kind, title, carry=None):
    target = MOVIE if kind == "movie" else EPISODE
    pairs = [("TITLE", title)] + sorted((carry or {}).items())
    return '<?xml version="1.0"?>\n<Tags>\n%s\n</Tags>\n' % _tag_xml(target, 50, pairs)


def build_tv_xml(show, tvdb, tmdb, season, episode_title, episode_number, carry=None):
    blocks = [
        _tag_xml(COLLECTION, 70, [("TITLE", show), ("TVDB", tvdb), ("TMDB", tmdb)]),
        _tag_xml(SEASON, 60, [("TITLE", "Season %d" % int(season)), ("PART_NUMBER", int(season))]),
        _tag_xml(
            EPISODE,
            50,
            [("TITLE", episode_title), ("PART_NUMBER", int(episode_number))]
            + sorted((carry or {}).items()),
        ),
    ]
    return '<?xml version="1.0"?>\n<Tags>\n%s\n</Tags>\n' % "\n".join(blocks)


#----- Writing
def write_tags(path, xml, segment_title=None, add_stats=True):
    handle, xml_path = tempfile.mkstemp(suffix=".xml", prefix="mediaimport-tags-")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(xml)
        #----- global: replaces untargeted tags only;  all: would take per-track statistics with it.
        args = [MKVPROPEDIT, str(path), "--tags", "global:%s" % xml_path]
        if segment_title is not None:
            args += ["--edit", "info", "--set", "title=%s" % segment_title]
        proc = _run(args)
        log.debug("mkvpropedit tag write exit %d", proc.returncode)
        if proc.returncode != 0:
            raise TagError(
                "mkvpropedit tag write failed rc=%d: %s"
                % (proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[-400:])
            )
        log.info(
            "tags written to %s%s",
            os.path.basename(str(path)),
            ", segment title set" if segment_title is not None else "",
        )
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass

    if add_stats:
        return refresh_statistics(path)
    return None


#----- Track statistics
def add_track_statistics(path):
    proc = _run([MKVPROPEDIT, str(path), "--add-track-statistics-tags"])
    log.debug("add-track-statistics-tags exit %d", proc.returncode)
    if proc.returncode != 0:
        raise TagError(
            "adding track statistics failed rc=%d: %s"
            % (proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[-400:])
        )


def byte_sum_ratio(path):
    data = probemod.ffprobe_json(path, ["-show_streams"])
    total = 0
    for stream in data.get("streams") or []:
        for key, value in (stream.get("tags") or {}).items():
            if key.upper().startswith("NUMBER_OF_BYTES"):
                try:
                    total += int(value)
                except (TypeError, ValueError):
                    pass
                break
    try:
        size = os.path.getsize(path)
    except OSError:
        return -1.0
    return (total / float(size)) if size else -1.0


def refresh_statistics(path):
    add_track_statistics(path)
    ratio = byte_sum_ratio(path)
    log.debug("byte-sum ratio after first statistics pass: %.4f", ratio)
    if ratio <= 0.01:
        log.info("statistics came back zero, re-running add-track-statistics-tags")
        add_track_statistics(path)
        ratio = byte_sum_ratio(path)
        log.debug("byte-sum ratio after re-run: %.4f", ratio)
    log.info("track statistics refreshed, byte-sum ratio %.4f", ratio)
    return ratio


#----- Identity from tags
def movie_identity(path):
    try:
        root = read_tags(path)
    except (TagError, OSError):
        return None
    if root is None:
        return None
    for tag in root.findall("Tag"):
        if _target_type(tag) != MOVIE:
            continue
        simples = _simples(tag)
        released = simples.get("DATE_RELEASED") or ""
        m = re.search(r"(\d{4})", released)
        found = {
            "title": (simples.get("TITLE") or "").strip() or None,
            "tmdb": (simples.get("TMDB") or "").strip() or None,
            "imdb": (simples.get("IMDB") or "").strip() or None,
            "year": int(m.group(1)) if m else None,
        }
        if any(found.values()):
            log.info("read identity from the embedded MOVIE tag block")
            log.debug("embedded identity: %s", found)
            return found
    return None


#----- The readiness gate
def check_movie(path, expected_title, required=CANONICAL_MOVIE):
    problems = []
    root = read_tags(path)
    if root is None:
        return ["no tags at all"]

    for name in slash_named_simples(root):
        problems.append("flattened tag block, Simple Name contains a slash: %s" % name)

    movie = None
    for tag in root.findall("Tag"):
        if _target_type(tag) == MOVIE:
            movie = _simples(tag)
            break

    if movie is None:
        problems.append("no MOVIE-targeted tag")
    else:
        for key in required:
            if not movie.get(key):
                problems.append("MOVIE tag missing %s" % key)
        if movie.get("TITLE") and movie["TITLE"] != expected_title:
            problems.append(
                "MOVIE TITLE is %r, expected %r" % (movie["TITLE"], expected_title)
            )
    return problems


def check_tv(path, expected_show, expected_episode_title, levels=(COLLECTION, SEASON, EPISODE)):
    problems = []
    root = read_tags(path)
    if root is None:
        return ["no tags at all"]

    for name in slash_named_simples(root):
        problems.append("flattened tag block, Simple Name contains a slash: %s" % name)

    present = target_types_present(root)
    for level in levels:
        if level not in present:
            problems.append("missing a %s-targeted tag" % level)

    levels = {}
    for tag in root.findall("Tag"):
        target = _target_type(tag)
        if target in (COLLECTION, SEASON, EPISODE):
            levels[target] = _simples(tag)

    collection = levels.get(COLLECTION, {})
    episode = levels.get(EPISODE, {})

    if collection.get("TITLE") and collection["TITLE"] != expected_show:
        problems.append(
            "COLLECTION TITLE is %r, expected the show name %r"
            % (collection["TITLE"], expected_show)
        )
    if episode.get("TITLE") and episode["TITLE"] != expected_episode_title:
        problems.append(
            "EPISODE TITLE is %r, expected %r" % (episode["TITLE"], expected_episode_title)
        )
    return problems


def check_tracks(path):
    problems = []
    rows, data = probemod.track_selectors(path)

    audio_defaults = sum(1 for r in rows if r["type"] == "audio" and r["default"])
    if audio_defaults != 1:
        problems.append("audio default count is %d, must be exactly 1" % audio_defaults)

    sub_defaults = sum(
        1 for r in rows if r["type"] == "subtitles" and r["default"] and not r["forced"]
    )
    if sub_defaults:
        problems.append("%d non-forced subtitle track(s) marked default" % sub_defaults)

    for row in rows:
        lang = row["language"]
        if row["type"] in ("audio", "subtitles") and lang not in KEEP_LANGS:
            problems.append("foreign %s track tagged %s" % (row["type"], lang))
        if row["type"] == "video" and "V_MJPEG" not in (row["codec_id"] or "").upper():
            if lang != "eng":
                problems.append("video track language is %s, must be eng" % lang)

    return problems


#----- HDR declaration check
def _declared(video, name):
    if name == "dolby_vision":
        return bool(video.get("dolby_vision"))
    return video.get(name) is not None


def _carried(video, name):
    in_bitstream = video.get(probemod.BITSTREAM_KEY[name])
    if name == "dolby_vision":
        return bool(video.get("dolby_vision") or in_bitstream)
    return video.get(name) is not None or in_bitstream is not None


def check_hdr(path, baseline=None):
    problems = []
    video = probemod.probe(path).video
    if not video.get("hdr") and not (baseline or {}).get("hdr"):
        return problems

    gap = video.get("hdr_declaration_gap") or []
    if gap:
        problems.append(
            "hdr declaration short of the bitstream: container lacks %s" % ", ".join(gap)
        )

    #----- loss against the baseline is a hold;  absence on both surfaces is not.
    if not baseline:
        return problems

    for tag in COLOUR_TAGS:
        before = (baseline.get(tag) or "").lower()
        after = (video.get(tag) or "").lower()
        if before and before != after:
            problems.append("%s was %s at probe and is now %s" % (tag, before, after or "unset"))

    for name in probemod.HDR_DECLARATIONS:
        if _carried(baseline, name) and not _declared(video, name):
            problems.append("%s carried by the source is not declared by the output" % name)
    return problems


def check_segment_title(path, expected):
    _, data = probemod.track_selectors(path)
    actual = ((data.get("container") or {}).get("properties") or {}).get("title")
    if actual != expected:
        return ["segment title is %r, expected %r" % (actual, expected)]
    return []


def readiness(path, kind, expected_title, show=None, hdr_baseline=None, unidentified=False):
    problems = []
    if not os.path.isfile(path):
        return False, ["output file is missing"]

    try:
        if kind == "movie":
            problems += check_movie(
                path, expected_title, required=("TITLE",) if unidentified else CANONICAL_MOVIE
            )
        else:
            problems += check_tv(
                path, show, expected_title,
                levels=(EPISODE,) if unidentified else (COLLECTION, SEASON, EPISODE),
            )
        problems += check_segment_title(path, expected_title)
        problems += check_tracks(path)
        problems += check_hdr(path, baseline=hdr_baseline)
    except (TagError, probemod.ProbeError) as exc:
        return False, problems + [str(exc)]

    ratio = byte_sum_ratio(path)
    if ratio <= STATS_RATIO_FLOOR:
        problems.append("track statistics missing or stale, byte-sum ratio %.4f" % ratio)

    return (not problems), problems
