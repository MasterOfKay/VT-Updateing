"""
Pinned shows: ongoing shows that get a new episode added.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import command_builder, retry

PINS_PATH = Path(__file__).resolve().parent / "pins.json"
TVMAZE_URL = "https://api.tvmaze.com/singlesearch/shows?q={q}&embed=nextepisode"
AIR_REFRESH_SECONDS = 12 * 3600
AFTER_AIR_SECONDS = 2 * 3600  # check this long after the TV air time
ACTIVE = ("queued", "running")

# Form fields a pin doesn't carry over into its jobs.
_FORM_DROP = ("wanted", "meta", "mode", "probe_mode", "combos", "check_quality", "extend",
              "check_episode", "list_", "selected", "keys", "first_title")


def _ep(e):
    return "S{:02}E{:02}".format(*e) if e else None


class Pins:
    def __init__(self, job_queue):
        self.queue = job_queue
        self._lock = threading.RLock()
        self._pins = self._load()
        job_queue.add_finished_hook(self._on_job_finished)
        threading.Thread(target=self._scheduler, daemon=True).start()

    # ---- storage ----

    def _load(self):
        try:
            data = json.loads(PINS_PATH.read_text(encoding="utf-8"))
            return {p["id"]: p for p in data.get("pins", []) if isinstance(p, dict) and p.get("id")}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self):
        with self._lock:
            data = json.dumps({"version": 1, "pins": list(self._pins.values())}, indent=1)
        tmp = PINS_PATH.with_name(PINS_PATH.name + ".tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, PINS_PATH)
        except OSError:
            pass

    # ---- reading ----

    def _active_job(self, pin):
        for job in self.queue.jobs_snapshot():
            if job.pin_id == pin["id"] and job.status in ACTIVE:
                return job
        return None

    def list(self):
        with self._lock:
            pins = sorted(self._pins.values(), key=lambda p: (p.get("name") or p["title"]).lower())
        out = []
        for p in pins:
            job = self._active_job(p)
            out.append({
                **{k: v for k, v in p.items() if k != "form"},
                "next_episode": _ep(p.get("next_episode")),
                "last_downloaded": _ep(p.get("last_downloaded")),
                "active_job": {"id": job.id, "status": job.status} if job else None,
            })
        return out

    def pin_for_job(self, job):
        return self._pins.get(job.pin_id) if job.pin_id else None

    # ---- pinning ----

    def _find(self, service, title):
        return next((p for p in self._pins.values()
                     if p["service"] == service and p["title"].strip() == (title or "").strip()), None)

    def _new_pin(self, service, title, form, name=None, year=None, poster=None):
        hours = retry.get_settings()["pin_check_hours"]
        return {
            "id": uuid.uuid4().hex[:10], "service": service, "title": title.strip(),
            "form": {k: v for k, v in (form or {}).items() if k not in _FORM_DROP},
            "name": name, "year": year, "poster": poster,
            "next_episode": None, "last_downloaded": None, "last_season": None,
            "interval_hours": hours, "created_at": time.time(),
            "last_check_at": None, "next_check_at": None, "last_result": "Pinned",
            "air": None,
        }

    def pin_job(self, job_id):
        """Pin the show of a queue job. Already queued/running: that job is the pin's
        first check. Otherwise the show goes (back) into the queue right away - from
        the episode after the last one that job downloaded, or the one it got stuck on."""
        job = self.queue.get(job_id)
        if not job or not job.form or not job.service:
            raise ValueError("This job can't be pinned (it has no saved download options).")
        if job.retried_by and self.queue.get(job.retried_by):
            job = self.queue.get(job.retried_by)  # pin the newest attempt
        if job.status == "done" and not job.current_episode and not job.last_season:
            raise ValueError("Only shows can be pinned, and this download had no episodes.")
        with self._lock:
            pin = self._find(job.service, job.title) or self._new_pin(
                job.service, job.title, job.form, job.display_name, job.display_year, job.poster)
            self._pins[pin["id"]] = pin
            pin["name"] = job.display_name or pin["name"]
            pin["poster"] = job.poster or pin["poster"]
            pin["last_season"] = job.last_season or pin["last_season"]
            job.pin_id = pin["id"]
            if job.status == "done" and job.current_episode:
                pin["last_downloaded"] = list(job.current_episode)
                pin["next_episode"] = [job.current_episode[0], job.current_episode[1] + 1]
            elif job.current_episode:
                pin["next_episode"] = list(job.current_episode)
            elif job.resume_point:
                pin["next_episode"] = list(job.resume_point)
        self.queue.save()
        self._refresh_air(pin, force=True)
        if job.status not in ACTIVE:
            self.check_now(pin["id"])
        self._save()
        return pin

    def pin_form(self, form, title=None):
        """Pin from the Download page: the form as it is, starting at its -w (if it
        starts at an episode), and queue the first check right away."""
        service = form.get("service")
        title = (title or form.get("title") or "").strip()
        if not service or not title:
            raise ValueError("Select a service and enter a link / ID first.")
        meta = form.get("meta") if isinstance(form.get("meta"), dict) else {}
        with self._lock:
            pin = self._find(service, title)
            if not pin:
                pin = self._new_pin(service, title, form, meta.get("name"), meta.get("year"), meta.get("poster"))
                self._pins[pin["id"]] = pin
            else:
                pin["form"] = {k: v for k, v in form.items() if k not in _FORM_DROP}
            start = retry.wanted_start(form.get("wanted"))
            if start:
                pin["next_episode"] = list(start)
        self._refresh_air(pin, force=True)
        self.check_now(pin["id"])
        self._save()
        return pin

    def unpin(self, pin_id):
        with self._lock:
            pin = self._pins.pop(pin_id, None)
        if not pin:
            return False
        for job in self.queue.jobs_snapshot():
            if job.pin_id == pin_id:
                job.pin_id = None  # queued checks stay queued as normal jobs
        self.queue.save()
        self._save()
        return True

    def set_interval(self, pin_id, hours):
        with self._lock:
            pin = self._pins.get(pin_id)
            if not pin:
                return None
            pin["interval_hours"] = max(1, min(24 * 14, int(hours)))
            if pin.get("last_check_at") and not self._active_job(pin):
                pin["next_check_at"] = self._next_check(pin, pin["last_check_at"])
        self._save()
        return pin

    # ---- checking ----

    def _wanted(self, pin):
        nxt = pin.get("next_episode")
        if not nxt:
            return pin["form"].get("wanted")
        s, e = nxt
        # Up to one season past the last known one, so a new season's premiere counts too.
        end = max(s, pin.get("last_season") or s) + 1
        return f"S{s:02}E{e:02}-S{end:02}"

    def check_now(self, pin_id):
        """Queue a check for this pin (unless one is already queued/running) and start
        the queue if it's idle - a paused queue stays paused."""
        with self._lock:
            pin = self._pins.get(pin_id)
        if not pin:
            return None
        active = self._active_job(pin)
        if active:
            return active
        form = dict(pin["form"], service=pin["service"], title=pin["title"])
        wanted = self._wanted(pin)
        if wanted:
            form["wanted"] = wanted
        else:
            form.pop("wanted", None)
        from . import services_info  # lazy: see queue_manager.retry()
        info = services_info.get_service_options(pin["service"])
        argv = command_builder.build_argv(form, pin["service"], info["options"] if info else [])
        idle = self.queue.state()["state"] == "idle"
        job = self.queue.add(pin["service"], pin["title"], argv, command_builder.format_command(argv),
                             form=form, pin_id=pin["id"], display_name=pin.get("name"),
                             display_year=pin.get("year"), poster=pin.get("poster"))
        with self._lock:
            pin["last_result"] = f"Checking for {_ep(pin.get('next_episode')) or 'new episodes'}…"
        self._save()
        if idle:
            self.queue.start()
        return job

    def _on_job_finished(self, job):
        pin = self.pin_for_job(job)
        if not pin:
            return
        now = time.time()
        with self._lock:
            pin["name"] = job.display_name or pin.get("name")
            pin["year"] = job.display_year or pin.get("year")
            pin["poster"] = job.poster or pin.get("poster")
            if job.last_season:
                pin["last_season"] = max(job.last_season, pin.get("last_season") or 0)
            got = job.current_episode
            if job.status == "done":
                if got:
                    pin["last_downloaded"] = list(got)
                    pin["next_episode"] = [got[0], got[1] + 1]
                    pin["last_result"] = f"Downloaded up to {_ep(got)}"
                else:
                    pin["last_result"] = f"No new episode yet ({_ep(pin.get('next_episode')) or 'nothing new'})"
            elif job.status == "stopped":
                if got:
                    pin["next_episode"] = list(got)
                pin["last_result"] = f"Stopped by you before {_ep(got) or 'the next episode'}"
            else:
                if got:
                    pin["next_episode"] = list(got)  # everything before it was downloaded
                pin["last_result"] = f"Last check {job.status}" + (f" at {_ep(got)}" if got else "")
            pin["last_check_at"] = now
            pin["next_check_at"] = self._next_check(pin, now)
        self._refresh_air(pin)
        self._save()

    def _next_check(self, pin, after):
        nxt = after + pin.get("interval_hours", 24) * 3600
        air = ((pin.get("air") or {}).get("next") or {})
        airstamp = air.get("airstamp_ts")
        if airstamp and airstamp + AFTER_AIR_SECONDS > after:
            # Known air date ahead: check right after it instead of every N hours until then.
            nxt = airstamp + AFTER_AIR_SECONDS
        return nxt

    def _scheduler(self):
        while True:
            time.sleep(60)
            try:
                now = time.time()
                with self._lock:
                    due = [p["id"] for p in self._pins.values()
                           if p.get("next_check_at") and p["next_check_at"] <= now]
                for pin_id in due:
                    pin = self._pins.get(pin_id)
                    if pin and not self._active_job(pin):
                        self.check_now(pin_id)
                with self._lock:
                    stale = [p for p in self._pins.values()
                             if now - ((p.get("air") or {}).get("checked_at") or 0) > AIR_REFRESH_SECONDS]
                for pin in stale:
                    self._refresh_air(pin)
            except Exception:
                pass  # keep scheduling whatever one pin's problem was

    # ---- air dates (TVmaze, opt-in) ----

    def _refresh_air(self, pin, force=False):
        if not retry.get_settings()["fetch_air_dates"] or not pin.get("name"):
            return
        air = pin.get("air") or {}
        if not force and time.time() - (air.get("checked_at") or 0) < AIR_REFRESH_SECONDS:
            return

        def fetch():
            info = {"checked_at": time.time(), "show": None, "next": None}
            try:
                url = TVMAZE_URL.format(q=urllib.parse.quote(pin["name"]))
                req = urllib.request.Request(url, headers={"User-Agent": "VT.PR.WV"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    show = json.load(resp)
                info["show"] = show.get("name")
                info["status"] = show.get("status")  # "Running", "Ended", ...
                nxt = (show.get("_embedded") or {}).get("nextepisode")
                if nxt and nxt.get("airstamp"):
                    from datetime import datetime
                    ts = datetime.fromisoformat(nxt["airstamp"].replace("Z", "+00:00")).timestamp()
                    info["next"] = {"season": nxt.get("season"), "number": nxt.get("number"),
                                    "name": nxt.get("name"), "airstamp": nxt["airstamp"], "airstamp_ts": ts}
            except Exception as e:  # not found (404), offline, ...
                info["error"] = str(e)[:200]
            with self._lock:
                pin["air"] = info
                if not self._active_job(pin) and pin.get("last_check_at"):
                    pin["next_check_at"] = self._next_check(pin, pin["last_check_at"])
            self._save()

        threading.Thread(target=fetch, daemon=True).start()


pins = None


def init(job_queue):
    global pins
    if pins is None:
        pins = Pins(job_queue)
    return pins
