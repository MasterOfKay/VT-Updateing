"""
Version / update control (the Update tab).

- The installed version is the VERSION file in the project root.
- Available versions are the GitHub releases of UPDATE_REPO. A release marked
  "pre-release" on GitHub is an experimental build: only offered when the
  "include pre-releases" setting is on. Tags are versions: v1.0.0, v1.1.0-beta.1, ...
- Installing downloads the release (a .zip attached to it, if there is one, otherwise
  GitHub's source zip of the tag), backs up every file it's about to replace into
  backups/, and copies the new files over the old ones - never touching anything in
  PROTECTED (configs with credentials, services, devices, cookies, key vault, binaries,
  downloads, the web UI's saved queue/settings, ...). Files a release no longer has are
  left in place, not deleted. A restart of the web UI then loads the new code.

Publishing an update: bump VERSION, commit, push, and create a GitHub release whose
tag is the new version (tick "Set as a pre-release" for an experimental build).
"""
import io
import json
import os
import re
import threading
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

UPDATE_REPO = "MasterOfKay/VT-Updateing"
RELEASES_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases?per_page=50"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
BACKUP_DIR = PROJECT_ROOT / "backups"
CACHE_SECONDS = 6 * 3600  # automatic checks (the nav badge); "Check for updates" forces one

# Never written by an update (relative to the project root, "/"-separated). Folders
# cover everything inside them. Mirrors .gitignore, so normally these aren't even in a
# release - this is the guarantee for when one slips in.
PROTECTED_DIRS = (
    "vinetrimmer/services", "vinetrimmer/config/services", "vinetrimmer/devices",
    "vinetrimmer/cache", "vinetrimmer/logs", "binaries", "linux-binaries", "downloads",
    "temp", "NF", "diag", "graphify-out", ".venv", ".git", "backups",
)
PROTECTED_FILES = (
    "vinetrimmer.yml", "vinetrimmer/vinetrimmer.yml",
    "webui/queue_state.json", "webui/webui_settings.json", "webui/poster_cache.json",
    "webui/pins.json",
)
DEPENDENCY_FILES = ("pyproject.toml", "poetry.lock", "uv.lock")

_lock = threading.Lock()
_cache = {"at": 0, "releases": None, "error": None}
_install_lock = threading.Lock()


def current_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def parse_version(text):
    """A comparable version for "v1.2.0", "1.2.0-beta.3", "1.2" ... (None if it isn't one)."""
    text = (text or "").strip().lstrip("vV")
    try:
        from packaging.version import InvalidVersion, Version
        try:
            return Version(text)
        except InvalidVersion:
            return None
    except ImportError:  # fallback: numbers only, pre-releases sort before their release
        m = re.match(r"^(\d+(?:\.\d+)*)(?:[-.]?([a-zA-Z]+)\.?(\d*))?$", text)
        if not m:
            return None
        nums = tuple(int(x) for x in m.group(1).split(".")) + (0,) * 3
        pre = (0, m.group(2) or "", int(m.group(3) or 0)) if m.group(2) else (1, "", 0)
        return nums[:3] + pre


def _request(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "VT.PR.WV-updater", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _fetch_releases():
    with _request(RELEASES_URL) as resp:
        data = json.load(resp)
    releases = []
    for r in data:
        if r.get("draft"):
            continue
        version = parse_version(r.get("tag_name"))
        if version is None:
            continue
        zip_asset = next((a for a in r.get("assets") or [] if a.get("name", "").lower().endswith(".zip")), None)
        releases.append({
            "tag": r.get("tag_name"),
            "version": str(r.get("tag_name") or "").lstrip("vV"),
            "name": r.get("name") or r.get("tag_name"),
            "prerelease": bool(r.get("prerelease")),
            "published_at": r.get("published_at"),
            "notes": r.get("body") or "",
            "url": r.get("html_url"),
            "download": zip_asset["browser_download_url"] if zip_asset else r.get("zipball_url"),
        })
    releases.sort(key=lambda r: parse_version(r["tag"]), reverse=True)
    return releases


def releases(force=False, include_prerelease=False):
    """{current, releases: [... with "state": newer/current/older], latest, update_available,
    checked_at, error}. Cached; force=True asks GitHub right away."""
    with _lock:
        stale = time.time() - _cache["at"] > (60 if force else CACHE_SECONDS)
        if _cache["releases"] is None or stale:
            try:
                _cache["releases"] = _fetch_releases()
                _cache["error"] = None
            except Exception as e:  # offline, rate limited, repo gone, ...
                _cache["error"] = f"Couldn't reach GitHub: {e}"
            _cache["at"] = time.time()
        found = list(_cache["releases"] or [])
        error = _cache["error"]
        checked_at = _cache["at"]

    current = current_version()
    cur = parse_version(current)
    out = []
    for r in found:
        v = parse_version(r["tag"])
        state = "current" if v == cur else "newer" if cur is None or v > cur else "older"
        # Pre-releases are only offered when asked for - but the installed one (and older
        # ones, for going back) always show.
        if r["prerelease"] and not include_prerelease and state == "newer":
            continue
        out.append({**r, "state": state})
    newer = [r for r in out if r["state"] == "newer"]
    return {
        "current": current, "repo": UPDATE_REPO, "releases": out,
        "latest": newer[0] if newer else None, "update_available": bool(newer),
        "checked_at": checked_at, "error": error,
    }


def _protected(rel):
    rel = rel.strip("/")
    if rel in PROTECTED_FILES or rel.startswith("vinetrimmer/key_store.db"):
        return True
    if rel.startswith("vinetrimmer/cookies/") and not rel.endswith(".gitkeep"):
        return True  # cookie files are personal; only the (empty) folder layout ships
    return any(rel == d or rel.startswith(d + "/") for d in PROTECTED_DIRS)


def install(tag, include_prerelease=False):
    """Download and install release `tag`. Returns a summary dict; raises ValueError
    with a readable message on problems (nothing is changed then)."""
    if not _install_lock.acquire(blocking=False):
        raise ValueError("An update is already being installed.")
    try:
        info = releases(include_prerelease=True)
        release = next((r for r in info["releases"] if r["tag"] == tag), None)
        if not release:
            raise ValueError(f"Release {tag} not found on GitHub.")

        with _request(release["download"], timeout=120) as resp:
            payload = resp.read()
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile:
            raise ValueError("The downloaded release isn't a valid zip file.")

        names = [n for n in archive.namelist() if not n.endswith("/")]
        if not names:
            raise ValueError("The release zip is empty.")
        # GitHub's source zips put everything in one top folder ("Owner-Repo-sha/").
        tops = {n.split("/", 1)[0] for n in names}
        strip = (tops.pop() + "/") if len(tops) == 1 and all("/" in n for n in names) else ""
        files = []
        for n in names:
            rel = n[len(strip):] if strip and n.startswith(strip) else n
            parts = PurePosixPath(rel).parts
            if not rel or ".." in parts or rel.startswith("/") or ":" in parts[0]:
                continue  # never write outside the project
            if _protected(rel) or "__pycache__" in parts:
                continue
            files.append((n, rel))
        if not any(rel in ("VERSION", "webui/app.py") for _, rel in files):
            raise ValueError("This doesn't look like a VT.PR.WV release (no VERSION / webui/app.py in it).")

        before = {f: _read(PROJECT_ROOT / f) for f in DEPENDENCY_FILES}
        old_version = current_version()

        # Back up everything about to be replaced, then write.
        BACKUP_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = BACKUP_DIR / f"before-{release['version']}-from-{old_version}-{stamp}.zip"
        changed, added = 0, 0
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as backup:
            for name, rel in files:
                target = PROJECT_ROOT / rel
                data = archive.read(name)
                if target.exists():
                    if target.read_bytes() == data:
                        continue
                    backup.write(target, rel)
                    changed += 1
                else:
                    added += 1
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".update-tmp")
                tmp.write_bytes(data)
                os.replace(tmp, target)
        if not changed:
            backup_path.unlink(missing_ok=True)

        deps = [f for f in DEPENDENCY_FILES if _read(PROJECT_ROOT / f) != before[f]]
        return {
            "installed": release["version"], "previous": old_version,
            "changed": changed, "added": added,
            "backup": str(backup_path) if changed else None,
            "dependencies_changed": deps,
        }
    finally:
        _install_lock.release()


def _read(path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def wait_for_port(host, port, timeout=30):
    """For a restart: wait until the previous web UI process has let go of the port."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host if host not in ("localhost",) else "127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.5)
    return False


def restart_web_ui(args):
    """Start a fresh web UI (in its own console window on Windows) that waits for this
    one to exit, then exit this one. The saved queue comes back by itself."""
    import subprocess
    import sys
    env = dict(os.environ, VT_RESTART_WAIT="1")
    kwargs = {"cwd": str(PROJECT_ROOT), "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        kwargs["start_new_session"] = True
    # Always through run_webui.py, however this one was started (run_webui.py / -m webui).
    subprocess.Popen([sys.executable, str(PROJECT_ROOT / "run_webui.py"), *args], **kwargs)
    threading.Timer(1.0, lambda: os._exit(0)).start()
