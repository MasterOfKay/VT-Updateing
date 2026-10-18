"""
Configuration I/O for the web UI.
"""
import functools
import threading
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from vinetrimmer.config import directories, filenames
from vinetrimmer.utils.collections import merge_dict
from vinetrimmer.utils.io import load_yaml

USER_CONFIG_PATH = Path(filenames.user_root_config)
COOKIES_DIR = Path(directories.cookies)
PROJECT_ROOT = Path(directories.package_root).parent
DEFAULT_DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
DEFAULT_TEMP_DIR = PROJECT_ROOT / "temp"

_ryaml = YAML()
_ryaml.preserve_quotes = True
_ryaml.allow_duplicate_keys = True

_config_lock = threading.RLock()


def _locked(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _config_lock:
            return func(*args, **kwargs)
    return wrapper


def needs_auth(service_name):
    """Mirrors vinetrimmer/commands/dl.py's get_service_config() needs_auth resolution."""
    default = load_yaml(filenames.service_config.format(service=service_name.lower())) or {}
    override = (
        load_yaml(filenames.user_service_config.format(service=service_name.lower()))
        or load_yaml(filenames.user_service_config.format(service=service_name))
        or {}
    )
    merged = dict(default)
    merge_dict(merged, override)
    return merged.get("needs_auth", True)


@_locked
def _read_user_config():
    if not USER_CONFIG_PATH.is_file():
        return CommentedMap()
    with open(USER_CONFIG_PATH, encoding="utf-8") as fd:
        data = _ryaml.load(fd)
    if data is None:
        return CommentedMap()

    plain_creds = (load_yaml(str(USER_CONFIG_PATH)) or {}).get("credentials") or {}
    if isinstance(data.get("credentials"), dict):
        for key, value in plain_creds.items():
            if data["credentials"].get(key) != value:
                data["credentials"][key] = value

    return data


@_locked
def _write_user_config(data):
    with open(USER_CONFIG_PATH, "w", encoding="utf-8") as fd:
        _ryaml.dump(data, fd)


@_locked
def credential_status(service_name):
    """Whether a credential entry exists for this service. Never returns the value."""
    data = _read_user_config()
    creds = data.get("credentials") or {}
    entry = creds.get(service_name)
    if entry is None:
        return {"configured": False, "profiles": []}
    if isinstance(entry, dict):
        return {"configured": True, "profiles": list(entry.keys())}
    return {"configured": True, "profiles": ["default"]}


@_locked
def set_credential(service_name, username, password, profile=None):
    """
    Write credentials.<service_name>[.<profile>] = "username:password" into
    vinetrimmer.yml, preserving everything else in the file untouched.

    Credential.loads() (vinetrimmer/objects/credential.py) parses this with a regex
    that treats ':' purely as the username/password/extra separator - a colon inside
    either field would silently corrupt it on the next read, so that's rejected here.
    """
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are required.")
    if ":" in username or ":" in password:
        raise ValueError("Username/password cannot contain a ':' character (used as the field separator).")

    data = _read_user_config()
    if not isinstance(data.get("credentials"), dict):
        data["credentials"] = CommentedMap()
    creds = data["credentials"]

    value = f"{username}:{password}"
    if profile and profile != "default":
        existing = creds.get(service_name)
        if not isinstance(existing, dict):
            existing = CommentedMap()
        existing[profile] = value
        creds[service_name] = existing
    else:
        creds[service_name] = value

    _write_user_config(data)


def cookie_status(service_name, profile="default"):
    for candidate in (service_name, service_name.lower()):
        path = COOKIES_DIR / candidate / f"{profile}.txt"
        if path.is_file():
            return {"configured": True, "path": str(path)}
    return {"configured": False, "path": str(COOKIES_DIR / service_name / f"{profile}.txt")}


def save_cookie(service_name, profile, file_storage):
    """
    Always stored as <profile>.txt (default.txt), whatever the uploaded file was
    called - that's the only name the CLI looks for. The folder follows dl.py's
    get_cookie_jar() lookup order (cookies/<service lower>, then cookies/<Service>),
    so an existing folder the CLI would check first is reused rather than creating
    a second one it would never read on case-sensitive filesystems (Linux).
    """
    target_dir = next(
        (COOKIES_DIR / c for c in (service_name.lower(), service_name) if (COOKIES_DIR / c).is_dir()),
        COOKIES_DIR / service_name,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{profile or 'default'}.txt"
    file_storage.save(str(target_path))
    return str(target_path)


@_locked
def get_storage_paths():
    """
    Current directories.downloads / directories.temp override from vinetrimmer.yml,
    plus what will actually be used (vinetrimmer's own setup_paths(), in
    vinetrimmer/config/__init__.py, only applies an override if it's already an
    existing directory, otherwise it silently falls back to the project-relative
    default - "_effective" mirrors that exact check so the UI can be honest about it).
    """
    data = _read_user_config()
    dirs = data.get("directories") or {}
    downloads = (dirs.get("downloads") or "").strip()
    temp = (dirs.get("temp") or "").strip()
    return {
        "downloads": downloads,
        "downloads_effective": downloads if downloads and Path(downloads).is_dir() else str(DEFAULT_DOWNLOADS_DIR),
        "temp": temp,
        "temp_effective": temp if temp and Path(temp).is_dir() else str(DEFAULT_TEMP_DIR),
    }


@_locked
def set_storage_paths(downloads=None, temp=None):
    """
    Write directories.downloads / directories.temp into vinetrimmer.yml - there is no
    per-run --downloads/--temp CLI flag, this is the only way to point the CLI at a
    different disk/folder (e.g. for a full boot SSD). A non-empty path is created
    immediately (mkdir -p) so it's an existing directory by the time setup_paths()
    checks for it - and resolved to an absolute path first if given as relative, since
    the CLI subprocess vinetrimmer.py always runs with the project root as its cwd,
    not whatever directory the web server itself happened to be launched from.
    An empty string reverts that entry to vinetrimmer's own default.
    """
    data = _read_user_config()
    if not isinstance(data.get("directories"), dict):
        data["directories"] = CommentedMap()
    dirs = data["directories"]

    for key, value in (("downloads", downloads), ("temp", temp)):
        if value is None:
            continue
        value = value.strip()
        if value:
            resolved = Path(value)
            if not resolved.is_absolute():
                resolved = PROJECT_ROOT / resolved
            resolved.mkdir(parents=True, exist_ok=True)
            value = str(resolved)
        dirs[key] = value

    _write_user_config(data)
    return get_storage_paths()


@_locked
def get_progress_display():
    """
    downloads.progress_display in vinetrimmer.yml - whether track downloads render as
    Rich's Live-updating progress tree, or as plain "Downloaded: <track>" log lines
    (vinetrimmer/commands/dl.py's make_log_progress_callback / progress_display_context).

    This matters a lot for the web console specifically: Rich's Live display only
    redraws in place on a real terminal. Piped through a subprocess (as every web UI
    job is), it renders nothing at all until the whole download finishes, then dumps
    the entire final tree in one go - confirmed by direct repro (a Rich Live progress
    bar over a pipe: identical ~6s delay before any output, whether or not stdout is
    unbuffered) and by a real job here that produced zero progress output for ~11
    minutes before the final tree appeared. The plain-log-lines mode is unaffected by
    any of this since it's normal logging output, not a Live redraw.
    """
    data = _read_user_config()
    pd = (data.get("downloads") or {}).get("progress_display")
    if isinstance(pd, dict):
        return bool(pd.get("default", True))
    if pd is None:
        return True
    return bool(pd)


@_locked
def set_progress_display(enabled):
    data = _read_user_config()
    if not isinstance(data.get("downloads"), dict):
        data["downloads"] = CommentedMap()
    downloads_cfg = data["downloads"]

    existing = downloads_cfg.get("progress_display")
    if isinstance(existing, dict):
        existing["default"] = bool(enabled)
    else:
        pd = CommentedMap()
        pd["default"] = bool(enabled)
        downloads_cfg["progress_display"] = pd

    _write_user_config(data)
    return get_progress_display()


ARGUMENT_DEFAULT_KEYS = ("alang", "slang", "vcodec")


@_locked
def get_argument_defaults():
    data = _read_user_config()
    args = data.get("arguments") or {}
    return {key: (args.get(key) or "") for key in ARGUMENT_DEFAULT_KEYS}


@_locked
def set_argument_defaults(values):
    data = _read_user_config()
    if not isinstance(data.get("arguments"), dict):
        data["arguments"] = CommentedMap()
    args = data["arguments"]

    for key in ARGUMENT_DEFAULT_KEYS:
        if key not in values:
            continue
        value = (values.get(key) or "").strip()
        if value:
            args[key] = value
        elif key in args:
            del args[key]

    _write_user_config(data)
    return get_argument_defaults()
