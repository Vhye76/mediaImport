import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION
from . import episodes as episodemod
from . import titles

log = logging.getLogger("provider")

USER_AGENT = "mediaimport/%s (+https://github.com/Vhye76/mediaImport)" % VERSION
THROTTLE_SECONDS = 3.0
TIMEOUT = 30

P_TMDB = "P4947"
P_IMDB = "P345"
P_TVDB = "P4835"
P_TVDB_SERIES = "P4983"
P_RELEASE = "P577"
P_START = "P580"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/%s.json"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
TMDB_MOVIE = "https://www.themoviedb.org/movie/%s"
TMDB_TV = "https://www.themoviedb.org/tv/%s"
TVDB_DEREFERRER = "https://thetvdb.com/dereferrer/series/%s"
TVDB_SEASONS = "https://thetvdb.com/series/%s/allseasons/%s"

EPISODE_LABEL = re.compile(
    r'episode-label">S(\d{1,2})E(\d{1,3})</span>.*?<a[^>]*>\s*(.*?)\s*</a>',
    re.I | re.S,
)


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    pass


class Client:
    def __init__(self, cache_dir, throttle=THROTTLE_SECONDS):
        self.cache_dir = str(cache_dir)
        self.throttle = throttle
        self._last = 0.0
        os.makedirs(self.cache_dir, exist_ok=True)

    def _cache_path(self, url):
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _cache_get(self, url):
        path = self._cache_path(url)
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _cache_put(self, url, status, body):
        try:
            with open(self._cache_path(url), "w") as fh:
                json.dump({"url": url, "status": status, "body": body}, fh)
        except OSError:
            pass

    def _wait(self):
        elapsed = time.time() - self._last
        if elapsed < self.throttle:
            log.debug("throttling %.1fs before the next request", self.throttle - elapsed)
            time.sleep(self.throttle - elapsed)
        self._last = time.time()

    def fetch(self, url, use_cache=True):
        if use_cache:
            cached = self._cache_get(url)
            if cached is not None:
                log.debug("cache hit for %s", url)
                return cached["status"], cached["body"]

        self._wait()
        log.debug("GET %s", url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                status = response.getcode()
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if status == 429:
                raise RateLimited("rate limited by %s" % url)
            if status >= 500:
                raise ProviderError("%s returned HTTP %d" % (url, status))
        except urllib.error.URLError as exc:
            raise ProviderError("could not reach %s: %s" % (url, exc))

        log.debug("HTTP %s from %s", status, url)
        if status == 200:
            self._cache_put(url, status, body)
        return status, body

    def fetch_json(self, url):
        status, body = self.fetch(url)
        if status != 200:
            raise ProviderError("%s returned HTTP %d" % (url, status))
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ProviderError("%s did not return JSON: %s" % (url, exc))

    def final_url(self, url):
        self._wait()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.geturl()


def _claims(entity, prop):
    values = []
    for claim in (entity.get("claims") or {}).get(prop, []):
        snak = (claim.get("mainsnak") or {}).get("datavalue") or {}
        value = snak.get("value")
        if isinstance(value, dict):
            value = value.get("time") or value.get("id")
        if value is not None:
            values.append(value)
    return values


def _year(value):
    m = re.search(r"(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None


class Provider:
    def __init__(self, client):
        self.client = client

    def search_entities(self, term, limit=20):
        url = "%s?%s" % (
            WIKIDATA_API,
            urllib.parse.urlencode(
                {
                    "action": "wbsearchentities",
                    "search": term,
                    "language": "en",
                    "format": "json",
                    "limit": limit,
                }
            ),
        )
        return self.client.fetch_json(url).get("search") or []

    def entity(self, qid):
        data = self.client.fetch_json(WIKIDATA_ENTITY % qid)
        return (data.get("entities") or {}).get(qid) or {}

    def ids_from_entity(self, entity):
        tmdb = _claims(entity, P_TMDB)
        imdb = _claims(entity, P_IMDB)
        tvdb = _claims(entity, P_TVDB) or _claims(entity, P_TVDB_SERIES)
        released = _claims(entity, P_RELEASE) or _claims(entity, P_START)
        return {
            "tmdb": tmdb[0] if tmdb else None,
            "imdb": imdb[0] if imdb else None,
            "tvdb": tvdb[0] if tvdb else None,
            "year": _year(released[0]) if released else None,
        }

    def verify_tmdb(self, kind, tmdb_id, title):
        url = (TMDB_MOVIE if kind == "movie" else TMDB_TV) % tmdb_id
        status, body = self.client.fetch(url)
        if status != 200:
            return False
        needle = re.sub(r"[^a-z0-9]+", "", title.lower())
        haystack = re.sub(r"[^a-z0-9]+", "", body.lower())
        return needle in haystack

    def resolve_movie(self, title, year=None):
        term = "%s (%s film)" % (title, year) if year else "%s (film)" % title
        for candidate in self.search_entities(term) or self.search_entities(title):
            entity = self.entity(candidate["id"])
            ids = self.ids_from_entity(entity)
            if not ids["tmdb"] or not ids["imdb"]:
                continue
            if not self.verify_tmdb("movie", ids["tmdb"], title):
                log.info("tmdb %s did not confirm %r, skipping", ids["tmdb"], title)
                continue
            label = ((entity.get("labels") or {}).get("en") or {}).get("value") or title
            return {
                "title": label,
                "year": ids["year"] or year,
                "tmdb": ids["tmdb"],
                "imdb": ids["imdb"],
                "qid": candidate["id"],
            }
        return None

    def resolve_show(self, name, year=None):
        for term in ("%s (TV series)" % name, name):
            for candidate in self.search_entities(term):
                entity = self.entity(candidate["id"])
                ids = self.ids_from_entity(entity)
                if not ids["tvdb"]:
                    continue
                if ids["tmdb"] and not self.verify_tmdb("tv", ids["tmdb"], name):
                    log.info("tmdb %s did not confirm show %r", ids["tmdb"], name)
                    continue
                label = ((entity.get("labels") or {}).get("en") or {}).get("value") or name
                return {
                    "show": label,
                    "show_year": ids["year"] or year,
                    "tvdb": ids["tvdb"],
                    "tmdb": ids["tmdb"],
                    "qid": candidate["id"],
                }
        return None

    def series_slug(self, tvdb_id):
        final = self.client.final_url(TVDB_DEREFERRER % tvdb_id)
        m = re.search(r"/series/([^/?#]+)", final)
        if not m:
            raise ProviderError("could not resolve a slug for tvdbid %s" % tvdb_id)
        return m.group(1)

    def episodes_for_order(self, slug, order="official"):
        status, body = self.client.fetch(TVDB_SEASONS % (slug, order))
        if status != 200:
            return []
        found = []
        for season, episode, title in EPISODE_LABEL.findall(body):
            found.append(
                {
                    "season": int(season),
                    "episode": int(episode),
                    "title": re.sub(r"<[^>]+>", "", title).strip(),
                }
            )
        return found

    def all_orders(self, slug):
        orders = {}
        for order in ("official", "dvd", "absolute", "alternate"):
            found = self.episodes_for_order(slug, order)
            if found:
                orders[order] = found
        return orders

    def identify(self, row, container, kind):
        source = row["source_path"]
        name = os.path.splitext(os.path.basename(source))[0]
        if kind == "movie":
            guess, year = _clean_movie_name(name)
            resolved = self.resolve_movie(guess, year)
            if resolved is None:
                return None
            return resolved

        show_guess = _clean_show_name(name, os.path.basename(os.path.dirname(source)))
        resolved = self.resolve_show(show_guess)
        if resolved is None:
            return None

        slug = self.series_slug(resolved["tvdb"])
        catalogue = self.episodes_for_order(slug, "official")
        if not catalogue:
            return None

        entry, how, score = episodemod.match_episode(source, catalogue)
        if entry is None:
            parsed = episodemod.parse_filename(os.path.basename(source))
            if parsed is None:
                return None
            log.warning(
                "no title match for %s, falling back to source numbering", os.path.basename(source)
            )
            entry = {
                "season": parsed["season"],
                "episode": parsed["first"],
                "title": episodemod.title_from_filename(source),
            }
            how = "fallback-numbering"

        warnings = self._order_warning(slug, catalogue)
        return {
            "title": episodemod.to_part_suffix(entry["title"]),
            "show": resolved["show"],
            "show_year": resolved["show_year"],
            "season": entry["season"],
            "episode": entry["episode"],
            "tvdb": resolved["tvdb"],
            "tmdb": resolved["tmdb"],
            "match_method": how,
            "match_score": score,
            "order_warnings": warnings,
        }

    def _order_warning(self, slug, aired):
        try:
            orders = self.all_orders(slug)
        except ProviderError:
            return []
        aired_counts = episodemod.season_counts(aired)
        notes = []
        for order, catalogue in orders.items():
            if order == "official":
                continue
            counts = episodemod.season_counts(catalogue)
            if counts != aired_counts:
                notes.append(
                    "%s order has per-season counts %s against aired %s"
                    % (order, counts, aired_counts)
                )
        return notes


YEAR_IN_NAME = re.compile(r"[.\s(\[_-](19\d{2}|20\d{2})[)\].\s_-]")
JUNK = re.compile(
    r"\b(1080p|720p|2160p|4k|bluray|blu-ray|bdrip|brrip|webrip|web-?dl|hdtv|remux|"
    r"x26[45]|h\.?26[45]|hevc|avc|xvid|divx|aac|ac3|dts(?:-hd)?|truehd|atmos|"
    r"ma|5\.1|7\.1|2\.0|10bit|8bit|hdr10?|dovi|dv|proper|repack|extended|"
    r"uncut|remastered|imax|multi|dual|complete)\b",
    re.I,
)


def _clean_movie_name(name):
    year = None
    m = YEAR_IN_NAME.search(name)
    if m:
        year = int(m.group(1))
        name = name[: m.start()]
    name = name.replace(".", " ").replace("_", " ")
    name = JUNK.sub(" ", name)
    name = re.sub(r"[-\[\(].*$", "", name)
    return re.sub(r"\s+", " ", name).strip(), year


def _clean_show_name(name, parent):
    for candidate in (name, parent):
        cleaned = re.split(
            r"(?:^|[^a-z0-9])s\d{1,2}[\s._-]*e\d{1,3}", candidate, flags=re.I
        )[0]
        cleaned = re.split(r"(?:^|[^a-z0-9])\d{1,2}x\d{1,3}", cleaned, flags=re.I)[0]
        cleaned = cleaned.replace(".", " ").replace("_", " ")
        cleaned = JUNK.sub(" ", cleaned)
        cleaned = YEAR_IN_NAME.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -[](){}")
        if cleaned:
            return cleaned
    return name
