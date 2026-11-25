"""
A request names a service, a title and the wanted codec (H.265 if none is given, with
H.264 SDR as a last resort) / range / resolution. A worker
thread runs the same availability check as the Download page for it, one request at a
time. If the wanted variant is missing, it goes on to the fallbacks - by default SDR in
the same codec - and offers that in the wanted resolution, or the next one below (4K HDR
missing: SDR 4K, otherwise SDR 1080p). Nothing is
downloaded until the offer is confirmed; then it becomes a normal queue job.

Requests live in memory only: after a restart, unconfirmed ones have to be sent again.
"""
import re
import threading
import time
import uuid

from . import api_settings, command_builder, discover_links, probe, retry

CODECS = {"h264": "H.264", "h265": "H.265", "av1": "AV1", "vp9": "VP9"}
RANGE_LABELS = {"sdr": "SDR", "hdr10": "HDR10", "hlg": "HLG", "dv": "DV", "dvhdr": "DV+HDR",
                "hybrid": "Hybrid (HDR10 + DV)", "hybridplus": "Hybrid+ (HDR10+ with DV)"}
_CODEC_ALIASES = {"avc": "h264", "x264": "h264", "h.264": "h264", "hevc": "h265", "x265": "h265", "h.265": "h265"}
_RANGE_ALIASES = {"hdr": "hdr10", "hdr10+": "hdr10", "hdr10plus": "hdr10", "dolbyvision": "dv", "dolby-vision": "dv",
                  "dv+hdr": "dvhdr", "hybrid+": "hybridplus", "hybrid-plus": "hybridplus"}
# What's tried when the wanted range isn't there (any kind of HDR): SDR in the same
# codec, 4K first, then 1080p. A request can give its own order with "fallback"
# (e.g. ["hdr10", "sdr"]), or [] for none.
DEFAULT_FALLBACK = ("sdr",)
# Which track ranges (as the check reports them) satisfy a requested range.
_TRACK_RANGES = {"sdr": ("SDR",), "hdr10": ("HDR10", "HDR10+"), "hlg": ("HLG",), "dv": ("DV", "DV+HDR"),
                 "dvhdr": ("DV+HDR",), "hybrid": ("HDR10", "HDR10+"), "hybridplus": ("HDR10+",)}
_QUALITY = {"best": None, "max": None, "4k": 2160, "uhd": 2160, "qhd": 1440, "fhd": 1080, "fullhd": 1080, "hd": 720}

OPEN = ("queued", "waiting", "checking")
MAX_OPEN = 25
MAX_KEPT = 200
OFFER_SECONDS = 24 * 3600
WAIT_LIMIT = 6 * 3600
# A failed run that says this won't go better with another codec / range.
_LOGIN_PROBLEM = re.compile(r"log ?in|credential|password|cookie|authori[sz]|unauthenticated|\b401\b|\bcdm\b|"
                            r"no title|unable to get title|geofence|proxy", re.I)
_WANTED_TOKEN = re.compile(r"^-?S(\d{1,3})(E\d{1,4})?(?:[-:]S(\d{1,3})(E\d{1,4})?)?$", re.I)
_LANGS = re.compile(r"^[A-Za-z0-9-]{1,20}(,[A-Za-z0-9-]{1,20}){0,19}$")
MAX_SEASON_SPAN = 60


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---- reading a request ----

def _codec(value, default="h265"):
    value = str(value or default).strip().lower()
    value = _CODEC_ALIASES.get(value, value)
    if value not in CODECS:
        raise RequestError(f"Unknown codec '{value}'. Use one of: {', '.join(CODECS)}.")
    return value


def _range(value):
    value = str(value or "sdr").strip().lower().replace(" ", "")
    value = _RANGE_ALIASES.get(value, value)
    if value not in RANGE_LABELS:
        raise RequestError(f"Unknown range '{value}'. Use one of: {', '.join(RANGE_LABELS)}.")
    return value


def parse_quality(value):
    if value in (None, ""):
        return None
    text = str(value).strip().lower().rstrip("p")
    if text in _QUALITY:
        return _QUALITY[text]
    if text.isdigit() and 100 <= int(text) <= 4320:
        return int(text)
    raise RequestError("quality: use best, 4k, 2160, 1440, 1080, 720 or another height.")


def possible(service, codec, range_):
    """Whether this codec / range combination can exist on this service at all."""
    if range_ == "hybridplus":
        return service == "Netflix" and codec == "av1"
    if service == "Netflix":
        return f"{codec}:{range_}" in probe.NETFLIX_COMBOS
    return range_ == "sdr" or codec != "h264"


def parse_wanted(data):
    wanted = data.get("wanted")
    if not wanted and data.get("season") not in (None, ""):
        try:
            season = int(data["season"])
            episode = int(data["episode"]) if data.get("episode") not in (None, "") else None
        except (TypeError, ValueError):
            raise RequestError("season and episode must be numbers.")
        wanted = f"S{season:02}" + (f"E{episode:02}" if episode is not None else "")
    elif not wanted and data.get("episode") not in (None, ""):
        text = str(data["episode"]).strip()
        if not text.upper().startswith("S"):
            raise RequestError("Give a season too (or episode as S02E05).")
        wanted = text
    if not wanted:
        return None
    tokens = [t for t in re.split(r"\s*[,;]\s*", str(wanted).strip()) if t]
    for token in tokens:
        found = _WANTED_TOKEN.match(token)
        if not found or len(tokens) > 40 or \
                (found.group(3) and not 0 <= int(found.group(3)) - int(found.group(1)) <= MAX_SEASON_SPAN):
            raise RequestError("wanted: use e.g. S02, S02E05, S01-S03 or S01E01-S01E04.")
    return ",".join(tokens).upper()


def _languages(data, key):
    value = data.get(key)
    if isinstance(value, list):
        value = ",".join(str(v) for v in value)
    value = str(value or "").replace(" ", "")
    if not value:
        return None
    if not _LANGS.match(value):
        raise RequestError(f"{key}: use language codes separated by commas, e.g. de,en (or orig / all).")
    return value


def parse(data):
    """The cleaned request, or RequestError."""
    from . import services_info  # lazy: see queue_manager.retry()
    if not isinstance(data, dict):
        raise RequestError("Send a JSON object.")
    name = str(data.get("service") or "").strip()
    info = services_info.get_service_options(name) if name else None
    if not info:
        raise RequestError(f"Unknown service '{name}'. GET /api/v1/services lists them." if name
                           else "service is missing.")
    service = info["name"]
    title = str(data.get("title") or data.get("link") or data.get("id") or "").strip()
    if not title:
        raise RequestError("title is missing (a link or ID; 'link' and 'id' work too).")
    # The title ends up on a command line as its own argument: it must not look like an option.
    if title.startswith("-") or len(title) > 500 or any(c.isspace() or ord(c) < 32 for c in title):
        raise RequestError("title must be a link or ID without spaces.")
    title = discover_links.title_value(service, title)
    range_ = _range(data.get("range"))
    given = data.get("codec") or data.get("vcodec")
    # Like the Download page: H.265 unless asked otherwise (4K and HDR are only there),
    # and H.264 if a title has no H.265. Netflix's Hybrid+ only exists in AV1.
    codec = _codec(given, "av1" if range_ == "hybridplus" else "h265")
    if not possible(service, codec, range_):
        raise RequestError(f"{CODECS[codec]} {RANGE_LABELS[range_]} doesn't exist on {service}.")

    fallback = data.get("fallback")
    if fallback is None:
        chain = [(codec, r) for r in DEFAULT_FALLBACK]
    elif isinstance(fallback, list) and len(fallback) <= 8:
        chain = []
        for item in fallback:
            if isinstance(item, dict):
                chain.append((_codec(item.get("codec"), codec), _range(item.get("range"))))
            else:
                chain.append((codec, _range(item)))
    else:
        raise RequestError("fallback must be a list, e.g. [\"sdr\"] or [{\"codec\": \"h265\", \"range\": \"sdr\"}].")
    if not given and codec == "h265" and fallback is None:
        chain.append(("h264", "sdr"))
    candidates = []
    for candidate in [(codec, range_), *chain]:
        if candidate not in candidates and possible(service, *candidate):
            candidates.append(candidate)

    return {
        "service": service, "title": title, "codec": codec, "range": range_, "codec_given": bool(given),
        "quality": parse_quality(data.get("quality")), "wanted": parse_wanted(data),
        "audio_lang": _languages(data, "audio_lang"), "sub_lang": _languages(data, "sub_lang"),
        "check_keys": bool(data.get("check_keys")), "candidates": candidates,
    }


# ---- reading a check result ----

def tier(width, height):
    """The resolution class a track counts as: a 3840x1600 picture is 4K too."""
    for name, min_width, min_height in ((2160, 3200, 1700), (1440, 2400, 1300), (1080, 1800, 1000), (720, 1200, 700)):
        if (width or 0) >= min_width or (height or 0) >= min_height:
            return name
    return height or 0


def tracks_for(videos, service, codec, range_, max_height=None):
    """The video tracks that give codec + range, best first - empty if it isn't there
    (the hybrids also need their second half)."""
    name = CODECS[codec]

    def having(track_codec, ranges):
        return [v for v in videos if v["codec"] == track_codec and v["range"] in ranges
                and not (max_height and (v["height"] or 0) > max_height)]

    if not possible(service, codec, range_):
        return []
    if range_ == "hybrid" and not having(name, _TRACK_RANGES["dv"]):
        return []
    if range_ == "hybridplus" and not having(CODECS["h265"], _TRACK_RANGES["dv"]):
        return []
    return sorted(having(name, _TRACK_RANGES[range_]),
                  key=lambda v: (tier(v["width"], v["height"]), v["bitrate"] or 0), reverse=True)


def option(service, codec, range_, track):
    resolution = tier(track["width"], track["height"])
    label = RANGE_LABELS[range_]
    if range_ == "hdr10" and track["range"] == "HDR10+":
        label = "HDR10+"
    return {
        "id": f"{codec}:{range_}:{resolution}", "codec": codec, "range": range_,
        "label": f"{CODECS[codec]} {label} {resolution}p",
        "resolution": f"{resolution}p", "tier": resolution, "width": track["width"], "height": track["height"],
        "bitrate_kbps": track["bitrate"],
    }


def all_options(videos, service, max_height=None):
    """Every codec / range / resolution that can be downloaded, best resolution first."""
    out = []
    for codec in CODECS:
        for range_ in RANGE_LABELS:
            seen = set()
            for track in tracks_for(videos, service, codec, range_, max_height):
                resolution = tier(track["width"], track["height"])
                if resolution not in seen:
                    seen.add(resolution)
                    out.append(option(service, codec, range_, track))
    return out


def pick(videos, service, codec, range_, quality, max_height=None):
    """(option, whether it has the wanted resolution) for one candidate, or None."""
    tracks = tracks_for(videos, service, codec, range_, max_height)
    if not tracks:
        return None
    if quality is None:
        return option(service, codec, range_, tracks[0]), True
    wanted = tier(0, quality)
    fitting = [t for t in tracks if tier(t["width"], t["height"]) == wanted]
    if fitting:
        return option(service, codec, range_, fitting[0]), True
    # Otherwise the closest one below it; if everything is above, the smallest.
    below = [t for t in tracks if tier(t["width"], t["height"]) < wanted]
    return option(service, codec, range_, below[0] if below else tracks[-1]), False


# ---- the requests ----

class Request:
    def __init__(self, spec):
        self.id = uuid.uuid4().hex[:16]
        self.spec = spec
        self.status = "queued"
        self.message = "Waiting to be checked."
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.title = None
        self.offer = None
        self.exact = False
        self.options = []
        self.audio_languages = []
        self.subtitle_languages = []
        self.warnings = []
        self.forced_options = {}
        self.keys = None
        self.job_id = None
        self.expires_at = None
        self.cancelled = False
        self.probe = None
        self.done = threading.Event()  # set once there's an answer (or the request is closed)

    def set(self, status, message):
        self.status, self.message, self.updated_at = status, message, time.time()
        if status not in OPEN:
            self.done.set()

    def to_dict(self):
        spec = self.spec
        return {
            "id": self.id, "status": self.status, "message": self.message,
            "service": spec["service"], "input": spec["title"],
            "requested": {
                "codec": spec["codec"], "range": spec["range"],
                "quality": spec["quality"] or "best", "wanted": spec["wanted"],
                "audio_lang": spec["audio_lang"], "sub_lang": spec["sub_lang"],
            },
            "title": self.title, "exact": self.exact, "offer": self.offer, "options": self.options,
            "audio_languages": self.audio_languages, "subtitle_languages": self.subtitle_languages,
            "keys": self.keys, "warnings": self.warnings, "job_id": self.job_id,
            "created_at": self.created_at, "expires_at": self.expires_at,
        }


class _Cancelled(Exception):
    pass


class Requests:
    def __init__(self, job_queue):
        self.queue = job_queue
        self._lock = threading.RLock()
        self._requests = {}
        self._wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- api side ----

    def create(self, data):
        spec = parse(data)
        # Same validation as a job made on the Download page.
        self._argv(self._form(spec, spec["codec"], spec["range"]))
        with self._lock:
            self._forget_old()
            if sum(r.status in OPEN for r in self._requests.values()) >= MAX_OPEN:
                raise RequestError("Too many requests are waiting to be checked. Try again later.", 429)
            request = Request(spec)
            self._requests[request.id] = request
        self._wake.set()
        return request

    def get(self, request_id):
        with self._lock:
            self._forget_old()
            return self._requests.get(request_id)

    def list(self):
        with self._lock:
            self._forget_old()
            return sorted(self._requests.values(), key=lambda r: r.created_at, reverse=True)

    def decline(self, request_id):
        with self._lock:
            request = self.get(request_id)
            if not request:
                raise RequestError("Unknown request.", 404)
            if request.status == "confirmed":
                raise RequestError("This request is already in the download queue.", 409)
            request.cancelled = True
            check = request.probe
            request.set("declined", "Declined. Nothing will be downloaded.")
        if check and check.status == "running":
            check.cancel()
        return request

    def confirm(self, request_id, data=None):
        """Queue the offered download - or another entry of "options" by its id."""
        data = data or {}
        with self._lock:
            request = self.get(request_id)
            if not request:
                raise RequestError("Unknown request.", 404)
            if request.status not in ("awaiting_confirmation", "unavailable") or not request.options:
                raise RequestError(f"This request can't be confirmed (status: {request.status}).", 409)
            wanted_id = data.get("option")
            if wanted_id:
                chosen = next((o for o in request.options if o["id"] == wanted_id), None)
                if not chosen:
                    raise RequestError("Unknown option. Use an id from \"options\".")
            elif request.offer:
                chosen = request.offer
            else:
                raise RequestError("There's no offer. Pick one with \"option\".", 409)
            spec = dict(request.spec)
            if data.get("wanted") or data.get("season") not in (None, ""):
                spec["wanted"] = parse_wanted(data)
            form = self._form(spec, chosen["codec"], chosen["range"])
            if spec["service"] == "Netflix" and chosen["tier"] <= 1080:
                form["svc__drm_system"] = "widevine"
            form.update(request.forced_options)
            # -q only when a smaller picture than the best one is meant.
            best = max(o["tier"] for o in request.options
                       if o["codec"] == chosen["codec"] and o["range"] == chosen["range"])
            # -q is needed for a smaller picture than the best one - and outside Netflix
            # for 4K too: without it a service like Amazon only asks for HD.
            if chosen["tier"] < best or (spec["service"] != "Netflix" and chosen["tier"] > 1080):
                form["quality"] = str(chosen["height"])
            if request.title:
                form["meta"] = {k: request.title.get(k) for k in ("name", "year", "poster")}
            argv = self._argv(form)
            job = self.queue.add(
                service=spec["service"], title=spec["title"], argv=argv,
                command=command_builder.format_command(argv), form=form)
            request.job_id = job.id
            request.offer = chosen
            request.expires_at = None
            idle = self.queue.state()["state"] == "idle"
            start = api_settings.get()["auto_start"] and idle
            request.set("confirmed", f"{chosen['label']} is in the download queue"
                        + (" and starts now." if start else "."))
        if start:
            self.queue.start()
        return request

    def job(self, request_id):
        """The request's queue job (or the newest retry of it), or None."""
        request = self.get(request_id)
        job = self.queue.get(request.job_id) if request and request.job_id else None
        while job and job.retried_by and self.queue.get(job.retried_by):
            job = self.queue.get(job.retried_by)
        return job

    # ---- worker ----

    def _forget_old(self):
        now = time.time()
        for r in list(self._requests.values()):
            if r.status == "awaiting_confirmation" and r.expires_at and r.expires_at < now:
                r.set("expired", "The offer ran out. Send the request again.")
        closed = sorted((r for r in self._requests.values()
                         if r.status not in OPEN and r.status != "awaiting_confirmation"),
                        key=lambda r: r.updated_at)
        too_many = len(self._requests) - MAX_KEPT
        for r in closed:
            if too_many > 0 or now - r.updated_at > 2 * OFFER_SECONDS:
                del self._requests[r.id]
                too_many -= 1

    def _blocked(self, request):
        """Why this request's check has to wait, or None."""
        if probe.active() is not None:
            return "Waiting for another availability check to finish."
        service = request.spec["service"]
        if api_settings.get()["wait_same_service"] and self.queue.is_busy(service):
            return f"Waiting: {service} is downloading. The check runs when that download is done."
        return None

    def _worker(self):
        while True:
            self._wake.wait(3)
            self._wake.clear()
            try:
                self._work()
            except Exception:
                time.sleep(3)  # keep serving whatever one request's problem was

    def _work(self):
        with self._lock:
            pending = sorted((r for r in self._requests.values() if r.status in ("queued", "waiting")),
                             key=lambda r: r.created_at)
        # The first one that can run: a blocked service doesn't hold the others up.
        for request in pending:
            reason = self._blocked(request)
            if not reason:
                break
            if time.time() - request.created_at > WAIT_LIMIT:
                request.set("failed", "Gave up waiting for the check to become possible.")
            else:
                with self._lock:
                    if not request.cancelled:
                        request.set("waiting", reason)
        else:
            return
        with self._lock:
            if request.cancelled:
                return
            request.set("checking", "Checking what's available…")
        try:
            self._check(request)
        except _Cancelled:
            pass
        except Exception as e:
            request.set("failed", f"The check failed: {type(e).__name__}: {e}")
        self._wake.set()

    def _form(self, spec, codec, range_):
        form = {"service": spec["service"], "title": spec["title"], "vcodec": codec, "range_": range_}
        if spec["wanted"]:
            form["wanted"] = spec["wanted"]
        if spec["audio_lang"]:
            form["alang"] = spec["audio_lang"]
        if spec["sub_lang"]:
            form["slang"] = spec["sub_lang"]
        # Netflix: Widevine reaches 1080p and always gets its keys; 4K needs PlayReady.
        if spec["service"] == "Netflix" and spec["quality"] and spec["quality"] <= 1080:
            form["svc__drm_system"] = "widevine"
        return form

    @staticmethod
    def _argv(form):
        from . import services_info  # lazy: see queue_manager.retry()
        info = services_info.get_service_options(form["service"])
        return command_builder.build_argv(form, form["service"], info["options"] if info else [])

    def _run_probe(self, request, form, mode, **kwargs):
        """Start a check as soon as no other one runs, and wait for it."""
        deadline = time.time() + WAIT_LIMIT
        while True:
            with self._lock:
                if request.cancelled:
                    raise _Cancelled()
                check = probe.start(form, mode, **kwargs)
                if check == "nothing":
                    return None
                if check:
                    request.probe = check
                    break
            if time.time() > deadline:
                raise RuntimeError("another availability check never finished")
            time.sleep(2)
        while check.finished_at is None:
            if request.cancelled:
                check.cancel()
            time.sleep(1)
        if request.cancelled or check.status == "cancelled":
            if not request.cancelled:
                request.set("failed", "The check was cancelled in the web UI.")
            raise _Cancelled()
        return check

    def _check(self, request):
        spec = request.spec
        service, quality = spec["service"], spec["quality"]
        netflix = service == "Netflix"
        check_quality = "1080" if netflix and quality and quality <= 1080 else "2160" if netflix else None
        max_height = 1080 if check_quality == "1080" else None
        episode = re.match(r"S\d+(E\d+)?", spec["wanted"] or "")

        last = None  # the latest check that returned a track list
        empty_runs = 0
        lower = []  # candidates that exist, but not in the wanted resolution
        chosen = None
        error = None
        for index, (codec, range_) in enumerate(spec["candidates"]):
            if codec == "h264" and not spec["codec_given"] and lower:
                break  # H.264 is only looked at if H.265 has nothing to offer
            videos = last.result["video"] if last else []
            request.set("checking", f"Checking {CODECS[codec]} {RANGE_LABELS[range_]}…")
            form = self._form(spec, codec, range_)
            if episode:
                form["check_episode"] = episode.group(0)
            extend = last.id if last else None
            if netflix:
                check = self._run_probe(request, form, "combos", combos=[f"{codec}:{range_}"],
                                        quality=check_quality, extend=extend)
            else:
                # The hybrids are listed by their own -r value; SDR is the default listing.
                run = (codec, None if range_ == "sdr" else range_)
                list_quality = "2160" if codec == "h265" and (quality is None or quality > 1080) else None
                check = self._run_probe(request, {**form, "list_quality": list_quality}, "runs",
                                        runs=[run], extend=extend)
                if list_quality and check and not any(s.get("listed") for s in check.steps) \
                        and not _LOGIN_PROBLEM.search(next((s["message"] for s in check.steps
                                                            if s.get("message")), "") or ""):
                    # Nothing for a 4K request (a title without UHD): ask the normal way.
                    check = self._run_probe(request, form, "runs", runs=[run], extend=extend)
            if check and check.result:
                last = check
                videos = check.result["video"]
            if check and check.status == "failed" or (check and any(s["status"] == "error" for s in check.steps)):
                error = check.error or next((s["message"] for s in check.steps if s.get("message")), None)
            if not last:
                empty_runs += 1
                # Don't log in again and again when it's the login (or the link) that fails.
                if empty_runs >= 2 or (error and _LOGIN_PROBLEM.search(error)):
                    break
                continue
            found = pick(videos, service, codec, range_, quality, max_height)
            if found and found[1]:
                chosen = (index, found[0], True)
                break
            if found:
                lower.append((index, found[0], False))

        if not last:
            request.set("failed", f"The check returned no track list: {error or 'unknown reason'}")
            return
        # Without the wanted resolution anywhere, a fallback below it (SDR 1080p for a
        # missing 4K HDR) comes before the wanted range below it (HDR 1080p).
        chosen = chosen or next((hit for hit in lower if hit[1]["range"] != spec["range"]), None) \
            or (lower[0] if lower else None)
        self._answer(request, last, chosen, max_height, check_quality)

    def _answer(self, request, check, chosen, max_height, check_quality):
        spec = request.spec
        result = check.result
        title = result["title"]
        request.title = {
            "name": title.get("name"), "year": title.get("year"), "type": title.get("type"),
            "season": title.get("season"), "episode": title.get("episode"),
            "episode_name": title.get("episode_name"), "poster": result.get("poster"),
            "episodes_total": result.get("titles_total"), "seasons": result.get("seasons"),
        }
        request.forced_options = result.get("forced_options") or {}
        request.options = all_options(result["video"], spec["service"], max_height)
        request.audio_languages = sorted({a["language"] for a in result["audio"]})
        request.subtitle_languages = sorted({s["language"] for s in result["subtitles"]})

        warnings = []
        for wanted, offered, what in ((spec["audio_lang"], request.audio_languages, "audio"),
                                      (spec["sub_lang"], request.subtitle_languages, "subtitles")):
            have = {language.split("-")[0].lower() for language in offered}
            missing = [x for x in (wanted or "").split(",")
                       if x and x.lower() not in ("all", "orig") and x.split("-")[0].lower() not in have]
            if missing:
                warnings.append(f"No {what} in {', '.join(missing)} on the checked episode.")
        is_show = title.get("type") == "TV" or bool(title.get("season"))
        if is_show and not spec["wanted"]:
            warnings.append("No season or episode given: confirming downloads EVERY episode.")

        if not chosen:
            request.warnings = warnings
            names = ", ".join(o["label"] for o in request.options) or "no video tracks"
            request.set("unavailable", f"{CODECS[spec['codec']]} {RANGE_LABELS[spec['range']]} isn't available, "
                                       f"and neither are its fallbacks. Found: {names}.")
            return

        index, offer, resolution_ok = chosen
        request.offer = offer
        request.exact = index == 0 and resolution_ok
        if spec["service"] == "Netflix" and offer["tier"] > 1080:
            warnings.append("Netflix PlayReady only works if the keys are already in your key vaults. "
                            "Request quality 1080 to use Widevine instead.")
            if spec["check_keys"]:
                self._check_keys(request, offer)
        request.warnings = warnings

        name = request.title["name"] or spec["title"]
        if request.exact:
            text = f"{name}: {offer['label']} is available. Download?"
        else:
            wanted = f"{CODECS[spec['codec']]} {RANGE_LABELS[spec['range']]}" \
                     + (f" {spec['quality']}p" if spec["quality"] else "")
            text = f"{name}: {wanted} isn't available. Available instead: {offer['label']}. Download?"
        if not request.exact:
            # What else there is in that codec - 1080p HDR, say, next to 4K SDR.
            others = [o["label"] for o in request.options
                      if o["codec"] == offer["codec"] and o["id"] != offer["id"]]
        else:
            others = [o["label"] for o in request.options
                      if (o["codec"], o["range"]) == (offer["codec"], offer["range"]) and o["tier"] < offer["tier"]]
        if others:
            text += f" (Also possible: {', '.join(others[:6])}.)"
        request.expires_at = time.time() + OFFER_SECONDS
        request.set("awaiting_confirmation", text)

    def _check_keys(self, request, offer):
        """Netflix PlayReady: look the offer's keys up in the vaults (never a license request)."""
        request.set("checking", "Looking for the keys in the vaults…")
        form = self._form(request.spec, offer["codec"], offer["range"])
        form["quality"] = str(offer["height"])
        episode = re.match(r"S\d+(E\d+)?", request.spec["wanted"] or "")
        if episode:
            form["check_episode"] = episode.group(0)
        check = self._run_probe(request, form, "keys")
        status = (check.result or {}).get("keys") if check else None
        request.keys = "cached" if status and status.get("complete") else "missing" if status else "unknown"
