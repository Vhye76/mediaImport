import difflib
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION
from . import episodes as episodemod
from . import tags as tagsmod
from . import titles

log = logging.getLogger("provider")

USER_AGENT = "mediaimport/%s (+https://github.com/Vhye76/mediaImport)" % VERSION
THROTTLE_SECONDS = 3.0
TIMEOUT = 30
TITLE_CUTOFF = episodemod.FUZZY_CUTOFF
CONTAINED_SCORE = 0.9
TEXT_SEARCH_LIMIT = 10

P_TMDB = "P4947"
P_IMDB = "P345"
P_TVDB = "P4835"
P_TMDB_TV = "P4983"
P_RELEASE = "P577"
P_START = "P580"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/%s.json"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
TMDB_MOVIE = "https://www.themoviedb.org/movie/%s"
TMDB_TV = "https://www.themoviedb.org/tv/%s"

TVDB_POSTER = re.compile(
    r"https://artworks\.thetvdb\.com/banners/posters/[^\"'\s>]+"
)

OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
TVDB_DEREFERRER = "https://thetvdb.com/dereferrer/series/%s"
TVDB_SERIES = "https://thetvdb.com/series/%s"
TVDB_SEASONS = "https://thetvdb.com/series/%s/allseasons/%s"
TVDB_SPECIALS = "https://thetvdb.com/series/%s/seasons/%s/0"

EPISODE_LABEL = re.compile(
    r'episode-label">S(\d{1,2})E(\d{1,3})</span>.*?<a[^>]*>\s*(.*?)\s*</a>',
    re.I | re.S,
)
SPECIAL_ROW = re.compile(
    r"<td>\s*S(\d{1,2})E(\d{1,3})\s*</td>\s*<td>\s*<a[^>]*>\s*(.*?)\s*</a>",
    re.I | re.S,
)


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    pass


#----- The HTTP client, throttled and cached
class Client:
    def __init__(self, cache_dir, throttle=THROTTLE_SECONDS):
        self.cache_dir = str(cache_dir)
        self.throttle = throttle
        self._last = 0.0
        self._local = threading.local()
        os.makedirs(self.cache_dir, exist_ok=True)

    #----- Thread-local, so a bypass on one assessment worker leaves the others on the cache.
    class _Fresh:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            self.client._local.fresh = True
            return self

        def __exit__(self, *exc):
            self.client._local.fresh = False
            return False

    def fresh(self):
        return Client._Fresh(self)

    def _bypassing(self):
        return bool(getattr(self._local, "fresh", False))

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
        if use_cache and not self._bypassing():
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


#----- Wikidata claim readers
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


RANK_ORDER = {"preferred": 2, "normal": 1}


def _dated_claims(entity, prop):
    rows = []
    for claim in (entity.get("claims") or {}).get(prop, []):
        if claim.get("rank") == "deprecated":
            continue
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if not isinstance(value, dict) or not value.get("time"):
            continue
        rows.append(
            {
                "time": value["time"],
                "precision": int(value.get("precision") or 0),
                "rank": RANK_ORDER.get(claim.get("rank"), 1),
            }
        )
    return rows


def _best_date(entity, prop):
    rows = _dated_claims(entity, prop)
    if not rows:
        return None
    top_rank = max(r["rank"] for r in rows)
    rows = [r for r in rows if r["rank"] == top_rank]
    top_precision = max(r["precision"] for r in rows)
    candidates = [r for r in rows if r["precision"] == top_precision]
    chosen = sorted(candidates, key=lambda r: r["time"])[0]
    log.debug(
        "%s: chose %s from %d claim(s) at rank %d precision %d",
        prop, chosen["time"], len(_dated_claims(entity, prop)),
        chosen["rank"], chosen["precision"],
    )
    return chosen["time"]


def _year(value):
    m = re.search(r"(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None


#----- Resolution
class Provider:
    def __init__(self, client, import_root=None):
        self.client = client
        self.import_root = os.path.normpath(str(import_root)) if import_root else None
        self._tvdb_posters = {}

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

    def search_text(self, term, limit=TEXT_SEARCH_LIMIT):
        url = "%s?%s" % (
            WIKIDATA_API,
            urllib.parse.urlencode(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": term,
                    "srlimit": limit,
                    "format": "json",
                }
            ),
        )
        hits = (self.client.fetch_json(url).get("query") or {}).get("search") or []
        return [h["title"] for h in hits if str(h.get("title", "")).startswith("Q")]

    def labels(self, qids):
        if not qids:
            return []
        url = "%s?%s" % (
            WIKIDATA_API,
            urllib.parse.urlencode(
                {
                    "action": "wbgetentities",
                    "ids": "|".join(qids),
                    "props": "labels|aliases",
                    "languages": "en",
                    "format": "json",
                }
            ),
        )
        entities = self.client.fetch_json(url).get("entities") or {}
        out = []
        for qid in qids:
            entity = entities.get(qid) or {}
            label = ((entity.get("labels") or {}).get("en") or {}).get("value")
            aliases = [a.get("value") for a in (entity.get("aliases") or {}).get("en") or []]
            out.append({"id": qid, "label": label, "aliases": [a for a in aliases if a]})
        return out

    def entity(self, qid):
        data = self.client.fetch_json(WIKIDATA_ENTITY % qid)
        return (data.get("entities") or {}).get(qid) or {}

    #----- P4983 is TMDB's series id, not a TVDB id;  TVDB comes from P4835 alone.
    def ids_from_entity(self, entity, kind="movie"):
        tmdb = _claims(entity, P_TMDB if kind == "movie" else P_TMDB_TV)
        imdb = _claims(entity, P_IMDB)
        tvdb = _claims(entity, P_TVDB)
        released = _best_date(entity, P_RELEASE) or _best_date(entity, P_START)
        return {
            "tmdb": tmdb[0] if tmdb else None,
            "imdb": imdb[0] if imdb else None,
            "tvdb": tvdb[0] if tvdb else None,
            "year": _year(released) if released else None,
        }

    def verify_tmdb(self, kind, tmdb_id, title):
        url = (TMDB_MOVIE if kind == "movie" else TMDB_TV) % tmdb_id
        status, body = self.client.fetch(url)
        if status != 200:
            return False
        needle = re.sub(r"[^a-z0-9]+", "", title.lower())
        haystack = re.sub(r"[^a-z0-9]+", "", body.lower())
        return needle in haystack

    def verify_tvdb(self, tvdb_id, name):
        try:
            status, body = self.client.fetch(TVDB_SERIES % self.series_slug(tvdb_id))
        except urllib.error.HTTPError as exc:
            log.info("tvdb %s does not dereference: HTTP %s", tvdb_id, exc.code)
            return False
        except urllib.error.URLError as exc:
            raise ProviderError("could not reach thetvdb for %s: %s" % (tvdb_id, exc))
        except ProviderError as exc:
            log.info("tvdb %s does not lead to a series page: %s", tvdb_id, exc)
            return False
        if status != 200:
            return False
        needle = re.sub(r"[^a-z0-9]+", "", name.lower())
        haystack = re.sub(r"[^a-z0-9]+", "", body.lower())
        return needle in haystack

    def tmdb_poster(self, kind, tmdb_id):
        if not tmdb_id:
            return None
        url = (TMDB_MOVIE if kind == "movie" else TMDB_TV) % tmdb_id
        try:
            status, body = self.client.fetch(url)
        except ProviderError as exc:
            log.debug("poster lookup failed for %s: %s", url, exc)
            return None
        if status != 200:
            return None
        match = OG_IMAGE.search(body)
        if not match:
            log.debug("no og:image on %s", url)
            return None
        return match.group(1)

    def tvdb_poster(self, tvdb_id):
        if not tvdb_id:
            return None
        key = str(tvdb_id)
        if key in self._tvdb_posters:
            return self._tvdb_posters[key]
        url = None
        try:
            status, body = self.client.fetch(TVDB_SERIES % self.series_slug(key))
            if status == 200:
                match = TVDB_POSTER.search(body)
                url = match.group(0) if match else None
                if url is None:
                    log.debug("no poster artwork on the tvdb page for %s", key)
        except Exception as exc:
            log.debug("tvdb poster lookup failed for %s: %s", key, exc)
        self._tvdb_posters[key] = url
        return url

    #----- The movie identity ladder
    def movie_candidates(self, source, container, origin=None):
        stem = os.path.splitext(os.path.basename(source))[0]
        parent = os.path.basename(os.path.dirname(source))
        origin_parent = os.path.basename(os.path.dirname(origin)) if origin else ""
        rungs = []

        embedded = tagsmod.movie_identity(source)
        if embedded:
            rungs.append((
                "embedded tag",
                {"tmdb": embedded.get("tmdb"), "imdb": embedded.get("imdb")},
                embedded.get("title"),
                embedded.get("year"),
            ))

        for label, name in (("filename ids", stem), ("folder ids", parent),
                            ("origin folder ids", origin_parent)):
            if not name:
                continue
            ids = titles.ids_from_name(name)
            if ids["tmdb"] and ids["imdb"]:
                rungs.append((label, ids, titles.title_before_ids(name), ids["year"]))

        segment = (container or {}).get("segment_title")
        if segment:
            rungs.append(("segment title", {}, segment, None))

        guess, year = _clean_movie_name(stem)
        if guess:
            rungs.append(("filename", {}, guess, year))

        directory = os.path.normpath(os.path.dirname(source))
        if self.import_root and directory == self.import_root:
            log.debug("file sits directly in the watched root, parent folder rung skipped")
        else:
            parent_guess, parent_year = _clean_movie_name(parent)
            if parent_guess and parent_guess.lower() != (guess or "").lower():
                rungs.append(("parent folder", {}, parent_guess, parent_year))

        log.debug("identity ladder: %s", [r[0] for r in rungs])
        return rungs

    def identify_movie(self, source, container, origin=None):
        for rung, ids, title, year in self.movie_candidates(source, container, origin):
            resolved = self.accept_candidate(rung, ids, title, year)
            if resolved is not None:
                log.info(
                    "identified from %s: %s (%s) tmdb=%s imdb=%s",
                    rung, resolved.get("title"), resolved.get("year"),
                    resolved.get("tmdb"), resolved.get("imdb"),
                )
                return resolved
            log.debug("rung %s did not resolve", rung)
        return None

    def accept_candidate(self, rung, ids, title, year):
        tmdb = (ids or {}).get("tmdb")
        imdb = (ids or {}).get("imdb")
        if tmdb and imdb and title and year:
            if not self.verify_tmdb("movie", tmdb, title):
                log.info(
                    "%s carried tmdb %s but the TMDB page does not confirm %r, rejected",
                    rung, tmdb, title,
                )
                return None
            return {
                "title": title,
                "year": year,
                "tmdb": tmdb,
                "imdb": imdb,
                "qid": None,
                "identified_from": rung,
            }
        if not title:
            return None
        resolved = self.resolve_movie(title, year)
        if resolved is not None:
            resolved["identified_from"] = rung
        return resolved

    #----- The show identity ladder
    def show_candidates(self, source, origin=None):
        stem = os.path.splitext(os.path.basename(source))[0]
        parent = os.path.basename(os.path.dirname(source))
        grandparent = os.path.basename(os.path.dirname(os.path.dirname(source)))
        rungs = []

        embedded = tagsmod.show_identity(source)
        if embedded:
            rungs.append((
                "embedded tag",
                {"tvdb": embedded.get("tvdb"), "tmdb": embedded.get("tmdb")},
                embedded.get("title"),
                None,
            ))

        folders = [("folder ids", parent), ("folder ids", grandparent)]
        if origin:
            origin_dir = os.path.dirname(origin)
            folders.append(("origin folder ids", os.path.basename(origin_dir)))
            folders.append(("origin folder ids", os.path.basename(os.path.dirname(origin_dir))))
        for label, name in folders:
            if not name:
                continue
            ids = titles.ids_from_name(name)
            if ids["tvdb"]:
                rungs.append((label, ids, titles.title_before_ids(name), ids["year"]))

        guess = _clean_show_name(stem, parent)
        if guess:
            rungs.append(("filename", {}, guess, _show_year(stem, parent)))

        log.debug("show identity ladder: %s", [r[0] for r in rungs])
        return rungs

    def identify_show(self, source, origin=None):
        for rung, ids, name, year in self.show_candidates(source, origin):
            resolved = self.accept_show_candidate(rung, ids, name, year)
            if resolved is not None:
                log.info(
                    "identified show from %s: %s (%s) tvdb=%s tmdb=%s",
                    rung, resolved.get("show"), resolved.get("show_year"),
                    resolved.get("tvdb"), resolved.get("tmdb"),
                )
                return resolved
            log.debug("rung %s did not resolve", rung)
        return None

    def accept_show_candidate(self, rung, ids, name, year):
        tvdb = (ids or {}).get("tvdb")
        tmdb = (ids or {}).get("tmdb")
        if not name:
            return None
        if tvdb:
            if not self.verify_tvdb(tvdb, name):
                log.info(
                    "%s carried tvdb %s but the TVDB page does not confirm %r, rejected",
                    rung, tvdb, name,
                )
                return None
            if tmdb and not self.verify_tmdb("tv", tmdb, name):
                log.info("%s carried tmdb %s but the TMDB page does not confirm %r, dropped", rung, tmdb, name)
                tmdb = None
            return {
                "show": name,
                "show_year": year,
                "tvdb": tvdb,
                "tmdb": tmdb,
                "qid": None,
                "identified_from": rung,
            }
        resolved = self.resolve_show(name, year)
        if resolved is not None:
            resolved["identified_from"] = rung
        return resolved

    #----- Wikidata search paths
    def resolve_movie(self, title, year=None):
        return self._resolve_by_search(
            title, year, _movie_search_terms(title, year), self._accept_search_hit,
        )

    #----- Prefix hits already matched the whole string, so they are ordered by score but not cut;
    #----- full-text hits matched on any word and must clear the cutoff.
    def _resolve_by_search(self, title, year, terms, accept):
        wanted = titles.normalise_for_match(title)
        seen = set()
        for term in terms:
            resolved = self._resolve_from(
                term, self.search_entities(term), title, wanted, year, seen, accept, cutoff=0.0,
            )
            if resolved is not None:
                return resolved
        qids = [q for q in self.search_text(title) if q not in seen]
        if not qids:
            return None
        return self._resolve_from(
            "text:%s" % title, self.labels(qids), title, wanted, year, seen, accept,
            cutoff=TITLE_CUTOFF,
        )

    def _resolve_from(self, term, candidates, title, wanted, year, seen, accept, cutoff):
        scored = []
        for candidate in candidates:
            if candidate["id"] in seen:
                continue
            seen.add(candidate["id"])
            score = _candidate_score(title, wanted, candidate)
            log.debug(
                "search %r: %s %r scored %.3f", term, candidate["id"],
                candidate.get("label"), score,
            )
            if score < cutoff:
                continue
            scored.append((score, candidate))
        for score, candidate in sorted(scored, key=lambda pair: -pair[0]):
            resolved = accept(candidate, title, year)
            if resolved is None:
                continue
            if score < 1.0:
                log.info(
                    "title %r matched %r by similarity %.3f on search %r",
                    title, resolved.get("title") or resolved.get("show"), score, term,
                )
            return resolved
        return None

    def _accept_search_hit(self, candidate, title, year):
        entity = self.entity(candidate["id"])
        ids = self.ids_from_entity(entity)
        if not ids["tmdb"] or not ids["imdb"]:
            return None
        if year and ids["year"] and abs(int(ids["year"]) - int(year)) > 1:
            log.info(
                "%s %r is a %s release, not %s, skipping",
                candidate["id"], candidate.get("label"), ids["year"], year,
            )
            return None
        if not self.verify_tmdb("movie", ids["tmdb"], title):
            log.info("tmdb %s did not confirm %r, skipping", ids["tmdb"], title)
            return None
        label = ((entity.get("labels") or {}).get("en") or {}).get("value") or title
        return {
            "title": label,
            "year": ids["year"] or year,
            "tmdb": ids["tmdb"],
            "imdb": ids["imdb"],
            "qid": candidate["id"],
        }

    def resolve_show(self, name, year=None):
        return self._resolve_by_search(
            name, year, ("%s (TV series)" % name, name), self._accept_show_hit,
        )

    def _accept_show_hit(self, candidate, name, year):
        entity = self.entity(candidate["id"])
        ids = self.ids_from_entity(entity, kind="tv")
        if not ids["tvdb"]:
            return None
        if year and ids["year"] and abs(int(ids["year"]) - int(year)) > 1:
            log.info(
                "%s %r started in %s, not %s, skipping",
                candidate["id"], candidate.get("label"), ids["year"], year,
            )
            return None
        if ids["tmdb"] and not self.verify_tmdb("tv", ids["tmdb"], name):
            log.info("tmdb %s did not confirm show %r", ids["tmdb"], name)
            return None
        label = ((entity.get("labels") or {}).get("en") or {}).get("value") or name
        return {
            "show": label,
            "show_year": ids["year"] or year,
            "tvdb": ids["tvdb"],
            "tmdb": ids["tmdb"],
            "qid": candidate["id"],
        }

    #----- Television catalogue
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
        found = _catalogue_entries(EPISODE_LABEL.findall(body))
        #----- allseasons omits season 0;  the specials sit on their own page as a plain table.
        if not any(e["season"] == 0 for e in found):
            status, body = self.client.fetch(TVDB_SPECIALS % (slug, order))
            if status == 200:
                specials = _catalogue_entries(SPECIAL_ROW.findall(body))
                if specials:
                    log.debug("%s %s: %d special(s) from the season 0 page", slug, order, len(specials))
                found.extend(specials)
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
        if kind == "movie":
            return self.identify_movie(source, container, row.get("origin_path"))

        resolved = self.identify_show(source, row.get("origin_path"))
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
            "identified_from": resolved.get("identified_from"),
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


#----- the trailing delimiter is a lookahead so two adjacent years both match.
YEAR_IN_NAME = re.compile(r"[.\s(\[_-](19\d{2}|20\d{2})(?=[)\].\s_-]|$)")
JUNK = re.compile(
    r"\b(1080p|720p|2160p|4k|bluray|blu-ray|bdrip|brrip|webrip|web-?dl|hdtv|remux|"
    r"x26[45]|h\.?26[45]|hevc|avc|xvid|divx|aac|ac3|dts(?:-hd)?|truehd|atmos|"
    r"ma|5\.1|7\.1|2\.0|10bit|8bit|hdr10?|dovi|dv|proper|repack|extended|"
    r"uncut|remastered|imax|multi|dual|complete)\b",
    re.I,
)


#----- Name cleaning
def _catalogue_entries(rows):
    return [
        {
            "season": int(season),
            "episode": int(episode),
            "title": html.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
        }
        for season, episode, title in rows
    ]


#----- Title matching under the section 9 rules
def _movie_search_terms(title, year):
    terms = []
    if year:
        terms.append("%s (%s film)" % (title, year))
    terms.append(title)
    return terms


def _name_score(title, wanted, name):
    if titles.matches(name, title):
        return 1.0
    other = titles.normalise_for_match(name)
    if not other or not wanted:
        return 0.0
    if other == wanted:
        return 1.0
    #----- 'Return of the Jedi' inside 'Star Wars: Episode VI – Return of the Jedi'.
    if " %s " % wanted in " %s " % other:
        return CONTAINED_SCORE
    return difflib.SequenceMatcher(None, wanted, other).ratio()


def _candidate_score(title, wanted, candidate):
    names = [candidate.get("label")]
    names.extend(candidate.get("aliases") or [])
    match = candidate.get("match") or {}
    if match.get("text"):
        names.append(match["text"])
    return max((_name_score(title, wanted, n) for n in names if n), default=0.0)


def _clean_movie_name(name):
    year = None
    matches = list(YEAR_IN_NAME.finditer(name))
    if matches:
        m = matches[-1]
        year = int(m.group(1))
        name = name[: m.start()]
    name = name.replace(".", " ").replace("_", " ")
    name = JUNK.sub(" ", name)
    name = re.sub(r"[-\[\(].*$", "", name)
    return re.sub(r"\s+", " ", name).strip(), year


def _show_year(name, parent):
    for candidate in (name, parent):
        found = titles.YEAR_IN_PARENS.findall(candidate or "")
        if found:
            return int(found[-1])
    return None


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
