"""
Queue management: Job and JobQueue classes, plus the worker thread that runs jobs in the background. 
The web UI and the CLI share the same queue state file, so the web UI can continue a CLI job that was killed or crashed, 
and the CLI can continue a web UI job that was killed or crashed.
"""
import codecs
import os
import queue as pyqueue
import tempfile
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import cleanup, command_builder, posters, queue_store, retry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


PROGRESS_MARKER = "@@VT_PROGRESS@@ "
# dl.py's exit code after stopping on its --stop-file (STOP_EXIT_CODE there).
STOP_EXIT_CODE = 3


def vinetrimmer_env():
    """Environment for running vinetrimmer as a child process - see _run_job's
    comment on why each of these is needed."""
    return dict(
        os.environ, COLUMNS="200", LINES="50",
        PYTHONIOENCODING="utf-8:backslashreplace", PYTHONUNBUFFERED="1",
        VT_WEBUI_PROGRESS="1",
    )


def _kill_tree(proc):
    """Stop a job's process including the downloaders/muxers it started (aria2c,
    N_m3u8DL-RE, ffmpeg, mkvmerge, ...). On Windows, terminate() only kills the
    Python process itself and leaves those running - still writing into temp."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            proc.kill()
    else:
        proc.terminate()


@dataclass
class Job:
    id: str
    service: str
    title: str
    argv: list
    command: str
    status: str = "queued"  # queued -> running -> done|failed|cancelled
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    returncode: Optional[int] = None
    output: list = field(default_factory=list)
    progress: Optional[str] = None  # latest progress snapshot (JSON), if any
    form: Optional[dict] = field(default=None, repr=False)
    attempt: int = 1  # 1 = original run, 2 = first retry, ...
    retry_of: Optional[str] = None  # job id this one retries
    retried_by: Optional[str] = None  # job id of the retry, once one was queued
    resume_point: Optional[tuple] = None  # (season, episode) this retry resumes at
    auto_streak: int = 0  # auto retries in a row that resumed at the same episode
    stop_requested: bool = False
    continuation: bool = False
    pin_id: Optional[str] = None  # the pinned show this job checks/downloads for (pins.py)
    current_episode: Optional[tuple] = None
    last_season: Optional[int] = None
    temp_dir: Optional[str] = field(default=None, repr=False)
    temp_before: Optional[dict] = field(default=None, repr=False)
    display_name: Optional[str] = None
    display_year: Optional[int] = None
    poster: Optional[str] = None
    poster_checked: bool = False
    _live: bool = field(default=False, repr=False, compare=False)
    process: object = field(default=None, repr=False, compare=False)
    _subscribers: list = field(default_factory=list, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _cancel_requested: bool = field(default=False, repr=False, compare=False)

    def to_dict(self):
        return {
            "id": self.id,
            "service": self.service,
            "title": self.title,
            "command": self.command,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "attempt": self.attempt,
            "retry_of": self.retry_of,
            "retried_by": self.retried_by,
            "resume_from": "S{:02}E{:02}".format(*self.resume_point) if self.resume_point else None,
            "current_episode": "S{:02}E{:02}".format(*self.current_episode) if self.current_episode else None,
            "display_name": self.display_name,
            "display_year": self.display_year,
            "poster": self.poster,
            "stop_requested": self.stop_requested,
            "continuation": self.continuation,
            "pin_id": self.pin_id,
        }

    # Fields written to queue_state.json (see queue_store.py). Logs aren't saved -
    # everything a retry needs from them is tracked in current_episode/last_season.
    _STATE_FIELDS = (
        "id", "service", "title", "argv", "command", "status", "created_at", "started_at",
        "finished_at", "returncode", "form", "attempt", "retry_of", "retried_by",
        "resume_point", "auto_streak", "current_episode", "last_season", "temp_dir", "temp_before",
        "display_name", "display_year", "poster", "poster_checked", "continuation", "pin_id",
    )

    def to_state(self):
        return {name: getattr(self, name) for name in self._STATE_FIELDS}

    @classmethod
    def from_state(cls, data):
        data = {k: v for k, v in data.items() if k in cls._STATE_FIELDS}
        for name in ("resume_point", "current_episode"):  # JSON turns tuples into lists
            if data.get(name) is not None:
                data[name] = tuple(data[name])
        if data.get("temp_before"):
            data["temp_before"] = {k: tuple(v) if isinstance(v, list) else v
                                   for k, v in data["temp_before"].items()}
        return cls(**data)

    def append_output(self, line):
        with self._lock:
            self.output.append(line)
            self._live = False
            subs = list(self._subscribers)
        for q in subs:
            q.put(line)

    def set_live_line(self, text, final=False):
        """Show `text` as the current in-place-redrawn line: replaces the previous
        redraw instead of adding a line per update. final=True ends the redraws
        (the line got its newline), so the next output starts a new line."""
        with self._lock:
            if self._live:
                self.output[-1] = text + "\n"
                item = ("replace", text)
            else:
                self.output.append(text + "\n")
                item = text + "\n"
            self._live = not final
            subs = list(self._subscribers)
        for q in subs:
            q.put(item)

    def set_progress(self, snapshot):
        with self._lock:
            self.progress = snapshot
            subs = list(self._subscribers)
        for q in subs:
            q.put(("progress", snapshot))

    def subscribe(self):
        """Returns a queue.Queue that first replays history, then streams live items:
        plain str log lines, ("progress", json) tuples for progress snapshots, or
        ("replace", text) tuples that replace the last line (in-place redraws).
        A None item means the stream has ended (job finished)."""
        q = pyqueue.Queue()
        with self._lock:
            for line in self.output:
                q.put(line)
            if self.progress is not None:
                q.put(("progress", self.progress))
            if self.status in ("done", "failed", "cancelled"):
                q.put(None)
            else:
                self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def send_input(self, text):
        """Type a line into the running process's stdin (answers input() prompts:
        Disney+ Web Token / OTP / profile PIN, SonyLIV accessToken, ...)."""
        with self._lock:
            proc = self.process
        if self.status != "running" or not proc or proc.poll() is not None or not proc.stdin:
            return False
        try:
            proc.stdin.write((text + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError:
            return False
        # Echo it like a terminal would, so the log shows what was answered.
        self.append_output(f"> {text}\n")
        return True

    def close_subscribers(self):
        with self._lock:
            subs = list(self._subscribers)
            self._subscribers.clear()
        for q in subs:
            q.put(None)


class JobQueue:
    def __init__(self, persist=False):
        self._jobs = {}
        self._order = []
        self._pending = deque()
        self._running_enabled = False
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._persist = persist
        self._user_paused = False  # Pause / Stop after this episode, until Start Queue
        self._finished_hooks = []
        if persist:
            self._restore()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    # ---- saved queue (queue_store.py) ----

    def _save(self):
        """Write the current queue to disk. Called after every change; cheap (no logs)."""
        if not self._persist:
            return
        with self._lock:
            jobs = [self._jobs[jid] for jid in self._order]
            pending = list(self._pending)
        # Done jobs need nothing anymore, and neither do failed/cancelled ones whose
        # retry is already queued (the retry job carries everything over).
        states = [j.to_state() for j in jobs
                  if j.status in ("queued", "running", "failed", "cancelled", "interrupted", "stopped")
                  and not j.retried_by]
        for attempt in range(3):
            try:
                queue_store.save(states, pending)
                return
            except OSError:
                # On Windows a virus scanner/indexer can briefly lock the file.
                time.sleep(0.2)

    def _restore(self):
        saved, pending = queue_store.load()
        interrupted = []
        for data in saved:
            try:
                job = Job.from_state(data)
            except TypeError:
                continue
            if job.status == "running":
                # Was downloading when the web UI / PC went down.
                job.status = "interrupted"
                job.finished_at = time.time()
                where = "S{:02}E{:02}".format(*job.current_episode) if job.current_episode else None
                job.append_output(
                    "[webui] This job was still running when the web UI stopped (crash, power loss, "
                    "restart...)" + (f". It was at {where}" if where else "") + ". Use Retry to resume it.\n")
                interrupted.append(job)
            elif job.status in ("failed", "cancelled"):
                job.append_output("[webui] Restored from the saved queue (the log from before isn't kept).\n")
            self._jobs[job.id] = job
            self._order.append(job.id)
        # Saved run order first, then any queued job it somehow doesn't list.
        queued = [jid for jid in self._order if self._jobs[jid].status == "queued"]
        self._pending.extend([jid for jid in pending if jid in queued] +
                             [jid for jid in queued if jid not in pending])
        if not self._order:
            return

        settings = retry.get_settings()
        for job in interrupted:
            if settings["cleanup_temp"] and job.temp_dir and job.temp_before is not None:
                self._clean_temp(job, job.temp_dir, job.temp_before)
            if settings["auto_retry"]:
                # Back at the front, where it was - but nothing starts on its own after
                # a restart: the queue waits for "Start Queue", as always.
                self.retry(job.id, start=False, front=True)
        self._save()

    def add(self, service, title, argv, command, auto_start=False, form=None, front=False, **extra):
        meta = (form or {}).get("meta") or {}
        if isinstance(meta, dict) and meta.get("name"):
            extra.setdefault("display_name", str(meta["name"])[:300])
            if str(meta.get("year") or "").isdigit():
                extra.setdefault("display_year", int(meta["year"]))
            if str(meta.get("poster") or "").startswith("https://"):
                extra.setdefault("poster", meta["poster"])
        job = Job(id=uuid.uuid4().hex[:12], service=service, title=title, argv=argv, command=command,
                  form=form, **extra)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            if front:
                self._pending.appendleft(job.id)
            else:
                self._pending.append(job.id)
        self._save()
        if auto_start:
            self.start()
        elif self._running_enabled:
            self._wake.set()
        return job

    def start(self):
        """(Re)start sequential processing of whatever is currently pending."""
        with self._lock:
            self._running_enabled = True
            self._user_paused = False
        self._wake.set()

    # ---- stop after the current episode ----

    @staticmethod
    def _stop_file(job):
        return os.path.join(tempfile.gettempdir(), f"vt_stop_{job.id}")

    def request_stop(self, job_id):
        """The running job finishes its current episode, then stops (status
        "stopped", resumable with continue_job); the queue pauses with it, so the
        next item doesn't start either."""
        job = self._jobs.get(job_id)
        if not job or job.status != "running" or job.stop_requested:
            return False
        try:
            open(self._stop_file(job), "w").close()
        except OSError:
            return False
        job.stop_requested = True
        self.pause()
        job.append_output("[webui] Stop requested: the current episode is finished, then the download stops "
                          "and the queue pauses. Continue picks it up at the next episode.\n")
        self._save()
        return True

    def cancel_stop(self, job_id):
        job = self._jobs.get(job_id)
        if not job or job.status != "running" or not job.stop_requested:
            return False
        try:
            os.unlink(self._stop_file(job))
        except OSError:
            pass
        job.stop_requested = False
        job.append_output("[webui] Stop cancelled. The download goes on as normal.\n")
        self.start()
        return True

    def continue_job(self, job_id):
        """Pick a stopped show back up at the episode it stopped before - first in
        line - and resume the queue."""
        job = self._jobs.get(job_id)
        if not job or job.status != "stopped":
            return None
        new_job = self.retry(job_id, start=False, front=True)
        if new_job:
            new_job.continuation = True
            self._save()
            self.start()
        return new_job

    def pause(self):
        """Stop after the current download: it finishes normally, but the next queued
        job only starts once Start Queue (or Download Now) is pressed again."""
        with self._lock:
            self._running_enabled = False
            self._user_paused = True

    def add_finished_hook(self, fn):
        """fn(job) is called (in the worker thread) when a job ran and ended."""
        self._finished_hooks.append(fn)

    def jobs_snapshot(self):
        with self._lock:
            return [self._jobs[jid] for jid in self._order]

    def save(self):
        self._save()

    def state(self):
        """"running" (working through the queue), "pausing" (finishing the current job,
        then stopping), "paused" (stopped with jobs still queued) or "idle"."""
        with self._lock:
            busy = any(self._jobs[jid].status == "running" for jid in self._order)
            pending = len(self._pending)
            enabled = self._running_enabled
        if enabled and (busy or pending):
            return {"state": "running", "pending": pending}
        if busy:
            return {"state": "pausing", "pending": pending}
        # Paused on purpose stays "paused" even with nothing queued - so e.g. a pinned
        # show's scheduled check doesn't start the queue again behind the user's back.
        return {"state": "paused" if pending or self._user_paused else "idle", "pending": pending}

    def list_jobs(self):
        with self._lock:
            position = {jid: i + 1 for i, jid in enumerate(self._pending)}
            out = []
            for jid in self._order:
                d = self._jobs[jid].to_dict()
                d["position"] = position.get(jid)  # place in the run order, for queued jobs
                out.append(d)
            return out

    def is_busy(self):
        with self._lock:
            return any(self._jobs[jid].status == "running" for jid in self._order)

    def get(self, job_id):
        return self._jobs.get(job_id)

    def cancel(self, job_id):
        job = self._jobs.get(job_id)
        if not job:
            return False
        with job._lock:
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = time.time()
                subs = list(job._subscribers)
                job._subscribers.clear()
            else:
                subs = None
                job._cancel_requested = True
            proc = job.process
        if subs is not None:
            for q in subs:
                q.put(None)
            with self._lock:
                if job_id in self._pending:
                    self._pending.remove(job_id)
            self._save()
            return True
        if proc and proc.poll() is None:
            _kill_tree(proc)
            return True
        return False

    def retry(self, job_id, auto=False, start=None, front=False):
        """
        Queue a new job re-running a failed/cancelled/interrupted one. For a show that
        died partway through, the new job's -w starts at the episode it died on (see
        retry.resume_wanted) instead of redownloading everything before it.
        start: start the queue right away (default: for manual retries).
        Returns the new Job, or None if this job can't be retried (anymore).
        """
        job = self._jobs.get(job_id)
        if not job:
            return None
        with job._lock:
            if job.status not in ("failed", "cancelled", "interrupted", "stopped") or job.retried_by:
                return None
            # An interrupted job was the one running when the web UI went down: its
            # retry takes that place back instead of waiting behind the whole queue.
            front = front or job.status in ("interrupted", "stopped")

        failed = job.current_episode
        if auto:
            # Only count auto retries that keep dying at the same episode; one that got
            # further made progress, so it starts a fresh count.
            # (failed None = it died before even reaching an episode: no progress either.)
            no_progress = job.retry_of is not None and (failed is None or failed == job.resume_point)
            streak = job.auto_streak + 1 if no_progress else 1
            limit = retry.get_settings()["auto_retry_max"]
            if streak > limit:
                job.append_output(f"[webui] Auto retry: gave up after {limit} attempt(s) failing at the same point.\n")
                return None
        else:
            streak = 0

        form = dict(job.form) if job.form else None
        argv = job.argv
        # The form (and so -w) is inherited from this job, so a retry of a retry keeps
        # its resume point even if it died before reaching any episode this time.
        resume_point = failed if failed is not None else job.resume_point
        if form is not None and failed is not None:
            new_wanted = retry.resume_wanted(form.get("wanted"), failed, job.last_season)
            if new_wanted != (form.get("wanted") or None):
                form["wanted"] = new_wanted
                # Imported here: importing vinetrimmer's services before its config
                # (as a module-level import here can) hits a circular import.
                from . import services_info
                info = services_info.get_service_options(job.service)
                argv = command_builder.build_argv(form, job.service, info["options"] if info else [])

        with job._lock:
            if job.retried_by:  # lost a race with another retry request
                return None
            new_job = self.add(
                service=job.service, title=job.title, argv=argv,
                command=command_builder.format_command(argv), form=form,
                attempt=job.attempt + 1, retry_of=job.id,
                resume_point=resume_point, auto_streak=streak,
                last_season=job.last_season, front=front,
                display_name=job.display_name, display_year=job.display_year, poster=job.poster,
                pin_id=job.pin_id,
            )
            job.retried_by = new_job.id
        self._save()  # again: the original now has retried_by, so it's dropped from the file

        where = "S{:02}E{:02}".format(*failed) if failed else None
        job.append_output(
            f"[webui] {'Auto retry' if auto else 'Retry'} queued (job {new_job.id})"
            + (f", resuming at {where} with -w {form['wanted']}" if where and form else ", running the same command again")
            + ".\n"
        )
        if start is None:
            start = not auto
        # A paused queue stays paused: the retry just waits in line like everything else.
        if start and self.state()["state"] not in ("paused", "pausing"):
            self.start()
        return new_job

    def clear_finished(self):
        with self._lock:
            keep = [jid for jid in self._order if self._jobs[jid].status in ("queued", "running")]
            for jid in self._order:
                if jid not in keep:
                    del self._jobs[jid]
            self._order = keep
        self._save()

    def _run_worker(self):
        while True:
            self._wake.wait()
            with self._lock:
                self._wake.clear()
                if not self._running_enabled:
                    continue  # paused: leave the rest queued until Start Queue / Download Now
                job_id = self._pending.popleft() if self._pending else None
                if job_id is None:
                    self._running_enabled = False
                    continue

            job = self._jobs.get(job_id)
            if job and job.status == "queued":
                self._run_job(job)

            with self._lock:
                if self._pending and self._running_enabled:
                    self._wake.set()

    def _run_job(self, job):
        job.status = "running"
        job.started_at = time.time()
        cmd = [sys.executable, "-m", "vinetrimmer.vinetrimmer", *job.argv]
        # Rich (vinetrimmer's console output) falls back to an 80-column width when
        # stdout isn't a real terminal, which wraps most log lines awkwardly in the
        # web console. COLUMNS is Rich's (and shutil.get_terminal_size's) own env-var
        # override, so this widens output without touching vinetrimmer's code.
        #
        # PYTHONIOENCODING is the fix for a real crash: on Windows, a piped (non-tty)
        # stdout makes Python fall back to the system ANSI code page (cp1252 here)
        # instead of UTF-8, so any Unicode in vinetrimmer's own output - e.g. the
        # "├─" box-drawing characters in track listings - raises UnicodeEncodeError
        # deep in the logging module, which itself then fails to log that error
        # (a "Logging error" cascade) and floods the console with tracebacks instead
        # of the actual output. Confirmed by direct repro: a piped child process
        # printing "├─" exits nonzero without this env var, exits 0 with it. Running
        # the CLI directly in a real terminal never hits this, since Windows' console
        # API handles Unicode regardless of code page - it's specific to piping.
        env = vinetrimmer_env()
        # dl.py's --stop-file (via its env var): "Stop after this episode" creates it.
        env["VT_STOP_FILE"] = self._stop_file(job)
        try:
            os.unlink(env["VT_STOP_FILE"])
        except OSError:
            pass

        # What's in the temp folder before this run, so its leftovers can be told
        # apart from everything else there if it fails (see cleanup.py).
        from . import config_io  # lazy: see the services_info import in retry()
        temp_dir = config_io.get_storage_paths()["temp_effective"]
        temp_before = cleanup.snapshot(temp_dir)
        job.temp_dir, job.temp_before = temp_dir, temp_before
        self._save()

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
            )
        except OSError as e:
            job.append_output(f"[webui] Failed to start: {e}\n")
            job.status = "failed"
            job.finished_at = time.time()
            self._save()
            job.close_subscribers()
            return

        with job._lock:
            job.process = proc

        self._pump_output(job, proc)
        proc.wait()
        try:
            proc.stdin.close()
        except OSError:
            pass

        job.returncode = proc.returncode
        with job._lock:
            was_cancelled = job._cancel_requested
        if was_cancelled:
            job.status = "cancelled"
        elif proc.returncode == STOP_EXIT_CODE and job.stop_requested:
            job.status = "stopped"  # finished an episode and stopped before the next, as asked
        elif proc.returncode == 0:
            job.status = "done"
        else:
            job.status = "failed"
        job.finished_at = time.time()
        try:
            os.unlink(env["VT_STOP_FILE"])
        except OSError:
            pass
        settings = retry.get_settings()
        # Cleanup runs here in the worker thread, so it's finished before the retry
        # or the next queued job starts.
        if job.status in ("failed", "cancelled") and settings["cleanup_temp"]:
            self._clean_temp(job, temp_dir, temp_before)
        if job.status == "failed" and settings["auto_retry"]:
            try:
                self.retry(job.id, auto=True)
            except Exception as e:  # never let a retry problem kill the worker thread
                job.append_output(f"[webui] Auto retry failed: {e}\n")
        for hook in self._finished_hooks:
            try:
                hook(job)
            except Exception as e:  # a hook's problem never stops the queue
                job.append_output(f"[webui] {getattr(hook, '__name__', 'hook')} failed: {e}\n")
        self._save()  # a done job drops out of the saved queue here
        job.close_subscribers()


    @staticmethod
    def _clean_temp(job, temp_dir, before):
        try:
            removed, failed = cleanup.clean_job_leftovers(temp_dir, before, job.started_at)
        except Exception as e:  # never let cleanup kill the worker thread
            job.append_output(f"[webui] Temp cleanup failed: {e}\n")
            return
        if removed:
            job.append_output(f"[webui] Cleaned up {len(removed)} leftover file(s)/folder(s) from {temp_dir}.\n")
        if failed:
            job.append_output(f"[webui] Could not delete (still in use?): {', '.join(failed)}\n")

    def _track_position(self, job, line):
        """Keep job.current_episode/last_season up to date, saving each change - so
        after a crash the saved queue still knows where to resume."""
        self._track_name(job, line)
        found = retry.scan_line(line)
        if found is None:
            return
        name, value = found
        if getattr(job, name) != value:
            setattr(job, name, value)
            self._save()

    _TITLE_LINE = re.compile(r"Titles : Title: (.+?)\s*$")
    _MOVIE_LINE = re.compile(r"Getting tracks for (.+) \((\d{4})\) \[[^\]]*\]\s*$")

    def _track_name(self, job, line):
        """The show/movie name (and a movie's year) as vinetrimmer reports it, for
        jobs added without an availability check - then a poster, if enabled."""
        if "Titles : Title: " in line and not job.display_name:
            m = self._TITLE_LINE.search(retry.strip_ansi(line))
            if m:
                job.display_name = m.group(1)
                self._save()
        elif "Getting tracks for " in line and job.display_name and not job.poster_checked:
            job.poster_checked = True
            clean = retry.strip_ansi(line)
            movie = self._MOVIE_LINE.search(clean)
            kind = "MOVIE" if movie else "TV"
            if movie and not job.display_year:
                job.display_year = int(movie.group(2))
            if job.poster or not retry.get_settings()["fetch_posters"]:
                self._save()
                return

            def fetch():
                poster = posters.lookup(job.display_name, job.display_year, kind)
                if poster:
                    job.poster = poster
                    self._save()
            threading.Thread(target=fetch, daemon=True).start()

    def _handle_line(self, job, line):
        """A complete (newline-terminated) line."""
        self._track_position(job, line)
        if "\r" in line:
            # Redrawn in place: like a terminal, only the last frame stays visible.
            frames = [f for f in line.split("\r") if f.strip()]
            if job._live or len(frames) > 1:
                job.set_live_line(frames[-1] if frames else "", final=True)
                return
            line = frames[-1] if frames else ""
        if line.startswith(PROGRESS_MARKER):
            job.set_progress(line[len(PROGRESS_MARKER):].strip())
        elif job._live:
            job.set_live_line(line, final=True)
        else:
            job.append_output(line + "\n")

    @staticmethod
    def _handle_partial(job, buffer):
        """
        An unfinished line. Returns what to keep buffered.
        - Contains \r: an in-place redraw ("\rProgress: 21%\rProgress: 22%") - show
          the newest frame now, replacing the previous one.
        - Otherwise it's only flushed after output pauses (see _pump_output), which
          is how input() prompts without a newline become visible.
        """
        if "\r" not in buffer:
            return buffer
        frames = [f for f in buffer.split("\r") if f.strip()]
        if frames and not frames[-1].startswith(PROGRESS_MARKER):
            job.set_live_line(frames[-1])
        # Keep just the newest frame; the final text is decided when the newline comes.
        return "\r" + frames[-1] if frames else "\r"

    def _pump_output(self, job, proc):
        """
        Forward the process's output line by line - plus any unfinished line that's
        been sitting there for a moment. That second part is what makes interactive
        prompts visible: input("Enter a Web Refresh Token: ") prints no newline and
        then blocks waiting for an answer, so a plain line-by-line reader (what this
        used to be) would never show the question at all.
        """
        chunks = pyqueue.Queue()

        def reader():
            fd = proc.stdout.fileno()
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                chunks.put(data or None)
                if not data:
                    return

        threading.Thread(target=reader, daemon=True).start()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        while True:
            try:
                data = chunks.get(timeout=0.4)
            except pyqueue.Empty:
                # Output paused mid-line: most likely a prompt waiting for input.
                # (A progress snapshot still being written is never flushed early,
                # and in-place redraws are already shown by _handle_partial.)
                if buffer and "\r" not in buffer and \
                        not PROGRESS_MARKER.startswith(buffer[:len(PROGRESS_MARKER)]):
                    self._handle_line(job, buffer)
                    buffer = ""
                continue
            if data is None:
                buffer += decoder.decode(b"", final=True)
                if buffer.strip("\r"):
                    self._handle_line(job, buffer)
                return
            buffer += decoder.decode(data)
            # Windows pipes deliver CRLF. Hold back a trailing CR in case its LF is in
            # the next chunk; any other lone CR is an in-place redraw of the line.
            hold = buffer.endswith("\r")
            if hold:
                buffer = buffer[:-1]
            buffer = buffer.replace("\r\n", "\n")
            *lines, buffer = buffer.split("\n")
            for line in lines:
                self._handle_line(job, line)
            buffer = self._handle_partial(job, buffer)
            if hold:
                buffer += "\r"


job_queue = JobQueue(persist=True)
