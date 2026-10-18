"""A minimal key/value cache with the shape the ported Netflix device paths expect.

The PlayReady-TV, Android-TV and Android-phone flows were written against
unshackle's Cacher, which hands back an object per key that reads through
`.data` and writes through `.set()`. vinetrimmer's BaseService.get_cache()
returns a path instead, so this wraps that path in the same two-method shape
rather than rewriting ~50 call sites across the three flows.

Values are plain strings - the callers do their own json/jsonpickle encoding.
A key that has never been written reads back as None, which is what those flows
branch on to decide whether to run a fresh handshake.
"""

import json
import os
import time


class CacheItem:
    """One cache key, backed by a file."""

    def __init__(self, path):
        self.path = path

    def _read(self):
        """The stored envelope, or None.

        Values are written as a small JSON envelope so an expiry can ride along
        with them. Anything that doesn't parse as one is treated as a bare
        string - that covers entries written before expiry existed.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fd:
                raw = fd.read()
        except (OSError, UnicodeDecodeError):
            return None
        if not raw:
            return None
        try:
            envelope = json.loads(raw)
            if isinstance(envelope, dict) and "__cache__" in envelope:
                return envelope
        except ValueError:
            pass
        return {"__cache__": 1, "value": raw, "expires": None}

    @property
    def data(self):
        """The stored string, or None if unset or expired."""
        envelope = self._read()
        if not envelope:
            return None
        if self.expired:
            return None
        return envelope.get("value") or None

    @property
    def expired(self):
        """Whether a stored expiry has passed. Entries without one never expire."""
        envelope = self._read()
        if not envelope:
            return False
        expires = envelope.get("expires")
        return expires is not None and time.time() >= expires

    def set(self, value, expiration=None):
        """Write a value, optionally with a lifetime in seconds.

        `expiration` is seconds-from-now, matching the callers that pass
        6 * 60 * 60 to rotate a generated ESN every six hours. An expiration of
        0 writes an already-dead entry, which is how callers invalidate one.
        """
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        envelope = {
            "__cache__": 1,
            "value": value if isinstance(value, str) else str(value),
            "expires": (time.time() + expiration) if expiration is not None else None,
        }
        with open(self.path, "w", encoding="utf-8") as fd:
            json.dump(envelope, fd)
        return value

    def clear(self):
        """Drop this key. Missing is already the desired state, so that's not an error."""
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Cacher:
    """Maps cache keys onto files under a service's cache directory."""

    def __init__(self, directory):
        self.directory = directory

    def get(self, key):
        # Keys are interpolated from ESNs and account names, so they can carry
        # characters that are illegal in a Windows filename.
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(key))
        return CacheItem(os.path.join(self.directory, safe))
