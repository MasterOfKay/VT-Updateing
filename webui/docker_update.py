"""
Updating from the Update tab inside Docker.

A container starts from the files in its image, so a release installed there would be
gone as soon as the container is recreated. The release is therefore also kept in the
data volume, and the entrypoint puts it back at the next start (`python -m
webui.docker_update`). A rebuilt image wins over the kept release, unless the kept one
is newer than what the image contains.

Only program files can be updated this way. A release that needs other Python packages
needs a new image.
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# What the image was built with (written by the Dockerfile).
IMAGE_DIR = Path(os.environ.get("VT_IMAGE_DIR") or "/opt/image")
STORE = Path(os.environ.get("VT_UPDATE_DIR") or "/data/update")
BACKUP_DIR = STORE.parent / "backups"

REBUILD = "update the project folder on the server to that release, then run: docker compose up -d --build"


def image():
    """{build, version, pyproject} of the running image, or None for one that is too old."""
    try:
        return {
            "build": (IMAGE_DIR / "build").read_text(encoding="utf-8").strip(),
            "version": (IMAGE_DIR / "VERSION").read_text(encoding="utf-8").strip(),
            "pyproject": (IMAGE_DIR / "pyproject.toml").read_bytes(),
        }
    except OSError:
        return None


def version_key(text):
    """"V1.0.4.1" as (1, 0, 4, 1)."""
    return tuple(int(number) for number in re.findall(r"\d+", text or ""))


def dependencies(pyproject):
    """The packages a pyproject.toml asks for; its other contents don't matter here."""
    import tomllib

    data = tomllib.loads(pyproject.decode("utf-8-sig").replace("\r\n", "\n"))
    return (data.get("project", {}).get("dependencies"),
            data.get("tool", {}).get("poetry", {}).get("dependencies"))


def refuse_if_needs_rebuild(release_files, built):
    """release_files: {path in the project: contents}. Raises ValueError with what to do."""
    if "webui/docker_update.py" not in release_files:
        raise ValueError(f"This release is from before updates worked inside Docker. To use it, {REBUILD}")
    wanted = release_files.get("pyproject.toml")
    if wanted is not None and dependencies(wanted) != dependencies(built["pyproject"]):
        raise ValueError("This release needs different Python packages, which only a new image brings. Nothing was "
                         f"changed. To install it, {REBUILD}")


def rebuild_notes(release_files):
    """What of a release this way of updating leaves out, as sentences for the person."""
    def differs(path, name):
        if path not in release_files:
            return False
        try:
            built = (IMAGE_DIR / name).read_bytes()
        except OSError:
            return False
        return release_files[path].replace(b"\r\n", b"\n") != built.replace(b"\r\n", b"\n")

    changed = [differs("Dockerfile", "Dockerfile"), differs("docker/entrypoint.sh", "entrypoint.sh"),
               differs("poetry.lock", "poetry.lock")]
    if not any(changed):
        return []
    return ["This release also changes the Docker image itself (tools or package versions). It works without that; "
            f"when convenient, {REBUILD}"]


def check_program():
    """Why the program as it is on disk wouldn't start, or None."""
    # Loading the web UI opens its saved queue and writes to it, so the trial gets an
    # empty folder instead of the real one.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        env = {**os.environ, "VT_WEBUI_DATA_DIR": scratch}
        for args in (["-c", "import webui.app"], ["-m", "vinetrimmer.vinetrimmer", "dl", "--help"]):
            try:
                run = subprocess.run([sys.executable, *args], cwd=str(PROJECT_ROOT), env=env, capture_output=True,
                                     text=True, errors="replace", timeout=120, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return "it didn't answer within two minutes"
            if run.returncode:
                lines = [line.strip() for line in (run.stderr or run.stdout).splitlines() if line.strip()]
                return lines[-1][:300] if lines else f"exit code {run.returncode}"
    return None


def _in_place(archive, files, root):
    """Whether every file of a release is already there, e.g. after a mere restart."""
    return all((root / rel).is_file() and (root / rel).read_bytes() == archive.read(entry) for entry, rel in files)


def keep(release, payload, version, built):
    """Remember an installed release (its zip) for the next containers."""
    STORE.mkdir(parents=True, exist_ok=True)
    state = {"tag": release.get("tag"), "name": release.get("name"), "published_at": release.get("published_at"),
             "version": version or release.get("tag"), "build": built["build"]}
    for name, data in (("release.zip", payload), ("state.json", json.dumps(state, indent=2).encode("utf-8"))):
        tmp = STORE / f"{name}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, STORE / name)


def kept():
    try:
        return json.loads((STORE / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def forget():
    """Drop the kept release: from now on the image's own files are what's installed."""
    from . import updater

    for path in (STORE / "state.json", STORE / "release.zip", updater.INSTALLED_FILE):
        path.unlink(missing_ok=True)


def restore():
    """At container start: put the kept release back over the image's files."""
    from . import updater

    state, built = kept(), image()
    if not state or not built:
        return
    name = state.get("name") or state.get("tag")
    rebuilt = state.get("build") != built["build"]
    if rebuilt and version_key(state.get("version")) <= version_key(built["version"]):
        print(f"Update: using the rebuilt image ({built['version']}); the update to {name} kept from before is dropped.")
        forget()
        return
    try:
        payload = (STORE / "release.zip").read_bytes()
        archive, files = updater.release_files(payload)
        if rebuilt:
            refuse_if_needs_rebuild({rel: archive.read(entry) for entry, rel in files}, built)
        changed, added = 0, []
        if not _in_place(archive, files, updater.PROJECT_ROOT):
            healthy = check_program() is None
            backup = BACKUP_DIR / "before-restoring-the-update.zip"
            changed, added = updater.write_files(archive, files, backup)
            problem = check_program() if healthy else None
            if problem:
                updater.undo(backup, added)
                raise ValueError(f"it doesn't start: {problem}")
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"Update: couldn't put {name} back, so the image's version ({built['version']}) runs: {error}")
        forget()
        return
    if rebuilt:
        keep(state, payload, state.get("version"), built)
    if changed or added:
        print(f"Update: {name} put back ({changed} file(s) changed, {len(added)} new).")


if __name__ == "__main__":
    restore()
