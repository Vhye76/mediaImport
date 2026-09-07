import difflib
import logging
import os
import re

from . import titles

log = logging.getLogger("episodes")

FUZZY_CUTOFF = 0.82

RANGE_PATTERNS = (
    re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})[\s._-]*e(\d{1,3})", re.I),
    re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})\s*[-+&]\s*e?(\d{1,3})", re.I),
    re.compile(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})\s*[-+&]\s*(?:\d{1,2}x)?(\d{1,3})", re.I),
)

SINGLE_PATTERNS = (
    re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})", re.I),
    re.compile(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})", re.I),
    re.compile(r"(?:^|[^a-z0-9])season[\s._-]*(\d{1,2})[\s._-]*episode[\s._-]*(\d{1,3})", re.I),
)

PART_MARKERS = (
    re.compile(r"\s*\(\s*part\s+(one|two|three|four|five|six)\s*\)\s*$", re.I),
    re.compile(r"\s*\(\s*part\s+(\d+)\s*\)\s*$", re.I),
    re.compile(r"\s*\(\s*(\d+)\s*\)\s*$", re.I),
)

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
}

MOVIE_YEAR = re.compile(r"(?:^|[^0-9])(19\d{2}|20\d{2})(?:[^0-9]|$)")


def classify(path):
    name = os.path.basename(str(path))
    parent = os.path.basename(os.path.dirname(str(path)))
    for candidate in (name, parent):
        if parse_filename(candidate) is not None:
            return "tv"
    return "movie"


def parse_filename(name, default_season=None):
    text = str(name)
    for pattern in RANGE_PATTERNS:
        m = pattern.search(text)
        if m:
            season, first, last = (int(g) for g in m.groups())
            if last >= first:
                return {"season": season, "first": first, "last": last}
    for pattern in SINGLE_PATTERNS:
        m = pattern.search(text)
        if m:
            season, first = (int(g) for g in m.groups())
            return {"season": season, "first": first, "last": first}
    if default_season is not None:
        m = re.search(r"(?:^|[^0-9])e(\d{1,3})(?:[^0-9]|$)", text, re.I)
        if m:
            episode = int(m.group(1))
            return {"season": default_season, "first": episode, "last": episode}
    return None


def parse_marker(title):
    text = str(title)
    for pattern in PART_MARKERS:
        m = pattern.search(text)
        if m:
            token = m.group(1).lower()
            number = WORD_NUMBERS.get(token)
            if number is None:
                try:
                    number = int(token)
                except ValueError:
                    continue
            return pattern.sub("", text).strip(), number
    return text, None


def to_part_suffix(title):
    base, number = parse_marker(title)
    if number is None:
        return str(title)
    return "%s, Part %d" % (base, number)


def title_from_filename(name):
    stem = os.path.splitext(os.path.basename(str(name)))[0]
    parts = stem.split(" - ")
    if len(parts) >= 3:
        return " - ".join(parts[2:]).strip()
    return stem


def _index(catalogue):
    exact = {}
    base = {}
    keys = []
    for entry in catalogue:
        key = titles.normalise_for_match(entry["title"])
        exact.setdefault(key, entry)
        keys.append(key)
        stripped = titles.normalise_for_match(parse_marker(entry["title"])[0])
        current = base.get(stripped)
        if current is None or _order(entry) < _order(current):
            base[stripped] = entry
    return exact, base, keys


def _order(entry):
    return (entry["season"], entry["episode"])


def match_episode(name, catalogue, cutoff=FUZZY_CUTOFF):
    entry, method, score = _match_episode(name, catalogue, cutoff)
    if method == "fuzzy":
        log.info("episode matched by fuzzy title on %s at %.2f", os.path.basename(str(name)), score)
    elif method == "none":
        log.info("no title match for %s", os.path.basename(str(name)))
    else:
        log.debug("episode matched exactly on %s", os.path.basename(str(name)))
    return entry, method, score


def _match_episode(name, catalogue, cutoff=FUZZY_CUTOFF):
    exact, base, keys = _index(catalogue)
    probe_title = title_from_filename(name)
    key = titles.normalise_for_match(probe_title)
    stripped = titles.normalise_for_match(parse_marker(probe_title)[0])

    if key in exact:
        return exact[key], "exact", 1.0
    if key in base:
        return base[key], "exact", 1.0
    if stripped in exact:
        return exact[stripped], "exact", 1.0
    if stripped in base:
        return base[stripped], "exact", 1.0

    close = difflib.get_close_matches(key, keys, n=3, cutoff=cutoff)
    log.debug("fuzzy candidates for %r: %s", key, close)
    if close:
        return exact[close[0]], "fuzzy", difflib.SequenceMatcher(None, key, close[0]).ratio()

    return None, "none", 0.0


def assign(files, catalogue):
    results = []
    claimed = set()

    for path in files:
        entry, how, score = match_episode(path, catalogue)
        if entry is not None:
            key = (entry["season"], entry["episode"])
            if key not in claimed:
                claimed.add(key)
                results.append(
                    {
                        "path": path,
                        "season": entry["season"],
                        "first": entry["episode"],
                        "last": entry["episode"],
                        "title": to_part_suffix(entry["title"]),
                        "method": how,
                        "score": score,
                    }
                )
                continue

        parsed = parse_filename(os.path.basename(str(path)))
        if parsed is None:
            results.append({"path": path, "method": "unmatched", "score": 0.0})
            continue
        results.append(
            {
                "path": path,
                "season": parsed["season"],
                "first": parsed["first"],
                "last": parsed["last"],
                "title": None,
                "method": "fallback-numbering",
                "score": 0.0,
            }
        )

    return results


def extend_ranges(assignments, catalogue):
    by_key = {(e["season"], e["episode"]): e for e in catalogue}
    claimed = {
        (a["season"], n)
        for a in assignments
        if a.get("season") is not None
        for n in range(a["first"], a.get("last", a["first"]) + 1)
    }
    for a in assignments:
        if a.get("season") is None or a["method"] == "unmatched":
            continue
        nxt = (a["season"], a["last"] + 1)
        if nxt in claimed or nxt not in by_key:
            continue
        this_base = parse_marker(by_key.get((a["season"], a["first"]), {}).get("title", ""))[0]
        next_base = parse_marker(by_key[nxt]["title"])[0]
        if this_base and this_base == next_base:
            a["last"] = nxt[1]
            a["title"] = this_base
            claimed.add(nxt)
    return assignments


def season_counts(catalogue):
    counts = {}
    for entry in catalogue:
        counts[entry["season"]] = counts.get(entry["season"], 0) + 1
    return counts


def disk_counts(assignments):
    counts = {}
    for a in assignments:
        season = a.get("season")
        if season is None:
            continue
        span = a.get("last", a.get("first")) - a.get("first") + 1
        counts[season] = counts.get(season, 0) + span
    return counts
