"""
Queue state persistence: saving and loading the queue's job list and pending job IDs to/from a JSON file.
"""
import json
import os
import threading
import time
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent / "queue_state.json"
VERSION = 1
_lock = threading.Lock()


def save(jobs, pending):
    """jobs: job state dicts in display order; pending: queued job ids in run order."""
    data = json.dumps({"version": VERSION, "saved_at": time.time(), "jobs": jobs,
                       "pending": list(pending)}, indent=1)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fd:
            fd.write(data)
            fd.flush()
            os.fsync(fd.fileno())
        os.replace(tmp, STATE_PATH)


def load():
    """(job dicts, pending ids), or ([], []) if there's no (readable) saved queue. An
    unreadable file is kept aside as queue_state.json.bad rather than overwritten."""
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except OSError:
        return [], []
    try:
        data = json.loads(raw)
        jobs = data["jobs"]
        if not isinstance(jobs, list):
            raise ValueError("jobs is not a list")
        pending = [p for p in data.get("pending") or [] if isinstance(p, str)]
        return [j for j in jobs if isinstance(j, dict) and j.get("id")], pending
    except (ValueError, KeyError, TypeError):
        try:
            os.replace(STATE_PATH, STATE_PATH.with_name(STATE_PATH.name + ".bad"))
        except OSError:
            pass
        return [], []
