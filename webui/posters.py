"""
Poster lookup via IMDb's suggestion API. The web UI uses this to show a poster for each queued job, 
and the CLI uses it to show a poster in the "info" command.
"""
import json
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent / "poster_cache.json"
SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/x/{query}.json"
_lock = threading.Lock()
_cache = None

_MOVIE_TYPES = {"movie", "tvMovie", "video", "short"}
_SHOW_TYPES = {"tvSeries", "tvMiniSeries", "tvSpecial", "tvShort"}


def _load_cache():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache():
    try:
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(_cache, indent=1), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError:
        pass


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _thumb(url):
    # IMDb image URLs take size parameters before the extension: ask for a 300px-wide
    # version instead of the multi-megabyte original.
    return re.sub(r"\._V1_.*?\.(jpg|png)$", r"._V1_UX300_.\1", url) if url else url


def lookup(name, year=None, kind=None, timeout=10):
    """Poster URL for a movie/show name, or None. kind: "MOVIE" / "TV" (vinetrimmer's
    Title.Types names) narrows the match."""
    if not name:
        return None
    key = f"{_norm(name)}|{year or ''}|{kind or ''}"
    with _lock:
        cache = _load_cache()
        if key in cache:
            return cache[key]

    url = SUGGEST_URL.format(query=urllib.parse.quote(name.lower()[:100]))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            results = json.load(resp).get("d") or []
    except Exception:
        return None  # not cached: try again next time

    wanted_types = _MOVIE_TYPES if kind == "MOVIE" else _SHOW_TYPES if kind == "TV" else None
    candidates = [r for r in results if (r.get("i") or {}).get("imageUrl")]
    if wanted_types:
        candidates = [r for r in candidates if r.get("qid") in wanted_types] or candidates

    def score(r):
        s = 0
        if _norm(r.get("l")) == _norm(name):
            s += 4
        if year and r.get("y"):
            diff = abs(int(r["y"]) - int(year))
            s += 3 if diff == 0 else 1 if diff == 1 else 0
        return s

    best = max(candidates, key=score) if candidates else None
    poster = _thumb(best["i"]["imageUrl"]) if best and score(best) > 0 else None
    with _lock:
        _load_cache()[key] = poster
        _save_cache()
    return poster
