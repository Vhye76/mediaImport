import logging
import re
import unicodedata

log = logging.getLogger("titles")

UNSAFE = '/\\:*?"<>|'
CONTROL = "".join(chr(c) for c in range(0x00, 0x20))

RESERVED = {"CON", "PRN", "AUX", "NUL"}
RESERVED |= {"COM%d" % n for n in range(1, 10)}
RESERVED |= {"LPT%d" % n for n in range(1, 10)}

EM_DASH = "—"
EN_DASH = "–"

_WS = re.compile(r"\s+")


class TitleError(ValueError):
    pass


def _collapse(s):
    return _WS.sub(" ", s).strip()


TMDBID_IN_NAME = re.compile(r"\[tmdbid-(\d+)\]", re.I)
IMDBID_IN_NAME = re.compile(r"\[imdbid-(tt\d+)\]", re.I)
TVDBID_IN_NAME = re.compile(r"\[tvdbid-(\d+)\]", re.I)
YEAR_IN_PARENS = re.compile(r"\((19\d{2}|20\d{2})\)")


#----- Reading ids back out of a name
def ids_from_name(name):
    s = str(name)
    tmdb = TMDBID_IN_NAME.search(s)
    imdb = IMDBID_IN_NAME.search(s)
    tvdb = TVDBID_IN_NAME.search(s)
    year = YEAR_IN_PARENS.search(s)
    found = {
        "tmdb": tmdb.group(1) if tmdb else None,
        "imdb": imdb.group(1) if imdb else None,
        "tvdb": tvdb.group(1) if tvdb else None,
        "year": int(year.group(1)) if year else None,
    }
    if any(found.values()):
        log.debug("ids parsed from %r: %s", s, found)
    return found


def title_before_ids(name):
    s = str(name)
    s = TMDBID_IN_NAME.sub(" ", s)
    s = IMDBID_IN_NAME.sub(" ", s)
    s = TVDBID_IN_NAME.sub(" ", s)
    s = YEAR_IN_PARENS.sub(" ", s)
    return _collapse(s)


#----- The filename transform
def to_filename(title):
    s = str(title)
    s = s.replace(EM_DASH, " - ").replace(EN_DASH, " - ")
    s = s.replace("/", "-")
    s = s.replace(":", "")
    s = "".join(ch for ch in s if ch not in CONTROL and ch not in UNSAFE)
    out = _collapse(s)
    if out != str(title):
        log.debug("filename transform: %r -> %r", str(title), out)
    return out


def matches(tag_title, name):
    return to_filename(tag_title) == _collapse(str(name))


def is_reserved(name):
    stem = str(name).split(".")[0].strip().upper()
    return stem in RESERVED


#----- Validation
def validate_component(name):
    problems = []
    s = str(name)
    if any(ch in CONTROL for ch in s):
        problems.append("control-character")
    for ch in UNSAFE:
        if ch in s:
            problems.append("unsafe-%s" % ch)
    if s != s.rstrip(". "):
        problems.append("trailing-period-or-space")
    if is_reserved(s):
        problems.append("reserved-device-name")
    return problems


def assert_component(name):
    problems = validate_component(name)
    if problems:
        raise TitleError("%r is not a safe path component: %s" % (name, ", ".join(problems)))
    return name


def normalise_for_match(s):
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace(EM_DASH, " - ").replace(EN_DASH, " - ")
    s = s.replace("&", " and ")
    s = re.sub(r"[^0-9A-Za-z ]+", " ", s)
    return _collapse(s).lower()


#----- Naming
def _require(**fields):
    missing = [name for name, value in fields.items() if value is None or value == ""]
    if missing:
        raise TitleError("cannot build a name without %s" % ", ".join(missing))


def movie_folder(title, year, tmdb, imdb):
    _require(title=title, year=year, tmdb=tmdb, imdb=imdb)
    imdb = str(imdb)
    if not imdb.startswith("tt"):
        imdb = "tt%s" % imdb
    return assert_component(
        "%s (%s) [tmdbid-%s] [imdbid-%s]" % (to_filename(title), year, tmdb, imdb)
    )


def movie_filename(title, year, edition=None, folder=None):
    if edition:
        base = folder or ""
        if not base:
            raise TitleError("an edition filename requires the full folder name")
        return assert_component("%s - %s.mkv" % (base, to_filename(edition)))
    _require(title=title, year=year)
    return assert_component("%s (%s).mkv" % (to_filename(title), year))


def show_folder(show, year, tvdb, tmdb):
    _require(show=show, year=year, tvdb=tvdb, tmdb=tmdb)
    return assert_component(
        "%s (%s) [tvdbid-%s] [tmdbid-%s]" % (to_filename(show), year, tvdb, tmdb)
    )


def season_folder(season):
    return "Season %02d" % int(season)


def episode_code(season, first, last=None):
    season = int(season)
    first = int(first)
    if last is None or int(last) == first:
        return "S%02dE%02d" % (season, first)
    return "S%02dE%02d-E%02d" % (season, first, int(last))


def episode_filename(show, season, first, episode_title, last=None):
    _require(show=show, season=season, first=first, episode_title=episode_title)
    return assert_component(
        "%s - %s - %s.mkv"
        % (to_filename(show), episode_code(season, first, last), to_filename(episode_title))
    )
