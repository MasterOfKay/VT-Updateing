"""
The Discover tab: what's new / popular / available on the streaming services, and where to get it.

Data comes from JustWatch's GraphQL API - the one justwatch.com itself uses. It isn't a
documented public API: it can change or stop answering at any time, and JustWatch's terms
only allow it for personal, non-commercial use. Only what you search for or browse is
sent; nothing from your configs or accounts.

Every offer's link is rewritten for the matching vinetrimmer service (discover_links.py)
and checked against that service's own TITLE_RE.
"""
import importlib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import discover_links, services_info

API_URL = "https://apis.justwatch.com/graphql"
IMAGES = "https://images.justwatch.com"
CACHE_SECONDS = 15 * 60
PAGE_SIZE = 40

COUNTRIES = [
    ("DE", "Germany"), ("US", "United States"), ("GB", "United Kingdom"), ("AT", "Austria"),
    ("CH", "Switzerland"), ("AU", "Australia"), ("BE", "Belgium"), ("BR", "Brazil"), ("CA", "Canada"),
    ("DK", "Denmark"), ("ES", "Spain"), ("FI", "Finland"), ("FR", "France"), ("IN", "India"),
    ("IE", "Ireland"), ("IT", "Italy"), ("JP", "Japan"), ("MX", "Mexico"), ("NL", "Netherlands"),
    ("NO", "Norway"), ("NZ", "New Zealand"), ("PL", "Poland"), ("PT", "Portugal"), ("SE", "Sweden"),
    ("TR", "Turkey"),
]
LANGUAGES = [("en", "English"), ("de", "Deutsch"), ("fr", "Français"), ("es", "Español"),
             ("it", "Italiano"), ("nl", "Nederlands"), ("pt", "Português"), ("pl", "Polski")]

# JustWatch package (technicalName) -> vinetrimmer service, for the provider filter.
PACKAGES = {
    "netflix": "Netflix", "netflixbasicwithads": "Netflix",
    "amazonprime": "Amazon", "amazonprimevideo": "Amazon", "amazon": "Amazon",
    "appletvplus": "AppleTVPlus", "itunes": "iTunes", "disneyplus": "DisneyPlus",
    "crunchyroll": "Crunchyroll", "max": "HBOMax", "hulu": "Hulu",
    "paramountplus": "ParamountPlus", "paramountpluspremium": "ParamountPlus",
    "paramountplusessential": "ParamountPlus", "paramountplusbasicwithads": "ParamountPlus",
    "peacocktv": "Peacock", "peacocktvpremium": "Peacock", "peacocktvpremiumplus": "Peacock",
    "plutotv": "PLUTO", "tubitv": "TUBI", "youtubefree": "YouTube", "youtubered": "YouTube",
    "bbc": "BBCiPlayer", "itv": "ITV", "itvpremium": "ITV", "all4": "All4", "all4plus": "All4",
    "my5": "MY5", "nowtv": "NowTVUK", "skyticket": "WowTV", "wuaki": "RakutenTV",
    "skyshowtime": "Skyshowtime", "stan": "Stan", "crave": "Crave", "britbox": "BritBox",
    "viaplay": "Viaplay", "videoland": "Videoland", "rokuchannel": "ROKU",
    "discoveryplus": "DSCPUS", "discoverypluseurope": "DSCPUS", "discoveryplusus": "DSCPUS",
    "sonyliv": "Sonyliv", "jiohotstar": "Hotstar", "moviesanywhere": "MoviesAnywhere",
}
# The order of the provider buttons; anything else follows alphabetically.
_SERVICE_ORDER = ("Netflix", "Amazon", "DisneyPlus", "Crunchyroll", "HBOMax", "AppleTVPlus",
                  "ParamountPlus", "Hulu", "Peacock", "WowTV", "NowTVUK", "Skyshowtime")
_CINEMA = ("CINEMA",)
# JustWatch's monetizationType -> how you get the title. A subscription sold as an
# add-on ("... Amazon Channel") counts as "channel", see _access.
_ACCESS = {"FLATRATE": "included", "LINEAR_FLATRATE": "included", "FREE": "free", "ADS": "ads", "FAST": "ads",
           "RENT": "rent", "BUY": "buy"}
_ACCESS_ORDER = ("included", "channel", "free", "ads", "rent", "buy", "disc")
# The price filter of the lists: the kinds of access each choice shows.
ACCESS_FILTERS = {"included": ("included", "free", "ads"), "paid": ("rent", "buy")}


class DiscoverError(Exception):
    pass


_lock = threading.Lock()
_cache = OrderedDict()


def _gql(query, variables, cache=True):
    key = json.dumps([query, variables], sort_keys=True)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    request = urllib.request.Request(
        API_URL, data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.load(response)
    except urllib.error.HTTPError as e:
        raise DiscoverError(f"JustWatch answered with an error (HTTP {e.code}).")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DiscoverError(f"Couldn't reach JustWatch: {e}")
    if not body.get("data"):
        message = next((x.get("message") for x in body.get("errors") or []), "no data")
        raise DiscoverError(f"JustWatch couldn't answer: {message}")
    if not cache:
        return body["data"]
    with _lock:
        _cache[key] = (time.time(), body["data"])
        while len(_cache) > 200:
            _cache.popitem(last=False)
    return body["data"]


def _installed():
    return {s["name"] for s in services_info.list_services()}


def _image(path, profile="s332"):
    if not path:
        return None
    return IMAGES + path.replace("{profile}", profile).replace("{format}", "jpg")


# ---- providers ----

_PACKAGES_QUERY = """query Packages($country: Country!) {
  packages(country: $country, platform: WEB) { shortName technicalName clearName icon(profile: S100) }
}"""


def providers(country):
    """The services that exist both in this country's catalogue and in this install:
    [{service, name, icon, packages}]."""
    installed = _installed()
    found = {}
    for package in _gql(_PACKAGES_QUERY, {"country": country})["packages"]:
        service = PACKAGES.get(package["technicalName"])
        if service not in installed:
            continue
        entry = found.setdefault(service, {"service": service, "name": package["clearName"],
                                           "icon": _image(package["icon"]), "packages": []})
        entry["packages"].append(package["shortName"])
    order = {name: i for i, name in enumerate(_SERVICE_ORDER)}
    return sorted(found.values(), key=lambda p: (order.get(p["service"], len(order)), p["name"].lower()))


# ---- offers ----

_OFFER_FIELDS = """standardWebURL monetizationType presentationType videoTechnology audioLanguages
  subtitleLanguages package { shortName technicalName clearName }"""
_SHORT_OFFER = "standardWebURL monetizationType presentationType package { shortName technicalName clearName }"


def _title_pattern_matches(service, url):
    """Whether the service's own TITLE_RE reads this link."""
    try:
        module = importlib.import_module(f"vinetrimmer.services.{service.lower()}")
        patterns = getattr(getattr(module, service), "TITLE_RE", None)
    except Exception:
        return False
    if not patterns:
        return True
    if isinstance(patterns, (str, re.Pattern)):
        patterns = [patterns]
    return any(re.search(p, url) for p in patterns)


def _access(offer):
    if _is_disc(offer):
        return "disc"
    access = _ACCESS.get(offer.get("monetizationType"))
    if access == "included" and "Channel" in ((offer.get("package") or {}).get("clearName") or ""):
        return "channel"
    return access


def _is_disc(offer):
    """A shop selling the DVD or Blu-ray: worth a link, but nothing to download."""
    return (offer.get("presentationType") or "").lstrip("_").startswith(("DVD", "BLURAY"))


def _offers(raw, kind, country):
    """JustWatch lists one offer per package and quality; this groups them into one row
    per link a service can use."""
    installed = _installed()
    rows = OrderedDict()
    for offer in raw or []:
        package = offer.get("package") or {}
        monetization = offer.get("monetizationType") or ""
        if monetization in _CINEMA:
            continue
        link = discover_links.normalize(offer.get("standardWebURL"), kind, country, package.get("technicalName") or "")
        if not link or link["service"] not in installed:
            continue
        disc = _is_disc(offer)
        key = (link["service"], link["url"], disc)
        row = rows.get(key)
        if not row:
            usable = not disc and _title_pattern_matches(link["service"], link["url"])
            note = link["note"]
            if disc:
                link = {**link, "resolve": False}
                note = "A physical disc: there is nothing to download."
            elif not usable and not link["resolve"] and not note:
                note = "The downloader doesn't read this link format. Open it and copy the title's link."
            row = rows[key] = {**link, "note": note, "usable": usable or link["resolve"],
                               "web_url": offer.get("standardWebURL"), "providers": [], "monetization": [], "access": [],
                               "quality": [], "video": [], "audio": [], "subtitles": []}
        for field, value in (("providers", package.get("clearName")), ("monetization", monetization),
                             ("access", _access(offer)),
                             ("quality", (offer.get("presentationType") or "").lstrip("_"))):
            if value and value not in row[field]:
                row[field].append(value)
        for field, values in (("video", offer.get("videoTechnology")), ("audio", offer.get("audioLanguages")),
                              ("subtitles", offer.get("subtitleLanguages"))):
            row[field].extend(v for v in values or [] if v and v not in row[field])

    for row in rows.values():
        row["access"].sort(key=_ACCESS_ORDER.index)

    def rank(row):
        paid = bool(row["access"]) and all(a in ACCESS_FILTERS["paid"] for a in row["access"])
        return ("disc" in row["access"], not row["usable"], paid, not row["exact"], any("Channel" in p for p in row["providers"]))

    return sorted(rows.values(), key=rank)


# ---- listings ----

_CONTENT = "content(country: $country, language: $language) { title originalReleaseYear posterUrl shortDescription }"

_POPULAR_QUERY = f"""query Popular($country: Country!, $language: Language!, $first: Int!, $offset: Int,
    $filter: TitleFilter, $sortBy: PopularTitlesSorting!) {{
  popularTitles(country: $country, first: $first, offset: $offset, filter: $filter, sortBy: $sortBy) {{
    totalCount
    pageInfo {{ hasNextPage }}
    edges {{ node {{ id objectType {_CONTENT} offers(country: $country, platform: WEB) {{ {_SHORT_OFFER} }} }} }}
  }}
}}"""

# A lighter listing for collecting a whole catalogue: no offers, no descriptions.
_CATALOGUE_QUERY = """query Catalogue($country: Country!, $language: Language!, $first: Int!, $offset: Int,
    $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, offset: $offset, filter: $filter, sortBy: POPULAR) {
    totalCount
    edges { node { id objectType content(country: $country, language: $language) { title originalReleaseYear posterUrl } } }
  }
}"""

_NEW_QUERY = f"""query New($country: Country!, $language: Language!, $date: Date!, $first: Int!, $after: String,
    $filter: TitleFilter) {{
  newTitles(country: $country, date: $date, first: $first, after: $after, filter: $filter, priceDrops: false,
      pageType: NEW) {{
    pageInfo {{ endCursor hasNextPage }}
    edges {{
      newOffer(platform: WEB) {{ {_SHORT_OFFER} newElementCount }}
      node {{
        ... on Movie {{ id objectType {_CONTENT} }}
        ... on Season {{ id objectType totalEpisodeCount
          content(country: $country, language: $language) {{ seasonNumber posterUrl shortDescription }}
          show {{ id {_CONTENT} }} }}
      }}
    }}
  }}
}}"""


def _service_of(offer, kind, country, access=None):
    """The installed service one offer belongs to, if it fits the price filter."""
    if _is_disc(offer) or (access and _access(offer) not in ACCESS_FILTERS[access]):
        return None
    package = offer.get("package") or {}
    link = discover_links.normalize(offer.get("standardWebURL"), kind, country, package.get("technicalName") or "")
    return link["service"] if link and link["service"] in _installed() else None


def _filter(country, services, kind, query=None, access=None):
    available = providers(country)
    wanted = [p for p in available if not services or p["service"] in services]
    if not wanted:
        raise DiscoverError("None of the selected services is available in this country.")
    out = {"packages": [short for p in wanted for short in p["packages"]]}
    if kind == "movie":
        out["objectTypes"] = ["MOVIE"]
    elif kind == "show":
        out["objectTypes"] = ["SHOW"]
    if access:
        out["monetizationTypes"] = [m for m, a in _ACCESS.items() if a in ACCESS_FILTERS[access]]
    if query:
        out["searchQuery"] = query
        # A search is about finding the title: show it wherever it is. (Not with a price
        # filter, which is about what your services offer.)
        if not services and not access:
            del out["packages"]
    return out


def _title_item(node):
    content = node.get("content") or {}
    return {
        "id": node["id"], "kind": "movie" if node.get("objectType") == "MOVIE" else "show",
        "title": content.get("title"), "year": content.get("originalReleaseYear"),
        "poster": _image(content.get("posterUrl")), "description": content.get("shortDescription"),
    }


def _services_of(offers, kind, country, access=None):
    found = []
    for offer in offers or []:
        service = _service_of(offer, kind, country, access)
        if service and service not in found:
            found.append(service)
    return found


def popular(country, language, services=None, kind=None, query=None, offset=0, access=None):
    """Popular titles, or the search results for query."""
    data = _gql(_POPULAR_QUERY, {
        "country": country, "language": language, "first": PAGE_SIZE, "offset": offset,
        "sortBy": "POPULAR", "filter": _filter(country, services, kind, query, access),
    })["popularTitles"]
    items = []
    for edge in data["edges"]:
        item = _title_item(edge["node"])
        item["services"] = _services_of(edge["node"].get("offers"), item["kind"], country, access)
        items.append(item)
    after = offset + PAGE_SIZE
    more = data["pageInfo"]["hasNextPage"]
    # JustWatch answers nothing once a page would reach past its limit.
    reachable = after + PAGE_SIZE <= _REACHABLE
    return {"items": items, "next": after if more and reachable else None,
            "total": data["totalCount"], "limited": bool(more and not reachable)}


# ---- the whole catalogue, sorted by name ----

SORTS = ("az", "za")
# Each title costs a little over 1/100th of a request, so this is about 400 of them.
MAX_SORTABLE = 40000
_REACHABLE = 1999  # JustWatch only answers while offset + first stays at or below this
_CATALOGUE_PAGE = 100
_CATALOGUE_SECONDS = 60 * 60
_catalogues = OrderedDict()  # key -> (time, titles sorted A to Z)
_catalogue_locks = {}


# Letters that don't come apart into a plain letter plus an accent.
_PLAIN_LETTERS = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "æ": "ae", "œ": "oe", "ı": "i"})


def _sort_key(title):
    """A to Z the way people read it: ignoring case, accents and punctuation, 2 before 10."""
    text = unicodedata.normalize("NFKD", title or "").casefold().translate(_PLAIN_LETTERS)
    text = "".join(c for c in text if c.isalnum() or c.isspace())
    return [(0, int(part), "") if part.isdecimal() else (1, 0, part)
            for part in re.split(r"(\d+)", " ".join(text.split())) if part]


def _other_script(title):
    """Whether a title starts in a script A to Z says nothing about (Korean, Cyrillic, ...)."""
    first = next((c for c in title or "" if c.isalpha() or c.isdecimal()), "")
    return first.isalpha() and not unicodedata.name(first, "").startswith(("LATIN", "FULLWIDTH LATIN"))


def _ordered(titles, sort):
    """titles (sorted A to Z) in the order asked for. Other scripts stay at the end both ways."""
    latin = [t for t in titles if not _other_script(t["title"])]
    other = [t for t in titles if _other_script(t["title"])]
    return (latin[::-1] if sort == "za" else latin) + other


def _catalogue_page(country, language, wanted, offset):
    variables = {"country": country, "language": language, "first": min(_CATALOGUE_PAGE, _REACHABLE - offset),
                 "offset": offset, "filter": wanted}
    for attempt in (1, 2, 3):
        try:
            return _gql(_CATALOGUE_QUERY, variables, cache=False)["popularTitles"]
        except DiscoverError:
            if attempt == 3:
                raise
            time.sleep(attempt)


def _collect(country, language, wanted):
    """Every title matching the filter. One listing stops at _REACHABLE titles, so a
    larger catalogue is fetched in ranges of release years."""
    first = _catalogue_page(country, language, wanted, 0)
    total = first["totalCount"]
    if total > MAX_SORTABLE:
        raise DiscoverError(
            f"That's {total:,} titles, too many to sort by name (the limit is {MAX_SORTABLE:,}). "
            "Pick fewer services, only Movies or Shows, or only Subscription & free.")
    found = {}

    def add(page):
        for edge in page["edges"]:
            found[edge["node"]["id"]] = edge["node"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        def fetch(parts):
            """parts: [(filter, first page)] that each fit into one listing."""
            jobs = []
            for part, page in parts:
                add(page)
                end = min(page["totalCount"], _REACHABLE)
                jobs += [pool.submit(_catalogue_page, country, language, part, offset)
                         for offset in range(_CATALOGUE_PAGE, end, _CATALOGUE_PAGE)]
            for job in jobs:
                add(job.result())

        if total <= _REACHABLE:
            fetch([(wanted, first)])
        else:
            ranges = [(1870, date.today().year + 5)]
            while ranges:
                parts = [{**wanted, "releaseYear": {"min": low, "max": high}} for low, high in ranges]
                pages = list(pool.map(lambda part: _catalogue_page(country, language, part, 0), parts))
                fits, split = [], []
                for (low, high), part, page in zip(ranges, parts, pages):
                    if page["totalCount"] <= _REACHABLE:
                        fits.append((part, page))
                    elif low < high:
                        middle = (low + high) // 2
                        split += [(low, middle), (middle + 1, high)]
                    else:
                        # one very full year: movies and shows separately
                        for object_type in part.get("objectTypes") or ["MOVIE", "SHOW"]:
                            narrower = {**part, "objectTypes": [object_type]}
                            fits.append((narrower, _catalogue_page(country, language, narrower, 0)))
                fetch(fits)
                ranges = split
    return [_title_item(node) for node in found.values()]


def _catalogue(country, language, wanted):
    key = json.dumps([country, language, wanted], sort_keys=True)
    with _lock:
        building = _catalogue_locks.setdefault(key, threading.Lock())
    with building:  # a second request for the same list waits instead of fetching it too
        with _lock:
            hit = _catalogues.get(key)
        if hit and time.time() - hit[0] < _CATALOGUE_SECONDS:
            return hit[1]
        titles = sorted(_collect(country, language, wanted),
                        key=lambda t: (_sort_key(t["title"]), t["year"] or 0, t["id"]))
        with _lock:
            _catalogues[key] = (time.time(), titles)
            _catalogues.move_to_end(key)
            while len(_catalogues) > 4:
                _catalogues.popitem(last=False)
        return titles


def _services_for(items, country, access=None):
    """{title id: services} for the titles on one page, in a single request."""
    if not items:
        return {}
    offers = f"offers(country: $country, platform: WEB) {{ {_SHORT_OFFER} }}"
    names = ", ".join(f"$id{i}: ID!" for i in range(len(items)))
    nodes = "\n".join(f"n{i}: node(id: $id{i}) {{ ... on Movie {{ {offers} }} ... on Show {{ {offers} }} }}"
                      for i in range(len(items)))
    variables = {"country": country, **{f"id{i}": item["id"] for i, item in enumerate(items)}}
    data = _gql(f"query Offers($country: Country!, {names}) {{\n{nodes}\n}}", variables)
    return {item["id"]: _services_of((data.get(f"n{i}") or {}).get("offers"), item["kind"], country, access)
            for i, item in enumerate(items)}


def browse(country, language, services=None, kind=None, sort=None, offset=0, access=None):
    """Everything on the services: in JustWatch's order, or sorted by name ("az" / "za")."""
    if sort not in SORTS:
        return popular(country, language, services, kind, None, offset, access)
    titles = _ordered(_catalogue(country, language, _filter(country, services, kind, access=access)), sort)
    page = titles[offset:offset + PAGE_SIZE]
    found = _services_for(page, country, access)
    after = offset + PAGE_SIZE
    return {"items": [{**item, "services": found.get(item["id"], [])} for item in page],
            "next": after if after < len(titles) else None, "total": len(titles), "limited": False}


def new(country, language, services=None, kind=None, day=None, cursor=None, access=None):
    """What the services added on one day (default today). next / earlier say how to
    get the rest of that day, and the day before it."""
    try:
        day = date.fromisoformat(day) if day else date.today()
    except ValueError:
        raise DiscoverError("Invalid date.")
    wanted = _filter(country, services, kind, access=access)
    if kind == "show":
        wanted["objectTypes"] = ["SHOW_SEASON"]  # new shows are listed season by season
    data = _gql(_NEW_QUERY, {
        "country": country, "language": language, "date": day.isoformat(), "first": PAGE_SIZE,
        "after": cursor, "filter": wanted,
    })["newTitles"]
    items = OrderedDict()
    for edge in data["edges"]:
        node, offer = edge.get("node") or {}, edge.get("newOffer") or {}
        if not node.get("id"):
            continue
        if node.get("objectType") == "SHOW_SEASON":
            show = node.get("show") or {}
            content = show.get("content") or {}
            season = node.get("content") or {}
            item = {"id": show.get("id"), "kind": "show", "season": season.get("seasonNumber"),
                    "new_episodes": offer.get("newElementCount"),
                    "poster": _image(season.get("posterUrl") or content.get("posterUrl")),
                    "description": season.get("shortDescription") or content.get("shortDescription")}
        else:
            content = node.get("content") or {}
            item = {"id": node["id"], "kind": "movie", "poster": _image(content.get("posterUrl")),
                    "description": content.get("shortDescription")}
        item.update(title=content.get("title"), year=content.get("originalReleaseYear"), services=[])
        item = items.setdefault((item["id"], item.get("season")), item)
        service = _service_of(offer, item["kind"], country, access)
        if service and service not in item["services"]:
            item["services"].append(service)
    page = data["pageInfo"]
    return {
        "items": list(items.values()), "date": day.isoformat(),
        "next": page.get("endCursor") if page.get("hasNextPage") else None,
        "earlier": (day - timedelta(days=1)).isoformat(),
    }


# ---- one title ----

_DETAIL_QUERY = f"""query Detail($id: ID!, $country: Country!, $language: Language!) {{
  node(id: $id) {{
    ... on Movie {{ id objectType {_CONTENT} offers(country: $country, platform: WEB) {{ {_OFFER_FIELDS} }} }}
    ... on Show {{ id objectType {_CONTENT} offers(country: $country, platform: WEB) {{ {_OFFER_FIELDS} }}
      seasons {{ id totalEpisodeCount content(country: $country, language: $language) {{ seasonNumber }}
        offers(country: $country, platform: WEB) {{ {_SHORT_OFFER} }} }} }}
  }}
}}"""

_EPISODES_QUERY = f"""query Episodes($id: ID!, $country: Country!, $language: Language!) {{
  node(id: $id) {{ ... on Season {{ episodes {{
    content(country: $country, language: $language) {{ episodeNumber title }}
    offers(country: $country, platform: WEB) {{ {_SHORT_OFFER} }}
  }} }} }}
}}"""

_ID = re.compile(r"^[a-z]{2,3}\d+$")


def _check_id(value):
    if not _ID.match(value or ""):
        raise DiscoverError("Unknown title.")


def details(title_id, country, language):
    _check_id(title_id)
    node = _gql(_DETAIL_QUERY, {"id": title_id, "country": country, "language": language}).get("node")
    if not node or not node.get("content"):
        raise DiscoverError("JustWatch doesn't know this title (any more).")
    content = node["content"]
    kind = "movie" if node.get("objectType") == "MOVIE" else "show"
    seasons = []
    for season in node.get("seasons") or []:
        number = (season.get("content") or {}).get("seasonNumber")
        if number is None:
            continue
        seasons.append({
            "id": season["id"], "number": number, "episodes": season.get("totalEpisodeCount"),
            # Some services (Amazon) give every season its own link.
            "links": {o["service"]: o["url"] for o in reversed(_offers(season.get("offers"), kind, country))
                      if o["usable"]},
        })
    return {
        "id": node["id"], "kind": kind, "title": content.get("title"),
        "year": content.get("originalReleaseYear"), "poster": _image(content.get("posterUrl"), "s592"),
        "description": content.get("shortDescription"),
        "offers": _offers(node.get("offers"), kind, country),
        "seasons": sorted(seasons, key=lambda s: s["number"]),
    }


def episodes(season_id, country, language):
    _check_id(season_id)
    node = _gql(_EPISODES_QUERY, {"id": season_id, "country": country, "language": language}).get("node") or {}
    out = []
    for episode in node.get("episodes") or []:
        content = episode.get("content") or {}
        if content.get("episodeNumber") is None:
            continue
        out.append({"number": content["episodeNumber"], "title": content.get("title"),
                    "services": sorted({s for s in (_service_of(o, "show", country)
                                                   for o in episode.get("offers") or []) if s})})
    return {"episodes": sorted(out, key=lambda e: e["number"])}
