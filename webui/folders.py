"""
The folder picker on the Settings page. It browses the computer the web UI runs on
(in Docker: inside the container), and only ever lists folder names.
"""
import os
import re
import string
from pathlib import Path

from . import config_io, paths

MAX_FOLDERS = 2000
# Linux folders that are never a place for downloads.
_SYSTEM = {"/proc", "/sys", "/dev"}


def _roots():
    if os.name == "nt":
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]
    extra = ["/data", "/data/downloads", "/mnt", "/media"]
    return ["/", *[p for p in extra if os.path.isdir(p)]]


def _resolve(path):
    path = (path or "").strip()
    if not path:
        return Path(config_io.get_storage_paths()["downloads_effective"])
    folder = Path(path)
    return folder if folder.is_absolute() else config_io.PROJECT_ROOT / folder


def listing(path=None):
    folder = _resolve(path)
    # A folder that doesn't exist (yet): start at the closest one that does.
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    if not folder.is_dir():
        folder = config_io.PROJECT_ROOT
    folder = Path(os.path.abspath(folder))
    names, error = [], ""
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir() and not entry.name.startswith((".", "$")) and \
                            (os.name == "nt" or entry.path not in _SYSTEM):
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError as e:
        error = f"This folder can't be opened ({e.strerror or type(e).__name__})."
    names.sort(key=str.lower)
    return {
        "path": str(folder),
        "parent": str(folder.parent) if folder.parent != folder else "",
        "folders": [{"name": n, "path": str(folder / n)} for n in names[:MAX_FOLDERS]],
        "roots": _roots(),
        "writable": os.access(folder, os.W_OK),
        "docker": paths.in_docker(),
        "error": error,
    }


def create(parent, name):
    name = (name or "").strip()
    if not name or name in (".", "..") or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        raise ValueError('A folder name can\'t be empty or hold \\ / : * ? " < > |')
    folder = _resolve(parent)
    if not folder.is_dir():
        raise ValueError("The folder to create it in doesn't exist.")
    try:
        (folder / name).mkdir()
    except FileExistsError:
        raise ValueError("A folder or file with that name is already there.")
    except OSError as e:
        raise ValueError(f"Couldn't create the folder: {e.strerror or e}")
    return listing(str(folder / name))
