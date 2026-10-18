"""
Experimental.

Checking a service's availability and what tracks it offers, without downloading anything. 
The web UI's "Check" tab does this, and the CLI's --probe option does it for a single title.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque

from . import command_builder, posters, retry
from .queue_manager import PROJECT_ROOT, _kill_tree, vinetrimmer_env

STEP_TIMEOUT = 300

NETFLIX_COMBOS = {
    "h264:sdr": [("h264", "sdr")],
    "h265:sdr": [("h265", "sdr")],
    "h265:hdr10": [("h265", "hdr10")],
    "h265:dv": [("h265", "dv")],
    "h265:hybrid": [("h265", "hdr10"), ("h265", "dv")],
    "av1:sdr": [("av1", "sdr")],
    "av1:hdr10": [("av1", "hdr10")],  # AV1's HDR is HDR10+
    "av1:hybridplus": [("av1", "hdr10"), ("h265", "dv")],
    "vp9:sdr": [("vp9", "sdr")],
    "vp9:hdr10": [("vp9", "hdr10")],
}
# "Everything": every manifest once (the hybrids are covered by their parts).
NETFLIX_ALL = ["h264:sdr", "h265:sdr", "h265:hdr10", "h265:dv", "av1:sdr", "av1:hdr10", "vp9:sdr", "vp9:hdr10"]
# Netflix check quality -> -drm: PlayReady (default) is the only way to 4K, Widevine
# tops out at 1080p.
NETFLIX_QUALITY_DRM = {"2160": "", "1080": "widevine"}
CODEC_NAMES = {"h264": "H.264", "h265": "H.265", "av1": "AV1", "vp9": "VP9"}

# Form fields that would change what gets listed or make the run do more than list.
_STRIP = ("vcodec", "range_", "quality", "vbitrate", "abitrate", "acodec", "channels", "atmos",
          "audio_only", "subs_only", "chapters_only", "no_subs", "no_audio", "no_video",
          "no_chapters", "keys", "selected", "worst", "audio_pick", "list_", "first_title",
          "cache", "no_cache", "mode", "probe_mode", "combos", "check_quality", "extend", "meta",
          "check_episode")

FORCED_OPTIONS = {
    "DisneyPlus": {"svc__tier_unlimits": True, "svc__imax": True},
}

MODES = ("combos", "all", "h265", "h264", "both", "keys")

_lock = threading.Lock()
_probes = {}
_active = None


class Probe:
    def __init__(self, form, mode, combos=None, quality=None, base=None):
        self.id = uuid.uuid4().hex[:12]
        self.service = form.get("service")
        self.forced_options = dict(FORCED_OPTIONS.get(self.service, {}))
        self.form = {**form, **self.forced_options}
        self.mode = mode
        self.combos = [c for c in (combos or []) if c in NETFLIX_COMBOS]
        # Which episode to check (-w, e.g. "S02E01"): a new episode may not have every
        # dub/sub yet, and seasons can differ. None = the first (wanted) title.
        self.episode = (form.get("check_episode") or "").strip().upper() or (base.episode if base else None)
        self.quality = quality if quality in NETFLIX_QUALITY_DRM else None
        if self.service == "Netflix" and mode == "keys":
            # The key check is about 4K: PlayReady (the default -drm). Widevine keys
            # come from the CDM anyway.
            self.form.pop("svc__drm_system", None)
        elif self.service == "Netflix" and self.quality:
            drm = NETFLIX_QUALITY_DRM[self.quality]
            if drm:
                self.form["svc__drm_system"] = drm
            else:
                self.form.pop("svc__drm_system", None)
        self.status = "running"  # running -> done | failed | cancelled
        self.steps = []
        self.result = None
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self._proc = None
        self._cancel = False
        # (step, title entry) per successful run - an extension starts with the
        # previous check's, so the result covers both.
        self._entries = list(base._entries) if base else []
        self._done_runs = dict(base._done_runs) if base else {}  # (codec, range) -> status
        if base and not self.quality:
            self.quality = base.quality

    def to_dict(self):
        return {
            "id": self.id, "service": self.service, "mode": self.mode, "status": self.status,
            "quality": self.quality,
            "steps": [{k: v for k, v in s.items() if k != "log"} for s in self.steps],
            "result": self.result, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }

    # ---- running ----

    def _add_step(self, vcodec, range_, label=None):
        if label is None:
            label = f"{vcodec.upper()} {range_.upper() if range_ != 'sdr' else 'SDR'}" if range_ \
                else vcodec.upper()
        step = {"label": label, "vcodec": vcodec, "range": range_, "status": "pending",
                "message": None, "log": deque(maxlen=40)}
        self.steps.append(step)
        return step

    def _argv(self, vcodec, range_, json_path, keys=False):
        if keys:
            # Exactly what's on the form (codec, range, quality, languages, -drm, ...).
            form = {k: v for k, v in self.form.items()
                    if k not in ("list_", "selected", "mode", "probe_mode", "combos",
                                 "check_quality", "extend", "meta", "no_cache", "check_episode")}
            form["keys"] = True
            form["cache"] = True  # vaults only - never a license request
        else:
            form = {k: v for k, v in self.form.items() if k not in _STRIP}
            form["vcodec"] = vcodec
            if range_:
                form["range_"] = range_
            form["list_"] = True
        form["first_title"] = True
        if self.episode:
            form["wanted"] = self.episode
        from . import services_info  # lazy: see queue_manager.retry()
        info = services_info.get_service_options(self.service)
        argv = command_builder.build_argv(form, self.service, info["options"] if info else [])
        # Global options go before the service name; "dl" is argv[0].
        return [argv[0], "--list-json", json_path, *argv[1:]]

    def _run_step(self, step, keys=False):
        if self._cancel:
            step["status"] = "cancelled"
            return None
        step["status"] = "running"
        fd, json_path = tempfile.mkstemp(prefix="vt_probe_", suffix=".json")
        os.close(fd)
        os.unlink(json_path)  # vinetrimmer creates it only once it has a track list
        try:
            cmd = [sys.executable, "-m", "vinetrimmer.vinetrimmer",
                   *self._argv(step["vcodec"], step["range"], json_path, keys=keys)]
            step["command"] = command_builder.format_command(cmd[3:])
            try:
                proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        env=vinetrimmer_env())
            except OSError as e:
                step["status"], step["message"] = "error", f"Could not start: {e}"
                return None
            self._proc = proc
            timer = threading.Timer(STEP_TIMEOUT, lambda: _kill_tree(proc))
            timer.start()
            try:
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line and not line.startswith("@@VT_PROGRESS@@") and "├─" not in line:
                        step["log"].append(line)
                proc.wait()
            finally:
                timer.cancel()
                self._proc = None

            entry = None
            try:
                with open(json_path, encoding="utf-8") as fd_:
                    titles = json.load(fd_).get("titles") or []
                entry = titles[0] if titles else None
            except (OSError, ValueError):
                pass
            if self._cancel:
                step["status"] = "cancelled"
            elif keys:
                status = (entry or {}).get("keys")
                step["keys"] = status
                if status is None:
                    step["status"] = "error"
                    step["message"] = _reason(step["log"])
                else:
                    step["status"] = "ok" if status["complete"] else "unavailable"
                    if not status["complete"]:
                        missing = next((t["track"] for t in status["tracks"] if not t["found"]), None)
                        step["message"] = f"No cached key for: {missing}" if missing else \
                            "Not every key is in the vaults."
            elif entry and entry["tracks"]["video"] + entry["tracks"]["audio"]:
                step["status"] = "ok"
                self._entries.append((step, entry))
            else:
                step["status"] = "unavailable" if proc.returncode in (0, 1) else "error"
                step["message"] = _reason(step["log"])
            if not keys and step["vcodec"]:
                self._done_runs[(step["vcodec"], step["range"] or "sdr")] = step["status"]
            return entry
        finally:
            try:
                os.unlink(json_path)
            except OSError:
                pass

    def _netflix_runs(self):
        combos = NETFLIX_ALL if self.mode == "all" else self.combos
        runs = []
        for combo in combos:
            for run in NETFLIX_COMBOS[combo]:
                if run not in runs and self._done_runs.get(run) not in ("ok", "unavailable"):
                    runs.append(run)
        return runs

    def run(self):
        try:
            if self.mode == "keys":
                step = self._add_step(None, None, label="Keys in vaults")
                self._run_step(step, keys=True)
                self.result = {"keys": step.get("keys"), "message": step.get("message")}
                self.status = "cancelled" if self._cancel else "done" if step["status"] in ("ok", "unavailable") \
                    else "failed"
                if self.status == "failed":
                    self.error = step.get("message") or "The key check failed."
                return
            if self.service == "Netflix":
                for codec, range_ in self._netflix_runs():
                    self._run_step(self._add_step(codec, range_))
            elif self.mode == "h264":
                self._run_step(self._add_step("h264", None))
            else:
                first = self._run_step(self._add_step("h265", None))
                videos = (first or {}).get("tracks", {}).get("video", []) if first else []
                has_h265 = any(v["codec"] == "H.265" for v in videos)
                if has_h265 and not any(v["range"] != "SDR" for v in videos):
                    self._run_step(self._add_step("h265", "hybrid"))
                if not has_h265 or self.mode == "both":
                    self._run_step(self._add_step("h264", None))
            if self._cancel:
                self.status = "cancelled"
            elif self._entries:
                self.result = merge(self._entries)
                self.result["forced_options"] = self.forced_options
                if self.service == "Netflix":
                    self._netflix_extras()
                self._add_poster()
                self.status = "done"
            else:
                self.status = "failed"
                self.error = next((s["message"] for s in self.steps if s.get("message")), None) or \
                    "No run returned a track list."
        except Exception as e:  # report it on the page instead of a silent dead thread
            self.status, self.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            self.finished_at = time.time()
            global _active
            with _lock:
                if _active is self:
                    _active = None

    def _netflix_extras(self):
        """What was asked and how it went (so the page can tell "not available" from
        "not checked"), the check's quality, and SDR inferred from HDR."""
        res = self.result
        res["quality"] = self.quality or "2160"
        res["checked"] = [{"codec": CODEC_NAMES.get(c, c), "vcodec": c, "range": r, "status": s}
                          for (c, r), s in self._done_runs.items()]
        assumed = []
        any_hdr = any(rng != "SDR" for cells in res["matrix"].values() for rng in cells)
        if any_hdr:
            for vc in ("h264", "h265", "av1"):
                name = CODEC_NAMES[vc]
                if "SDR" not in res["matrix"].get(name, {}) and (vc, "sdr") not in self._done_runs:
                    assumed.append({"codec": name, "vcodec": vc, "range": "SDR"})
        res["assumed"] = assumed

    def _add_poster(self):
        t = self.result["title"]
        poster = None
        if retry.get_settings()["fetch_posters"]:
            poster = posters.lookup(t.get("name"), t.get("year"), t.get("type"))
        self.result["poster"] = poster or t.get("image")

    def cancel(self):
        self._cancel = True
        proc = self._proc
        if proc and proc.poll() is None:
            _kill_tree(proc)


def _reason(log_lines):
    """The most telling line of a failed run: vinetrimmer's own error/critical line."""
    lines = list(log_lines)
    for marker in (" [C] ", " [E] ", " [W] ", "Error", "error", " - "):
        for line in reversed(lines):
            if marker in line:
                return line.split(" : ", 1)[-1].strip()[:300]
    return (lines[-1].split(" : ", 1)[-1].strip()[:300]) if lines else "No output."


def merge(entries):
    """One overview out of every successful run's track list."""
    base = entries[0][1]
    videos, audios, subs = {}, {}, {}
    matrix = {}
    for step, entry in entries:
        for v in entry["tracks"]["video"]:
            key = (v["codec"], v["range"], v["width"], v["height"], v["bitrate"])
            videos.setdefault(key, v)
            cell = matrix.setdefault(v["codec"], {}).setdefault(v["range"], {
                "max_height": 0, "max_width": 0, "max_bitrate": 0, "vcodec": step["vcodec"],
                "range_arg": step["range"]})
            if (v["height"] or 0) > cell["max_height"]:
                cell["max_height"], cell["max_width"] = v["height"] or 0, v["width"] or 0
            cell["max_bitrate"] = max(cell["max_bitrate"], v["bitrate"] or 0)
        for a in entry["tracks"]["audio"]:
            key = (a["language"], a["codec"], a["channels"], a["bitrate"], a["descriptive"], a["name"])
            audios.setdefault(key, a)
        for s in entry["tracks"]["subtitles"]:
            key = (s["language"], s["codec"], s["name"], s["forced"], s["sdh"], s["cc"])
            subs.setdefault(key, s)

    by_bitrate = lambda x: -(x.get("bitrate") or 0)  # noqa: E731
    return {
        "title": base["title"],
        "titles_total": base.get("titles_total"),
        "seasons": base.get("seasons"),
        "matrix": matrix,
        "video": sorted(videos.values(), key=lambda v: (-(v["height"] or 0), by_bitrate(v))),
        "audio": sorted(audios.values(), key=lambda a: (a["language"], a["descriptive"], by_bitrate(a))),
        "subtitles": sorted(subs.values(), key=lambda s: (s["language"], s["forced"], s["sdh"])),
        "default_selected": base.get("selected"),
    }


def start(form, mode=None, combos=None, quality=None, extend=None):
    """Start a check. extend: id of an earlier (finished) Netflix check whose result
    this one adds to - used for checking single variants afterwards."""
    global _active
    netflix = form.get("service") == "Netflix"
    if mode != "keys":
        if netflix:
            mode = "all" if mode == "all" else "combos"
        elif mode not in ("h265", "h264", "both"):
            mode = "h265"
    base = _probes.get(extend) if extend else None
    if base and (base.status != "done" or base.service != form.get("service") or base.mode == "keys"):
        base = None
    with _lock:
        if _active is not None:
            return None
        probe = Probe(form, mode, combos=combos, quality=quality, base=base)
        if netflix and mode == "combos" and not probe._netflix_runs() and not base:
            return "nothing"
        _probes[probe.id] = probe
        _active = probe
        # Keep only the last few finished checks around.
        for old in list(_probes)[:-10]:
            if _probes[old].status != "running":
                del _probes[old]
    threading.Thread(target=probe.run, daemon=True).start()
    return probe


def get(probe_id):
    return _probes.get(probe_id)


def active():
    return _active
