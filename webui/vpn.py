"""
The VPN as vinetrimmer knows it: the proxy settings in vinetrimmer.yml (nordvpn:,
surfshark:, proxies:, default_proxy_service:) that `--proxy <country>` uses.

Logins are write-only here: they are saved, but never sent back to the page.
"""
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import quote, unquote, urlsplit

from ruamel.yaml.comments import CommentedMap

from . import config_io, service_checks

PROVIDERS = {"nordvpn": "NordVPN", "surfshark": "Surfshark", "custom": "Your own proxies"}
_COUNTRY_URL = "https://api.nordvpn.com/v1/servers/countries"
_REFRESH_SECONDS = 24 * 3600

# Shown until NordVPN's own list has been fetched (and when it can't be).
_COUNTRIES = dict(pair.split(":") for pair in (
    "al:Albania|ar:Argentina|au:Australia|at:Austria|be:Belgium|ba:Bosnia and Herzegovina|br:Brazil|bg:Bulgaria|"
    "ca:Canada|cl:Chile|co:Colombia|cr:Costa Rica|hr:Croatia|cy:Cyprus|cz:Czech Republic|dk:Denmark|ee:Estonia|"
    "fi:Finland|fr:France|ge:Georgia|de:Germany|gr:Greece|hk:Hong Kong|hu:Hungary|is:Iceland|in:India|"
    "id:Indonesia|ie:Ireland|il:Israel|it:Italy|jp:Japan|lv:Latvia|lt:Lithuania|lu:Luxembourg|my:Malaysia|"
    "mx:Mexico|md:Moldova|nl:Netherlands|nz:New Zealand|mk:North Macedonia|no:Norway|pe:Peru|ph:Philippines|"
    "pl:Poland|pt:Portugal|ro:Romania|rs:Serbia|sg:Singapore|sk:Slovakia|si:Slovenia|za:South Africa|"
    "kr:South Korea|es:Spain|se:Sweden|ch:Switzerland|tw:Taiwan|th:Thailand|tr:Turkey|ua:Ukraine|"
    "ae:United Arab Emirates|gb:United Kingdom|us:United States|vn:Vietnam").split("|"))

_cache = {"at": 0, "countries": None, "loading": False}
_lock = threading.Lock()


def _fetch_countries():
    try:
        request = urllib.request.Request(_COUNTRY_URL, headers={"User-Agent": "VT.PR.WV"})
        with urllib.request.urlopen(request, timeout=10) as response:
            found = {str(c["code"]).lower(): str(c["name"]) for c in json.load(response)}
        if found:
            with _lock:
                _cache["countries"] = found
    except Exception:
        pass  # the built-in list stays
    finally:
        with _lock:
            _cache.update(at=time.time(), loading=False)


def _all_countries():
    with _lock:
        stale = time.time() - _cache["at"] > _REFRESH_SECONDS and not _cache["loading"]
        if stale:
            _cache["loading"] = True
        countries = _cache["countries"]
    if stale:
        threading.Thread(target=_fetch_countries, daemon=True).start()
    return countries or _COUNTRIES


def country_name(code):
    code = (code or "").lower()
    return _all_countries().get(code) or _COUNTRIES.get(code) or code.upper()


def _provider(cfg):
    chosen = cfg.get("default_proxy_service")
    if chosen in ("nordvpn", "surfshark"):
        return chosen
    return "custom" if cfg.get("proxies") else ""


def _login_set(cfg, name):
    section = cfg.get(name) or {}
    return bool(section.get("username") and section.get("password"))


def _proxy_rows(cfg):
    rows = []
    for region, value in (cfg.get("proxies") or {}).items():
        text = str(value or "")
        parts = urlsplit(text if "://" in text else f"https://{text}")
        try:
            port = parts.port
        except ValueError:
            port = None
        rows.append({"country": str(region), "scheme": parts.scheme or "https", "host": parts.hostname or "",
                     "port": port, "has_login": parts.username is not None,
                     "username": unquote(parts.username or "")})
    return sorted(rows, key=lambda r: r["country"])


def status():
    """Everything the pages need to offer the VPN. Holds no passwords."""
    cfg = service_checks._cfg()
    provider = _provider(cfg)
    if provider == "custom":
        codes = sorted(str(k).lower() for k in (cfg.get("proxies") or {}) if re.fullmatch(r"[A-Za-z]{2}", str(k)))
        countries = [{"code": c, "name": country_name(c)} for c in codes]
        ready = bool(countries)
    else:
        countries = sorted(({"code": c, "name": n} for c, n in _all_countries().items()), key=lambda x: x["name"])
        ready = bool(provider) and _login_set(cfg, provider)
    problem = ""
    if not provider:
        problem = "No VPN is set up yet. Add one on the Credentials tab, under VPN."
    elif not ready:
        problem = (f"{PROVIDERS[provider]} has no login saved yet." if provider != "custom"
                   else "No proxy for a country is saved yet.")
    nord = cfg.get("nordvpn") or {}
    return {
        "provider": provider,
        "provider_name": PROVIDERS.get(provider, ""),
        "ready": ready,
        "problem": problem,
        "countries": countries,
        "logins": {name: _login_set(cfg, name) for name in ("nordvpn", "surfshark")},
        # NordVPN's proxies refuse the account's email and password.
        "nordvpn_email_login": "@" in str(nord.get("username") or ""),
        "nordvpn_servers": {str(k): v for k, v in (nord.get("servers") or {}).items()},
        "proxies": _proxy_rows(cfg),
    }


def _section(data, name):
    if not isinstance(data.get(name), dict):
        data[name] = CommentedMap()
    return data[name]


def _save_login(data, name, values):
    username = str(values.get("username") or "").strip()
    password = str(values.get("password") or "")
    if values.get("remove"):
        section = _section(data, name)
        section["username"] = ""
        section["password"] = ""
        return
    if not username and not password:
        return  # left as it is
    if not username or not password:
        raise ValueError(f"{PROVIDERS[name]}: enter both the username and the password.")
    section = _section(data, name)
    section["username"] = username
    section["password"] = password


def _proxy_url(row, existing):
    country = str(row.get("country") or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2}", country):
        raise ValueError("Every proxy needs a two-letter country code, such as id or us.")
    scheme = row.get("scheme") if row.get("scheme") in ("http", "https") else "https"
    host = str(row.get("host") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise ValueError(f"Proxy for {country.upper()}: enter the server's name or IP address.")
    try:
        port = int(row.get("port"))
    except (TypeError, ValueError):
        port = 0
    if not 0 < port < 65536:
        raise ValueError(f"Proxy for {country.upper()}: enter a port between 1 and 65535.")
    username = str(row.get("username") or "")
    password = str(row.get("password") or "")
    login = ""
    if username and password:
        login = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    elif username:
        # unchanged username, password left empty: keep the saved login
        old = urlsplit(existing or "")
        if old.username is None or unquote(old.username) != username:
            raise ValueError(f"Proxy for {country.upper()}: enter the password too.")
        login = old.netloc.rsplit("@", 1)[0] + "@"
    return country, f"{scheme}://{login}{host}:{port}"


def save(values):
    """Save what the VPN form sent. Parts that weren't sent stay as they are."""
    with config_io._config_lock:
        data = config_io._read_user_config()
        for name in ("nordvpn", "surfshark"):
            if isinstance(values.get(name), dict):
                _save_login(data, name, values[name])
        if isinstance(values.get("nordvpn_servers"), dict):
            servers = CommentedMap()
            for country, number in values["nordvpn_servers"].items():
                country = str(country).strip().lower()
                if not re.fullmatch(r"[a-z]{2}", country) or not str(number).strip().isdigit():
                    raise ValueError("Preferred servers: a two-letter country and the server's number, e.g. id / 69.")
                servers[country] = int(str(number).strip())
            _section(data, "nordvpn")["servers"] = servers
        if isinstance(values.get("proxies"), list):
            merged = (service_checks._cfg().get("proxies") or {})
            proxies = CommentedMap()
            for row in values["proxies"][:100]:
                original = str(row.get("original") or row.get("country") or "")
                country, url = _proxy_url(row, str(merged.get(original) or ""))
                if country in proxies:
                    raise ValueError(f"There are two proxies for {country.upper()}.")
                proxies[country] = url
            data["proxies"] = proxies
        if "provider" in values:
            provider = values["provider"]
            if provider not in ("nordvpn", "surfshark", "custom", ""):
                raise ValueError("Unknown provider.")
            # Without a default provider, vinetrimmer uses the proxies: list.
            data["default_proxy_service"] = provider if provider in ("nordvpn", "surfshark") else None
        config_io._write_user_config(data)
    return status()


def test(country):
    """Connect through the VPN once and report which country that comes out in."""
    country = (country or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2}", country):
        raise ValueError("Choose a country to test.")
    try:
        run = subprocess.run([sys.executable, "-m", "webui.vpn_test", country], cwd=str(config_io.PROJECT_ROOT),
                             capture_output=True, text=True, timeout=75)
        return json.loads(run.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "No answer within 75 seconds. The server may be busy or blocked."}
    except (OSError, ValueError, IndexError):
        return {"ok": False, "message": "The test couldn't run. Check the web UI's log."}
