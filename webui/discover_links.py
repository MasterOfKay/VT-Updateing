"""
Turns the streaming links JustWatch hands out into links vinetrimmer's services accept.

JustWatch points wherever the provider's own app wants you to land: often one episode of a
show, with tracking parameters, or on a domain the service script doesn't know. Each rule
here rewrites one of those into the form the matching service's TITLE_RE reads. The service
is picked from the link's domain, so it's right whatever JustWatch calls the package.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/137.0 Safari/537.36")
_VIAPLAY_TLDS = ("se", "no", "dk", "fi", "pl", "is", "ee", "lv", "lt")
_UUID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
_EPISODE_NOTE = "This link is for one episode. Open it and copy the show's link to download more of the show."
_UNCHECKED_NOTE = "This link format hasn't been confirmed to work with the downloader. Check it first."


def _result(service, url, note=None, resolve=False, exact=True):
    """url: what goes in the downloader's Title field (a link, or for Netflix and Amazon
    the title ID).
    resolve: the link can be turned into a show link by resolve() when it's used.
    exact: False when the link only covers one episode."""
    return {"service": service, "url": url, "note": note, "resolve": resolve, "exact": exact}


def _host_is(host, *domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def unwrap(url):
    """Affiliate / click-tracking links carry the real one in a query parameter."""
    for _ in range(3):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        inner = next((query[k][0] for k in ("u", "r", "url", "murl")
                      if k in query and query[k][0].startswith("http")), None)
        if not inner:
            break
        url = inner
    return url


def netflix_id(url):
    """The title number in a Netflix link, or None."""
    found = re.search(r"netflix\.com/(?:[a-z-]+/)?(?:title|watch)/(\d+)|[?&]jbv=(\d+)|unogs\.com/title/(\d+)", url or "")
    return next((g for g in found.groups() if g), None) if found else None


def amazon_id(url):
    """The title's GTI or ASIN in an Amazon / Prime Video link, or None."""
    url = urllib.parse.unquote(url or "")
    found = re.search(r"amzn1\.dv\.gti\.[a-f0-9-]+", url)
    if found:
        return found.group(0)
    found = re.search(r"(?:/(?:detail|dp|product)/|[?&]asin=)([A-Z0-9]{10,})", url)
    return found.group(1) if found else None


def title_value(service, title):
    """What to put in the Title field for a pasted link: Netflix and Amazon want the ID,
    every other service takes the link as it is."""
    title = (title or "").strip()
    finder = {"netflix": netflix_id, "amazon": amazon_id}.get((service or "").lower())
    if not finder or not title.lower().startswith("http"):
        return title
    return finder(title) or title


def normalize(url, kind, country="", package=""):
    """kind: "movie" or "show". package: JustWatch's technical name of the offer, which
    is the only way to tell Apple TV+ from the Apple TV Store. Returns None for links no
    service handles."""
    url = unwrap((url or "").strip())
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    path = parts.path.rstrip("/")
    query = urllib.parse.parse_qs(parts.query)
    bare = f"https://{parts.netloc}{path}"
    show = kind == "show"

    def match(pattern):
        return re.search(pattern, path)

    if not path and not parts.query:
        return None  # a provider's start page, not a title

    # Netflix and Amazon take the title's ID rather than a link.
    if _host_is(host, "netflix.com"):
        found = netflix_id(url)
        return _result("Netflix", found) if found else _result("Netflix", bare, _UNCHECKED_NOTE)

    if re.search(r"(^|\.)amazon\.[a-z.]+$", host) or _host_is(host, "primevideo.com"):
        found = amazon_id(url)
        return _result("Amazon", found) if found else _result("Amazon", url, _UNCHECKED_NOTE)

    if host == "tv.apple.com":
        service = "iTunes" if package == "itunes" else "AppleTVPlus"
        show_id = (query.get("showId") or [None])[0]
        episode = match(r"^/([a-z]{2})/episode/")
        if show and episode and show_id:
            return _result(service, f"https://tv.apple.com/{episode.group(1)}/show/show/{show_id}")
        return _result(service, bare, _EPISODE_NOTE if show and episode else None, exact=not (show and episode))

    if _host_is(host, "disneyplus.com"):
        return _result("DisneyPlus", bare)

    if _host_is(host, "crunchyroll.com"):
        if show and "/watch/" in path:
            return _result("Crunchyroll", bare, "The show's link is looked up when you use it.", resolve=True,
                           exact=False)
        return _result("Crunchyroll", bare)

    if _host_is(host, "paramountplus.com"):
        found = match(r"/shows/([A-Za-z0-9_-]+)")
        if show and found:
            return _result("ParamountPlus", f"https://{parts.netloc}{path[:found.end()]}/")
        return _result("ParamountPlus", bare)

    if _host_is(host, "hbomax.com", "max.com"):
        found = match(rf"/(movie|show)/({_UUID})")
        if found:
            return _result("HBOMax", f"https://play.hbomax.com/{found.group(1)}/{found.group(2)}")
        return _result("HBOMax", bare, _EPISODE_NOTE, exact=False)

    if _host_is(host, "hulu.com"):
        found = match(rf"/watch/({_UUID})")
        if found:
            if show:
                return _result("Hulu", bare, _EPISODE_NOTE, exact=False)
            return _result("Hulu", f"https://www.hulu.com/movie/{found.group(1)}")
        return _result("Hulu", bare)

    if _host_is(host, "peacocktv.com"):
        found = match(r"^(/watch/asset/tv/[a-z0-9.-]+/\d+)")
        if found:
            return _result("Peacock", f"https://www.peacocktv.com{found.group(1)}")
        return _result("Peacock", bare, None if "/watch/asset/" in path else _UNCHECKED_NOTE)

    if _host_is(host, "pluto.tv"):
        if "/live-tv/" in path:
            return None  # a live channel, nothing to download
        found = match(r"/(movies|series)/([^/]+)")
        if found:
            return _result("PLUTO", f"https://pluto.tv/on-demand/{found.group(1)}/{found.group(2)}")
        return _result("PLUTO", bare, _UNCHECKED_NOTE)

    if _host_is(host, "tubitv.com"):
        return _result("TUBI", bare)

    if _host_is(host, "youtube.com", "youtu.be"):
        if host == "youtu.be":
            return _result("YouTube", f"https://www.youtube.com/watch?v={path.lstrip('/')}")
        keep = {k: query[k][0] for k in ("v", "list") if k in query}
        return _result("YouTube", bare + (f"?{urllib.parse.urlencode(keep)}" if keep else ""))

    if _host_is(host, "bbc.co.uk"):
        single = show and "/iplayer/episode/" in path
        return _result("BBCiPlayer", bare, _EPISODE_NOTE if single else None, exact=not single)

    if _host_is(host, "itv.com"):
        return _result("ITV", bare)

    if _host_is(host, "channel4.com"):
        found = match(r"^(/programmes/[a-z0-9-]+)")
        return _result("All4", f"https://www.channel4.com{found.group(1)}" if found else bare)

    if _host_is(host, "channel5.com"):
        return _result("MY5", bare)

    if _host_is(host, "nowtv.it"):
        return _result("NowTVIT", bare, _UNCHECKED_NOTE)

    if _host_is(host, "nowtv.com"):
        if "/watch/asset" in path:
            return _result("NowTVUK", bare)
        found = match(r"^/watch/(?:from/)?([a-z0-9-]+/[A-Za-z0-9]{15,})")
        if found:
            return _result("NowTVUK", f"https://www.nowtv.com/watch/asset/{found.group(1)}", _UNCHECKED_NOTE)
        return _result("NowTVUK", bare, _UNCHECKED_NOTE)

    if _host_is(host, "wowtv.de"):
        found = match(r"^(/watch/asset/[a-z0-9-]+/\d+)/seasons")
        return _result("WowTV", f"https://www.wowtv.de{found.group(1)}" if show and found else bare, _UNCHECKED_NOTE)

    if _host_is(host, "skyshowtime.com"):
        found = match(rf"/(tv|movies)/([a-z0-9.-]+)/({_UUID})")
        if found:
            return _result("Skyshowtime", "https://www.skyshowtime.com/watch/asset/" + "/".join(found.groups()))
        return _result("Skyshowtime", bare, _UNCHECKED_NOTE)

    if _host_is(host, "stan.com.au"):
        return _result("Stan", bare)

    if _host_is(host, "crave.ca"):
        found = match(r"^/([a-z]{2})/play/([a-z0-9-]+)/")
        if show and found:
            return _result("Crave", f"https://www.crave.ca/{found.group(1)}/tv-shows/{found.group(2)}", _UNCHECKED_NOTE)
        return _result("Crave", bare)

    if _host_is(host, "britbox.com"):
        single = show and "/episode/" in path
        return _result("BritBox", bare, _EPISODE_NOTE if single else None, exact=not single)

    if _host_is(host, "videoland.com"):
        return _result("Videoland", bare)

    viaplay = re.search(r"(^|\.)viaplay\.([a-z]+)$", host)
    if viaplay:
        found = match(r"^(.*/series/[^/]+)")
        clean = found.group(1) if show and found else path
        tld = viaplay.group(2)
        if tld == "com" and country.lower() in _VIAPLAY_TLDS:
            tld = country.lower()
        return _result("Viaplay", f"https://viaplay.{tld}{clean}", None if tld in _VIAPLAY_TLDS else _UNCHECKED_NOTE)

    if _host_is(host, "rakuten.tv"):
        return _result("RakutenTV", bare)

    if _host_is(host, "roku.com"):
        found = match(r"^/(?:details|watch)/([a-f0-9]{32})")
        if found:
            return _result("ROKU", f"https://therokuchannel.roku.com/details/{found.group(1)}")
        return _result("ROKU", bare, _UNCHECKED_NOTE)

    if _host_is(host, "discoveryplus.com"):
        found = match(rf"/(show|movie|video)/({_UUID})")
        if found:
            return _result("DSCPUS", f"https://play.discoveryplus.com/{found.group(1)}/{found.group(2)}")
        return _result("DSCPUS", bare, _UNCHECKED_NOTE)

    if _host_is(host, "sonyliv.com"):
        found = match(r"^(/shows/[a-z0-9-]+-\d+)")
        return _result("Sonyliv", f"https://www.sonyliv.com{found.group(1)}" if show and found else bare)

    if _host_is(host, "hotstar.com"):
        usable = match(r"/(shows|movies)/")
        return _result("Hotstar", bare, None if usable else _UNCHECKED_NOTE)

    if _host_is(host, "moviesanywhere.com"):
        return _result("MoviesAnywhere", bare)

    return None


# ---- Crunchyroll: episode link -> show link ----

_cr_lock = threading.Lock()
_cr_token = {"value": None, "expires": 0}


def _json(request, timeout=15):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _crunchyroll_token():
    """The anonymous token crunchyroll.com gives every visitor - no account involved."""
    with _cr_lock:
        if _cr_token["value"] and time.time() < _cr_token["expires"]:
            return _cr_token["value"]
        data = _json(urllib.request.Request(
            "https://www.crunchyroll.com/auth/v1/token",
            data=urllib.parse.urlencode({"grant_type": "client_id"}).encode(),
            headers={"Authorization": "Basic Y3Jfd2ViOg==", "User-Agent": _BROWSER_UA,
                     "Content-Type": "application/x-www-form-urlencoded"}))
        _cr_token["value"] = data["access_token"]
        _cr_token["expires"] = time.time() + max(30, int(data.get("expires_in") or 300) - 30)
        return _cr_token["value"]


def resolve(url):
    """The show link behind a Crunchyroll episode link: {url, season, episode}, where
    season/episode say which episode the original link was. Raises ValueError when it
    can't be looked up."""
    found = re.search(r"crunchyroll\.com(?:/[a-z-]+)?/watch/([A-Z0-9]+)", url or "")
    if not found:
        raise ValueError("Only Crunchyroll episode links can be looked up.")
    try:
        data = _json(urllib.request.Request(
            f"https://www.crunchyroll.com/content/v2/cms/objects/{found.group(1)}",
            headers={"Authorization": f"Bearer {_crunchyroll_token()}", "User-Agent": _BROWSER_UA}))["data"][0]
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        raise ValueError(f"Crunchyroll didn't answer the show lookup ({e}). Open the link and copy the show's link.")
    meta = data.get("episode_metadata") or {}
    series = meta.get("series_id")
    if not series:
        raise ValueError("Crunchyroll didn't name a show for this link.")
    slug = meta.get("series_slug_title") or ""
    return {"url": f"https://www.crunchyroll.com/series/{series}" + (f"/{slug}" if slug else ""),
            "season": meta.get("season_number"), "episode": meta.get("episode_number")}
