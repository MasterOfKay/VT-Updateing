"""
Settings of the external API (see api_server.py), including its key. Kept in their own
file, apart from the other settings, because of the key.
"""
import json
import os
import secrets
import threading

from . import paths

PATH = paths.data_file("api_settings.json")
DEFAULTS = {
    "enabled": False,
    "host": "127.0.0.1",  # 0.0.0.0: reachable from other machines
    "port": 8788,
    "key": "",
    # Don't check a service while it is downloading; the request waits until it's done.
    "wait_same_service": True,
    # Start the queue for a confirmed request if it's idle (a paused queue stays paused).
    "auto_start": True,
}
HOSTS = ("127.0.0.1", "0.0.0.0")
_lock = threading.RLock()


def _new_key():
    return "vt_" + secrets.token_urlsafe(32)


def _clean(data, out):
    for name in ("enabled", "wait_same_service", "auto_start"):
        if name in data:
            out[name] = bool(data[name])
    if data.get("host") in HOSTS:
        out["host"] = data["host"]
    if "port" in data:
        try:
            port = int(data["port"])
        except (TypeError, ValueError):
            port = 0
        if 1024 <= port <= 65535:
            out["port"] = port
    return out


def _read():
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    out = _clean(data, dict(DEFAULTS))
    out["key"] = str(data.get("key") or "")
    return out


def _write(settings):
    tmp = PATH.with_name(PATH.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # owner only, where the system supports it
    except OSError:
        pass
    os.replace(tmp, PATH)


def get():
    """The settings in effect. VT_API_HOST / VT_API_PORT (set by the Docker image) win
    over the saved ones."""
    with _lock:
        out = _read()
    out["host_locked"] = bool(os.environ.get("VT_API_HOST"))
    out["port_locked"] = bool(os.environ.get("VT_API_PORT"))
    if out["host_locked"]:
        out["host"] = os.environ["VT_API_HOST"]
    if out["port_locked"]:
        out["port"] = int(os.environ["VT_API_PORT"])
    return out


def save(values):
    with _lock:
        current = _clean(values or {}, _read())
        if current["enabled"] and not current["key"]:
            current["key"] = _new_key()
        _write(current)
    return get()


def regenerate_key():
    with _lock:
        current = _read()
        current["key"] = _new_key()
        _write(current)
    return get()
