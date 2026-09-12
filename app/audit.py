import logging
import os
import threading
import time

import re

from . import compare, episodes, probe as probemod, tags, titles

log = logging.getLogger("audit")

REPAIR_REMUX = "remux"
REPAIR_STRIP = "language strip"
REPAIR_FLAGS = "flag repair"
REPAIR_TAGS = "tag rewrite"
REPAIR_SEGMENT = "segment title"
REPAIR_STATS = "statistics refresh"
REPAIR_HDR = "hdr declaration"
REPAIR_NAMING = "republish"
REPAIR_NONE = None

TAG_INCOMPLETE = "tag incomplete"

HDR_ROWS = (
    ("mastering_display", "mastering display"),
    ("content_light", "content light level"),
    ("dolby_vision", "Dolby Vision record"),
)


#----- Rows of the detail table
def _row(check, expected, actual, ok, repair, surface=None):
    return {
        "check": check,
        "expected": expected,
        "actual": actual,
        "ok": bool(ok),
        "repair": repair if not ok else None,
        "surface": surface,
    }


def _stem(path):
    return os.path.splitext(os.path.basename(str(path)))[0]


def _text(simples, name):
    return (simples.get(name) or "").strip() or None


def _number(simples, name):
    value = _text(simples, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _year_of(value):
    match = re.search(r"(\d{4})", value or "")
    return int(match.group(1)) if match else None


#----- Reading the identity out of the tag block
def _tag_identity(root, kind):
    found = {
        "title": None, "show": None, "present": set(), "flattened": [],
        "tmdb": None, "imdb": None, "tvdb": None, "year": None,
        "season": None, "episode": None,
    }
    if root is None:
        return found
    found["flattened"] = tags.slash_named_simples(root)
    found["present"] = tags.target_types_present(root)
    for tag in root.findall("Tag"):
        target = tags._target_type(tag)
        simples = tags._simples(tag)
        if kind == "movie" and target == tags.MOVIE:
            found["title"] = _text(simples, "TITLE")
            found["tmdb"] = _text(simples, "TMDB")
            found["imdb"] = _text(simples, "IMDB")
            found["year"] = _year_of(_text(simples, "DATE_RELEASED"))
        elif kind == "tv" and target == tags.EPISODE:
            found["title"] = _text(simples, "TITLE")
            found["episode"] = _number(simples, "PART_NUMBER")
        elif kind == "tv" and target == tags.SEASON:
            found["season"] = _number(simples, "PART_NUMBER")
        elif kind == "tv" and target == tags.COLLECTION:
            found["show"] = _text(simples, "TITLE")
            found["tvdb"] = _text(simples, "TVDB")
            found["tmdb"] = _text(simples, "TMDB")
    return found


#----- Naming rows, built by the same functions the publish step uses
def _build(fn, *args):
    try:
        return fn(*args)
    except titles.TitleError as exc:
        return "unbuildable (%s)" % exc


def _naming_row(check, expected, actual):
    return _row(check, expected, actual, expected == actual, REPAIR_NAMING)


def _movie_naming_rows(path, found):
    name = os.path.basename(path)
    stem = _stem(path)
    parent = os.path.basename(os.path.dirname(path))
    if not all(found.get(k) for k in ("title", "year", "tmdb", "imdb")):
        return [
            _row("folder name", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
            _row("file name", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
        ]
    folder = _build(titles.movie_folder, found["title"], found["year"], found["tmdb"], found["imdb"])
    rows = [_naming_row("folder name", folder, parent)]
    plain = _build(titles.movie_filename, found["title"], found["year"])
    edition_prefix = folder + " - "
    if stem.startswith(edition_prefix):
        label = stem[len(edition_prefix):]
        rows.append(_naming_row("file name", _build(titles.movie_filename, found["title"], found["year"], label, folder), name))
    elif stem.startswith(parent + " - "):
        label = stem[len(parent) + 3:]
        rows.append(_naming_row("file name", _build(titles.movie_filename, found["title"], found["year"], label, folder), name))
    else:
        rows.append(_naming_row("file name", plain, name))
    return rows


def _tv_naming_rows(path, found):
    name = os.path.basename(path)
    stem = _stem(path)
    season_dir = os.path.basename(os.path.dirname(path))
    show_dir = os.path.basename(os.path.dirname(os.path.dirname(path)))
    complete = (
        found.get("show") and found.get("tvdb") and found.get("tmdb")
        and found.get("season") is not None and found.get("episode") is not None
        and found.get("title")
    )
    if not complete:
        return [
            _row("show folder", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
            _row("season folder", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
            _row("file name", "(from tag)", TAG_INCOMPLETE, False, REPAIR_NAMING),
        ]
    #----- the COLLECTION block carries no year, so the folder's own is used and everything else is checked.
    years = titles.YEAR_IN_PARENS.findall(show_dir)
    show_year = years[-1] if years else "YYYY"
    show = _build(titles.show_folder, found["show"], show_year, found["tvdb"], found["tmdb"])
    season = titles.season_folder(found["season"])
    parsed = episodes.parse_filename(stem)
    last = None
    if parsed and parsed["season"] == found["season"] and parsed["first"] == found["episode"]:
        last = parsed["last"]
    file_expected = _build(
        titles.episode_filename, found["show"], found["season"], found["episode"], found["title"], last,
    )
    return [
        _naming_row("show folder", show, show_dir),
        _naming_row("season folder", season, season_dir),
        _naming_row("file name", file_expected, name),
    ]


def _component_rows(path, kind):
    components = [os.path.basename(path), os.path.basename(os.path.dirname(path))]
    if kind == "tv":
        components.append(os.path.basename(os.path.dirname(os.path.dirname(path))))
    problems = []
    for component in components:
        for problem in titles.validate_component(component):
            problems.append("%s: %s" % (component, problem))
    return [_row("component rules", "none", "; ".join(problems) or "none", not problems, REPAIR_NAMING)]


def _hdr_rows(video):
    rows = []
    gap = set(video.get("hdr_declaration_gap") or [])
    for tag in ("color_primaries", "color_transfer", "color_space"):
        rows.append(_row(tag.replace("_", " "), video.get(tag), video.get(tag), True, REPAIR_NONE, "container"))
    for key, label in HDR_ROWS:
        if key == "dolby_vision":
            declared = "present" if video.get("dolby_vision") else "absent"
            carried = "present" if (video.get("dolby_vision") or video.get("dv_in_bitstream")) else "absent"
            repair = REPAIR_NONE
        else:
            declared = compare._mastering_summary(video.get(key)) if key == "mastering_display" \
                else compare._content_light_summary(video.get(key))
            bitstream = video.get(key + "_bitstream")
            carried = compare._mastering_summary(bitstream) if key == "mastering_display" \
                else compare._content_light_summary(bitstream)
            repair = REPAIR_HDR
        rows.append(_row(label, carried or "absent", declared or "absent", key not in gap, repair, "bitstream"))
    return rows


#----- Assessing one library file
def assess(path, kind):
    container = probemod.probe(path).container
    video = container.get("video") or {}
    rows = []

    fmt = (container.get("format_name") or "").lower()
    rows.append(_row("container", "matroska", fmt or "unknown", "matroska" in fmt, REPAIR_REMUX))

    foreign = container.get("foreign_tracks") or []
    rows.append(_row("foreign tracks", "none", ", ".join(foreign) or "none", not foreign, REPAIR_STRIP))

    selectors, _ = probemod.track_selectors(path)
    audio_defaults = int(container.get("audio_default_count") or 0)
    rows.append(_row("audio default count", 1, audio_defaults, audio_defaults == 1, REPAIR_FLAGS))
    sub_defaults = int(container.get("subtitle_default_count") or 0)
    rows.append(_row("subtitle defaults", 0, sub_defaults, sub_defaults == 0, REPAIR_FLAGS))
    video_lang = next(
        (r["language"] for r in selectors
         if r["type"] == "video" and "V_MJPEG" not in (r["codec_id"] or "").upper()),
        "und",
    )
    rows.append(_row("video language", "eng", video_lang, video_lang == "eng", REPAIR_FLAGS))

    root = tags.read_tags(path)
    found = _tag_identity(root, kind)
    if kind == "movie":
        wanted = {tags.MOVIE}
        expected_structure = "MOVIE target"
    else:
        wanted = {tags.COLLECTION, tags.SEASON, tags.EPISODE}
        expected_structure = "COLLECTION, SEASON and EPISODE targets"
    present = sorted(t for t in found["present"] if t in wanted)
    structure_ok = wanted <= found["present"] and not found["flattened"]
    actual_structure = ", ".join(present) if present else "none"
    if found["flattened"]:
        actual_structure += ", flattened"
    rows.append(_row("tag structure", expected_structure, actual_structure, structure_ok, REPAIR_TAGS))

    if kind == "movie":
        rows += _movie_naming_rows(path, found)
    else:
        rows += _tv_naming_rows(path, found)
    rows += _component_rows(path, kind)

    tag_title = found["title"]
    segment = container.get("segment_title")
    rows.append(_row(
        "segment title", tag_title or "(tag TITLE)", segment or "unset",
        bool(tag_title) and segment == tag_title, REPAIR_SEGMENT,
    ))

    ratio = round(tags.byte_sum_ratio(path), 4)
    rows.append(_row(
        "statistics ratio", "> %s" % tags.STATS_RATIO_FLOOR, ratio,
        ratio > tags.STATS_RATIO_FLOOR, REPAIR_STATS,
    ))

    if video.get("hdr"):
        rows += _hdr_rows(video)

    failed = [r["check"] for r in rows if not r["ok"]]
    measured = {
        "rows": rows,
        "size_bytes": int(container.get("size_bytes") or 0),
        "hdr_format": video.get("hdr_format"),
        "codec": video.get("codec"),
    }
    summary = "; ".join(
        "%s: %s" % (r["check"], r["actual"]) for r in rows if not r["ok"]
    ) or "meets the standard"
    return failed, measured, summary


#----- The sweep
class Auditor:
    def __init__(self, cfg, layout, store, stop_event):
        self.cfg = cfg
        self.layout = layout
        self.store = store
        self.stop_event = stop_event
        self.thread = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._restart = threading.Event()
        self._status = {
            "running": False,
            "done": 0,
            "total": 0,
            "started_at": None,
            "finished_at": None,
            "next_at": None,
            "current": None,
        }

    def start(self):
        if not self.cfg.audit_enabled:
            log.info("library audit disabled, AUDIT_INTERVAL=%d", self.cfg.audit_interval)
            return
        self.thread = threading.Thread(target=self._run, name="auditor", daemon=True)
        self.thread.start()
        log.info(
            "library audit started, %ds between files, passes %ds apart",
            self.cfg.audit_interval, self.cfg.audit_sweep_interval,
        )

    def sweep_now(self):
        self._wake.set()

    def rescan(self):
        removed = self.store.audit_forget_all()
        log.info("library audit findings wiped, %d row(s), full rescan requested", removed)
        self._restart.set()
        self._wake.set()

    def status(self):
        with self._lock:
            snapshot = dict(self._status)
        snapshot.update(self.store.audit_totals())
        snapshot["enabled"] = bool(self.cfg.audit_enabled)
        return snapshot

    def _set(self, **fields):
        with self._lock:
            self._status.update(fields)

    def _run(self):
        while not self.stop_event.is_set():
            self._wake.clear()
            try:
                self._sweep()
            except Exception:
                log.exception("library audit pass failed")
            next_at = time.time() + self.cfg.audit_sweep_interval
            self._set(next_at=next_at)
            while not self.stop_event.is_set() and time.time() < next_at:
                if self._wake.wait(timeout=5):
                    break

    def _files(self):
        for kind, root in sorted(self.layout.libraries.items()):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    if not name.lower().endswith(probemod.VIDEO_EXTENSIONS):
                        continue
                    yield kind, os.path.join(dirpath, name)

    def _sweep(self):
        self._restart.clear()
        files = list(self._files())
        self._set(running=True, done=0, total=len(files), started_at=time.time(), finished_at=None)
        log.info("library audit pass over %d file(s)", len(files))
        seen = []
        assessed = 0
        for kind, path in files:
            if self.stop_event.is_set() or self._restart.is_set():
                break
            seen.append(path)
            try:
                st = os.stat(path)
            except OSError as exc:
                log.debug("audit skipped %s: %s", path, exc)
                self._set(done=len(seen))
                continue
            previous = self.store.audit_seen(path)
            if previous == (st.st_size, st.st_mtime):
                self._set(done=len(seen))
                continue
            self._set(current=os.path.basename(path))
            try:
                failed, measured, summary = assess(path, kind)
            except Exception as exc:
                log.warning("audit could not assess %s: %s", os.path.basename(path), exc)
                failed, measured, summary = ["unreadable"], {"rows": []}, str(exc)
            self.store.audit_record(path, kind, st.st_size, st.st_mtime, failed, measured, summary)
            assessed += 1
            if failed:
                log.info("audit finding on %s: %s", os.path.basename(path), summary)
            self._set(done=len(seen), current=None)
            if self.stop_event.wait(self.cfg.audit_interval):
                break
        #----- an interrupted pass has not seen every file, so it must not prune the ones it missed.
        interrupted = self.stop_event.is_set() or self._restart.is_set()
        removed = self.store.audit_forget_missing(seen) if not interrupted else 0
        self._set(running=False, finished_at=time.time(), current=None)
        totals = self.store.audit_totals()
        log.info(
            "library audit pass finished: %d assessed, %d unchanged, %d forgotten, %d finding(s)",
            assessed, len(seen) - assessed, removed, totals["findings"],
        )
