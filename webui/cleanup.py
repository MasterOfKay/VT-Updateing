"""
Cleanup functions for the temp folder, used by the web UI and the CLI.
The web UI runs a background job queue worker thread, which can leave temp files behind if the process is killed or crashes. 
The CLI runs a single job in the foreground, and cleans up after itself, but if the process is killed or crashes it can also leave temp files behind.
"""
import os
import shutil
import stat
import time
from pathlib import Path


def snapshot(temp_dir):
    """{name: signature} of the temp folder's top-level entries."""
    out = {}
    try:
        with os.scandir(temp_dir) as it:
            for entry in it:
                out[entry.name] = _signature(entry)
    except OSError:
        pass
    return out


def _signature(entry):
    try:
        if entry.is_dir(follow_symlinks=False):
            return "dir"
        st = entry.stat(follow_symlinks=False)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _touched_since(path, since):
    """True if anything inside directory `path` was modified at/after `since`."""
    try:
        if os.stat(path).st_mtime >= since:
            return True
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    if os.stat(os.path.join(root, name)).st_mtime >= since:
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def _on_rm_error(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc_info[1]


def _remove(path, attempts=5):
    """Delete a file/folder, retrying a few times: a downloader child process that
    is still shutting down can hold a file lock on Windows for a moment."""
    for i in range(attempts):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, onerror=_on_rm_error)
            elif os.path.lexists(path):
                try:
                    os.unlink(path)
                except PermissionError:
                    os.chmod(path, stat.S_IWRITE)
                    os.unlink(path)
            return True
        except OSError:
            if i == attempts - 1:
                return False
            time.sleep(1)
    return False


def leftovers(temp_dir, before, started_at):
    """Top-level temp entries the job created or modified (see module docstring)."""
    found = []
    try:
        with os.scandir(temp_dir) as it:
            entries = list(it)
    except OSError:
        return found
    for entry in entries:
        if entry.name not in before:
            found.append(entry.path)
        elif before[entry.name] == "dir":
            if _touched_since(entry.path, started_at):
                found.append(entry.path)
        elif _signature(entry) != before[entry.name]:
            found.append(entry.path)
    return found


def clean_job_leftovers(temp_dir, before, started_at):
    """Returns (removed, failed) lists of entry names."""
    removed, failed = [], []
    for path in leftovers(temp_dir, before, started_at):
        (removed if _remove(path) else failed).append(os.path.basename(path))
    return removed, failed


def usage(temp_dir):
    """(entry count, total bytes) of the temp folder."""
    count, total = 0, 0
    try:
        with os.scandir(temp_dir) as it:
            for entry in it:
                count += 1
                if entry.is_dir(follow_symlinks=False):
                    for root, _, files in os.walk(entry.path):
                        for f in files:
                            try:
                                total += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                else:
                    try:
                        total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
    except OSError:
        pass
    return count, total


def wipe(temp_dir):
    """Delete everything inside the temp folder (not the folder itself)."""
    removed, failed = [], []
    temp_dir = Path(temp_dir)
    if not temp_dir.is_dir():
        return removed, failed
    for entry in list(temp_dir.iterdir()):
        (removed if _remove(str(entry), attempts=2) else failed).append(entry.name)
    return removed, failed
