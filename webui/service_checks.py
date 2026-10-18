"""
Service checks: what the CLI needs to run a download of a service, and whether it's present.
"""
import html
import http.cookiejar
import importlib
import io
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from appdirs import AppDirs

from vinetrimmer import services as vt_services
from vinetrimmer.config import directories, filenames
from vinetrimmer.objects.credential import Credential
from vinetrimmer.utils.collections import merge_dict
from vinetrimmer.utils.io import load_yaml

PROJECT_ROOT = Path(directories.package_root).parent

OK, INFO, WARN, ERROR = "ok", "info", "warn", "error"

# Service-specific details a docstring line can't express. Keep these factual:
# each one is derived from the service's own code, referenced alongside.
SERVICE_NOTES = {
    # disneyplus.py: credentials -> email/password login (may ask for an OTP code
    # sent by email); no credentials -> asks for a Web Refresh Token if Cookies are inisde. Tokens are
    # cached in cache/DSNP/tokens_<REGION>_*.json and refreshed automatically.
    "DisneyPlus": [
        "Web Token/Cookies: without credentials, the script asks for a Web Refresh Token when it starts. "
        "You find it in a browser that's logged in to disneyplus.com (Dev Tools &rarr; Application &rarr; "
        "Local Storage). Paste it into the input box on the Console tab when you're asked.",
        "With credentials, Disney+ may email you a one-time code (OTP), and a profile with a PIN asks for "
        "that PIN. Type both into the input box on the Console tab.",
        "After the first login the tokens are saved and renewed automatically. You're only asked again "
        "when the saved login runs out.",
    ],
    # sonyliv.py: asks for accessToken via input() on first run, caches it.
    "Sonyliv": [
        "accessToken: the first time, the script asks for the accessToken from your browser "
        "(Dev Tools &rarr; Application &rarr; Local Storage &rarr; https://www.sonyliv.com &rarr; accessToken). "
        "Paste it into the input box on the Console tab. It's saved after that.",
    ],
    # hotstar.py login(): literal "username:password" credentials switch to TV code login.
    "Hotstar": [
        "If the credentials are exactly <code>username:password</code>, the script shows a code. Enter it "
        "on tv.hotstar.com, then confirm with y in the input box on the Console tab.",
    ],
    "ITV": [
        "Cookies are only needed for premium content. Free content works without them.",
    ],
    "YouTube": [
        "Only videos without DRM can be downloaded. Protected movies and rentals are skipped.",
    ],
}

# Docstring phrasings for "no login".
_NONE_RE = re.compile(r"^\s*none\b", re.I)

_ip_cache = {"at": 0, "country": None, "error": None}
_ip_lock = threading.Lock()


def _cfg():
    """
    Fresh merge of config/vinetrimmer.yml + vinetrimmer.yml, exactly like
    vinetrimmer.config does for each CLI run. Re-read on every check (rather than
    using the already-imported vinetrimmer.config.config) so edits made on the
    Credentials/Settings pages - or by hand - are reflected without a restart.
    """
    cfg = load_yaml(filenames.root_config) or {}
    merge_dict(cfg, load_yaml(filenames.user_root_config) or {})
    return cfg


# ---------------------------------------------------------------- docstring info

def _service_source(service_name):
    key = vt_services.get_service_key(service_name)
    if not key:
        return None, None
    path = Path(vt_services.__file__).parent / f"{key.lower()}.py"
    try:
        return key, path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return key, None


def _docstring_field(source, name):
    """First `Name: value` line in the script (the service docstrings' own convention)."""
    m = re.search(rf"^[ \t]*{name}:[ \t]*(.+?)[ \t]*$", source or "", re.M)
    if not m:
        return None
    value = m.group(1).replace("\\n", "").strip()
    return value or None


def _security_text(source):
    """The Security:/Robustness: block, which may continue on indented lines."""
    m = re.search(r"^([ \t]*)(?:Security(?: Levels)?|Robustness):[ \t]*(.*)$", source or "", re.M)
    if not m:
        return None
    indent = len(m.group(1).expandtabs())
    lines = [m.group(2).strip()]
    for line in source[m.end():].splitlines()[1:]:
        if not line.strip() or len(line.expandtabs()) - len(line.expandtabs().lstrip()) <= indent:
            break
        lines.append(line.strip())
    text = " ".join(x for x in lines if x).strip()
    return text or None


def _prompts(source):
    """Text of every input() prompt in the script - each is a question asked at runtime."""
    found = []
    for m in re.finditer(r"""\binput\(\s*(['"])(.*?)\1""", source or ""):
        text = m.group(2).strip()
        if len(text) < 3:  # e.g. SonyLIV's bare input(">")
            text = "(asks for a value)"
        if text not in found:
            found.append(text)
    if re.search(r"\binput\(\s*\)", source or "") and "(asks for a value)" not in found:
        found.append("(asks for a value)")
    return found


def parse_authorization(text):
    """
    Turn a docstring `Authorization:` value into what's needed:
      {"none": bool, "cookies": bool, "credentials": bool, "token": bool,
       "mode": "all" | "any", "optional": bool}
    `+` joins requirements that are all needed ("Cookies + accessToken"), a comma or
    "or" lists alternatives ("Credentials, Web Token"). "Login" means credentials.
    """
    if not text:
        return None
    lower = text.lower()
    if _NONE_RE.match(lower):
        return {"none": True, "cookies": False, "credentials": False, "token": False,
                "mode": "all", "optional": False}
    return {
        "none": False,
        "cookies": "cookie" in lower,
        "credentials": bool(re.search(r"credential|login", lower)),
        "token": "token" in lower,
        "mode": "all" if "+" in lower else ("any" if re.search(r",|\bor\b", lower) else "all"),
        "optional": "optional" in lower,
    }


# ---------------------------------------------------------------- CLI mirrors

def effective_profile(service_name, profile=None):
    """dl.py get_profile(): -p wins, then profiles.<Service>, then profiles.default.
    Returns (profile or None, error message or None)."""
    if profile:
        return profile, None
    profiles = _cfg().get("profiles") or {}
    p = profiles.get(service_name)
    if p is False:
        return None, None  # explicitly auth-less in config
    p = p or profiles.get("default")
    if not p:
        return None, f"No profile is defined for '{service_name}' (profiles: in vinetrimmer.yml), so the download won't start."
    return p, None


def _cookie_path(service_name, profile):
    """dl.py get_cookie_jar(): cookies/<service lower>/<profile>.txt, then cookies/<Service>/..."""
    for candidate in (service_name.lower(), service_name):
        path = Path(directories.cookies) / candidate / f"{profile}.txt"
        if path.is_file():
            return path
    return None


def check_cookie_file(path):
    """Parse the cookie file the same way dl.py does and look at expiry."""
    try:
        text = html.unescape(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        return ERROR, f"Cookie file can't be read: {e}"
    if not text.strip():
        return ERROR, (f"Cookie file {path.parent.name}/{path.name} is empty, and the download crashes when it loads it. "
                       "Upload a real cookies.txt on the Credentials tab (or delete the empty file).")
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar._really_load(io.StringIO(text), str(path), ignore_discard=True, ignore_expires=True)
    except Exception:
        return ERROR, ("Cookie file isn't in Netscape cookies.txt format, so the CLI can't load it. "
                       "Export it with a \"Get cookies.txt\"-style browser extension (JSON exports don't work).")
    cookies = list(jar)
    if not cookies:
        return ERROR, "Cookie file contains no cookies."

    now = time.time()
    persistent = [c for c in cookies if c.expires]
    expired = [c for c in persistent if c.expires < now]
    if persistent and len(expired) == len(persistent):
        newest = max(c.expires for c in persistent)
        return ERROR, (f"All {len(persistent)} cookies with an expiry date have expired "
                       f"(last one on {datetime.fromtimestamp(newest):%Y-%m-%d}). Log in again in the browser and re-export.")
    age_days = (now - path.stat().st_mtime) / 86400
    if expired:
        return WARN, (f"{len(expired)} of {len(cookies)} cookies have expired. If the login fails, "
                      f"re-export the cookies from a logged-in browser. File is {age_days:.0f} days old.")
    return OK, f"{len(cookies)} cookies, none expired. File is {age_days:.0f} days old."


def credential_for(service_name, profile):
    """dl.py get_credentials(), without constructing it. Returns (raw value or None)."""
    creds = _cfg().get("credentials") or {}
    cred = creds.get(service_name, {})
    if isinstance(cred, dict):
        return cred.get(profile)
    return cred if profile == "default" else None


def check_credential(raw):
    if isinstance(raw, list):
        return (OK, "Set.") if len(raw) >= 2 and all(raw[:2]) else (ERROR, "Credential list must be [username, password].")
    try:
        Credential.loads(str(raw))
    except Exception:
        return ERROR, "This isn't in the mail:password format, so it can't be read."
    return OK, "Set (mail:password)."


def resolve_cdm(service_name, profile=None, cdm_override=None):
    """
    Mirrors dl.py get_cdm() without loading the device. Returns
    (name, kind, status, message).
    """
    cfg = _cfg()
    cdm_cfg = cfg.get("cdm") or {}
    name = cdm_override or cdm_cfg.get(service_name) or cdm_cfg.get("default")
    if not name:
        return None, None, ERROR, "No CDM is set for this service (cdm: in vinetrimmer.yml), so the download won't start."
    if isinstance(name, dict):
        if not profile:
            return None, None, ERROR, "The CDM is mapped per profile, but no profile is selected."
        name = name.get(profile) or cdm_cfg.get("default")
        if not name:
            return None, None, ERROR, f"No CDM is mapped for profile '{profile}'."

    devices = Path(directories.devices)
    lower = name.lower()
    kind = "PlayReady SL3000" if "sl3000" in lower else "PlayReady SL2000" if "sl2000" in lower else "Widevine"
    device_dir = devices / name
    if device_dir.is_dir() and (device_dir / "zgpriv.dat").is_file() and (device_dir / "bgroupcert.dat").is_file():
        return name, kind, OK, f"PlayReady device folder found ({device_dir.name})."
    if "sl2000" in lower or "sl3000" in lower:
        if (devices / f"{name}.prd").is_file():
            return name, kind, OK, f"Device file found ({name}.prd)."
    elif (devices / f"{name}.wvd").is_file():
        return name, kind, OK, f"Device file found ({name}.wvd)."

    for d in (devices / name,
              Path(AppDirs("pywidevine", False).user_data_dir) / "devices" / name,
              Path(AppDirs("pywidevine", False).site_data_dir) / "devices" / name):
        if (d / "wv.json").is_file() and (d / "device_client_id_blob").is_file():
            return name, "Widevine", OK, f"Widevine device folder found ({d})."

    api = next((x for x in (cfg.get("cdm_api") or []) if x.get("name") == name), None)
    if api:
        if not api.get("host"):
            return name, "Remote CDM API", ERROR, f"Remote CDM '{name}' has no host set (cdm_api: in vinetrimmer.yml)."
        return name, "Remote CDM API", OK, f"Remote CDM API ({api.get('host')})."

    if device_dir.is_dir():
        return name, kind, ERROR, (f"Folder devices/{name} exists but is missing the device files "
                                   "(PlayReady: zgpriv.dat + bgroupcert.dat). The job will fail at startup.")
    return name, kind, ERROR, f"CDM device '{name}' isn't in devices/, so the download will fail right away."


def _tool_path():
    """The PATH the CLI subprocess uses (vinetrimmer.py prepends the bundled binaries)."""
    dirs = [str(PROJECT_ROOT / "binaries")]
    if sys.platform != "win32":
        dirs.insert(0, str(PROJECT_ROOT / "linux-binaries"))
    return os.pathsep.join([*dirs, os.environ.get("PATH", "")])


def _which(*names):
    path = _tool_path()
    return next((p for p in (shutil.which(n, path=path) for n in names) if p), None)


def check_tools(service_name):
    """External programs a download of this service will call."""
    cfg = _cfg()
    downloader_cfg = cfg.get("downloader") or {}
    downloader = downloader_cfg.get(service_name) or downloader_cfg.get("default", "aria2c")
    platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)
    decrypter = cfg.get("decrypter") or "packager"

    needed = []
    if str(downloader).lower().startswith("n_m3u8dl"):
        needed.append((f"N_m3u8DL-RE (downloader)", ("m3u8re", "N_m3u8DL-RE")))
        needed.append(("ffmpeg (used by N_m3u8DL-RE)", ("ffmpeg",)))
    else:
        needed.append(("aria2c (downloader)", ("aria2c", "aria2")))
    if decrypter == "packager":
        needed.append(("Shaka Packager (decrypter)", ("shaka-packager", "packager", f"packager-{platform}")))
    else:
        needed.append(("mp4decrypt (decrypter)", ("mp4decrypt",)))
    needed.append(("mkvmerge (muxing into .mkv)", ("mkvmerge",)))
    if service_name == "YouTube":
        needed.append(("yt-dlp", ("yt-dlp",)))
        needed.append(("deno (used by yt-dlp)", ("deno",)))

    missing = [label for label, names in needed if not _which(*names)]
    return downloader, [label for label, _ in needed], missing


def current_country():
    """Same lookup the CLI's get_proxy() uses (ipwho.is), cached for 10 minutes."""
    with _ip_lock:
        if time.time() - _ip_cache["at"] < 600:
            return _ip_cache["country"], _ip_cache["error"]
    try:
        import httpx
        country = (httpx.get("https://ipwho.is/", timeout=5).json().get("country_code") or "").lower() or None
        error = None
    except Exception as e:
        country, error = None, str(e)
    with _ip_lock:
        _ip_cache.update(at=time.time(), country=country, error=error)
    return country, error


def _proxy_available(region):
    cfg = _cfg()
    proxies = cfg.get("proxies") or {}
    default_service = cfg.get("default_proxy_service")
    if proxies.get(region) and not default_service:
        return True
    for svc in ("nordvpn", "surfshark"):
        creds = cfg.get(svc) or {}
        if default_service == svc and creds.get("username") and creds.get("password"):
            return True
    return False


# ---------------------------------------------------------------- the check

def check_service(service_name, profile=None, cdm_override=None, proxy=None, no_proxy=False):
    key, source = _service_source(service_name)
    if not key:
        return None

    auth_text = _docstring_field(source, "Authorization")
    auth = parse_authorization(auth_text)
    items = []  # {"label", "status", "detail"}

    def add(label, status, detail):
        items.append({"label": label, "status": status, "detail": detail})

    # --- profile / login material
    prof, prof_error = effective_profile(key, profile)
    if prof_error:
        add("Profile", ERROR, prof_error)

    cookie_path = _cookie_path(key, prof) if prof else None
    cred_raw = credential_for(key, prof) if prof else None
    cookie_status = check_cookie_file(cookie_path) if cookie_path else None
    cred_status = check_credential(cred_raw) if cred_raw else None
    have_cookie = bool(cookie_status and cookie_status[0] != ERROR)
    have_cred = bool(cred_status and cred_status[0] != ERROR)
    cookie_where = f"cookies/{key}/{prof}.txt"

    cdm_name, cdm_kind, cdm_state, cdm_msg = resolve_cdm(key, prof, cdm_override)

    if auth is None:
        add("Login", INFO, "The service script doesn't say what login it needs (no \"Authorization:\" line).")
        want_cookies = want_creds = want_token = False
        mode = "any"
    elif auth["none"]:
        add("Login", OK, "Not needed. This service works without an account.")
        want_cookies = want_creds = want_token = False
        mode = "all"
    else:
        want_cookies, want_creds, want_token = auth["cookies"], auth["credentials"], auth["token"]
        mode = auth["mode"]

    # Netflix: cookies always; credentials too unless a Chrome CDM is used (netflix.py).
    if key == "Netflix":
        want_cookies = True
        mode = "all"
        want_creds = not (cdm_name and "chrome" in cdm_name.lower())

    optional = bool(auth and auth["optional"])

    def need_state(present):
        if present:
            return OK
        if optional:
            return INFO
        return ERROR if mode == "all" else WARN

    if want_creds:
        if cred_status:
            state, detail = cred_status
        else:
            state = need_state(False)
            detail = f"Not set for profile '{prof}'. Add mail:password on the Credentials tab."
        add("Credentials (mail:password)", state, detail)
    elif cred_raw:
        add("Credentials", INFO, "Set, but this service doesn't use them.")

    if want_cookies:
        if cookie_status:
            state, detail = cookie_status
        else:
            state = need_state(False)
            detail = f"No cookie file at {cookie_where}. Upload one on the Credentials tab."
        add("Cookies", state, detail)
    elif cookie_status:
        add("Cookies", INFO, "Cookie file found, but this service doesn't use cookies.")

    if want_token:
        add("Token", INFO, "You're asked for it while the download runs (see the notes below). Answer in the input box on the Console tab.")

    # Overall login verdict, following the docstring's all/any semantics.
    if auth and not auth["none"]:
        options = []
        if want_creds:
            options.append(have_cred)
        if want_cookies:
            options.append(have_cookie)
        if want_token:
            options.append(True)  # entered at runtime
        if options:
            satisfied = all(options) if mode == "all" else any(options)
            if not satisfied and not optional:
                for item in items:
                    if item["status"] == WARN and item["label"].startswith(("Credentials", "Cookies")):
                        item["status"] = ERROR

    # dl.py itself stops with "Profile has no cookies or credentials" in this case.
    needs_auth = _needs_auth(key)
    if prof and needs_auth and not cookie_path and not cred_raw:
        add("Startup check", ERROR, f"Profile '{prof}' has no cookies or credentials, so the download stops right away.")

    # --- CDM
    security = _security_text(source)
    add(f"CDM ({cdm_kind or 'unknown'})" + (f": {cdm_name}" if cdm_name else ""), cdm_state,
        cdm_msg + (f" Service security: {security}" if security else ""))

    # --- region
    geofence = list(getattr(importlib.import_module(f"vinetrimmer.services.{key.lower()}"), key).GEOFENCE or [])
    requires_ip = re.search(r"requires? an? ([A-Z]{2}) IP", source or "")
    region = (geofence[0] if geofence else (requires_ip.group(1) if requires_ip else None))
    if region:
        region = region.lower()
        if no_proxy:
            add("Region", WARN, f"Needs a {region.upper()} IP and --no-proxy is set. This only works if you're already there.")
        elif proxy:
            add("Region", INFO, f"Needs a {region.upper()} IP. Using the proxy you set ({proxy}).")
        elif not geofence:
            add("Region", INFO, f"Needs a {region.upper()} IP (VPN or --proxy); the script doesn't switch automatically.")
        else:
            country, err = current_country()
            if country == region:
                add("Region", OK, f"Needs a {region.upper()} IP. You're in {region.upper()}.")
            elif _proxy_available(region):
                add("Region", OK, f"Needs a {region.upper()} IP. A proxy for it is set up and will be used.")
            else:
                where = f"you appear to be in {country.upper()}" if country else f"your location couldn't be checked ({err})"
                add("Region", ERROR, f"Needs a {region.upper()} IP, but {where}, and no proxy for {region.upper()} is "
                                     "set up (proxies: / nordvpn: / surfshark: in vinetrimmer.yml). The download "
                                     "stops with \"Unable to obtain a proxy\". Set --proxy or use a VPN.")

    # --- tools
    downloader, _, missing = check_tools(key)
    if missing:
        add("Tools", ERROR, "Missing: " + ", ".join(missing) + ". Put them in binaries/ or on PATH.")
    else:
        add("Tools", OK, f"Everything needed is available (downloader: {downloader}).")

    # --- questions asked during the run
    prompts = _prompts(source)
    if prompts:
        add("Questions while running", INFO,
            "The script may ask: " + "; ".join(f"\"{p}\"" for p in prompts)
            + ". Answer in the input box on the Console tab.")

    worst = ERROR if any(i["status"] == ERROR for i in items) else \
        WARN if any(i["status"] == WARN for i in items) else OK

    return {
        "authorization": auth_text,
        "security": security,
        "profile": prof,
        "items": items,
        "notes": SERVICE_NOTES.get(key, []),
        "level": worst,
        "credential": {"configured": bool(cred_raw)},
        "cookie": {"configured": bool(cookie_path)},
    }


def _needs_auth(key):
    from . import config_io
    return config_io.needs_auth(key)
