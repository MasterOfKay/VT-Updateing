"""
What the queue is downloading right now, for the external API's /progress calls.

Built from what the web UI already knows about each job (see queue_manager.Job). Command
lines and logs are left out on purpose: they can hold things like a proxy login.
"""
import json
import time

FINISHED = ("done", "failed", "cancelled", "interrupted", "stopped")
RECENT = 5


def _percent(track):
    total = track.get("total") or 100
    return max(0.0, min(100.0, (track.get("completed") or 0) * 100.0 / total))


def _track_state(track):
    # Same rules as the web console (trackState in app.js).
    status = str(track.get("status") or "")
    if "fail" in status.lower():
        return "failed"
    if status == "Downloaded":
        return "done"
    return "queued" if track.get("elapsed") is None else "downloading"


def _duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02}m"
    return f"{minutes}m {seconds:02}s" if minutes else f"{seconds}s"


def _tracks(job):
    try:
        raw = json.loads(job.progress or "{}").get("tracks") or []
    except (ValueError, AttributeError):
        raw = []
    return [{
        "type": t.get("type"), "name": t.get("name"), "percent": round(_percent(t), 1),
        "state": _track_state(t), "eta_seconds": t.get("eta"),
    } for t in raw if isinstance(t, dict)]


def _basics(job):
    return {
        "job_id": job.id, "service": job.service, "title": job.display_name or job.title,
        "year": job.display_year, "poster": job.poster, "status": job.status,
    }


def _active(job, now):
    tracks = _tracks(job)
    # The picture is nearly all of the download, so it stands for the whole thing.
    main = [t for t in tracks if t["type"] == "Video"] or tracks
    percent = round(sum(t["percent"] for t in main) / len(main), 1) if main else None
    etas = [t["eta_seconds"] for t in tracks
            if t["state"] == "downloading" and t["type"] != "Subtitle" and t["eta_seconds"] is not None]
    eta = max(etas) if etas else None
    done = sum(t["state"] == "done" for t in tracks)
    episode = "S{:02}E{:02}".format(*job.current_episode) if job.current_episode else None
    episodes = job.episodes if job.episodes and job.episodes.get("total") else None

    entry = _basics(job)
    parts = [entry["title"] + (f" ({job.display_year})" if job.display_year else ""), f"[{job.service}]"]
    if episode:
        parts.append(episode)
    if episodes and episodes.get("index"):
        parts.append(f"(episode {episodes['index']} of {episodes['total']})")
    if not tracks:
        detail = "preparing"
    elif done == len(tracks):
        detail = "all tracks downloaded, processing"
    else:
        detail = f"{percent:.0f}% · {done}/{len(tracks)} tracks" + (f" · about {_duration(eta)} left" if eta else "")
    entry.update({
        "episode": episode,
        "episodes": {"current": episodes.get("index"), "total": episodes["total"]} if episodes else None,
        "percent": percent, "eta_seconds": eta, "tracks_done": done, "tracks_total": len(tracks),
        "elapsed_seconds": int(now - job.started_at) if job.started_at else None,
        "text": " ".join(parts) + f" - {detail}",
        "tracks": tracks,
    })
    return entry


def snapshot(queue, requests_=None):
    now = time.time()
    jobs = queue.jobs_snapshot()
    request_ids = {r.job_id: r.id for r in requests_.list() if r.job_id} if requests_ else {}
    active = [_active(j, now) for j in jobs if j.status == "running"]
    queued = [_basics(j) for j in jobs if j.status == "queued"]
    finished = sorted((j for j in jobs if j.status in FINISHED and j.finished_at),
                      key=lambda j: j.finished_at, reverse=True)[:RECENT]
    finished = [{**_basics(j), "finished_at": j.finished_at} for j in finished]
    for entry in (*active, *queued, *finished):
        entry["request_id"] = request_ids.get(entry["job_id"])
    state = queue.state()
    if active:
        text = "\n".join(e["text"] for e in active)
    elif queued:
        text = f"Nothing is downloading. {len(queued)} waiting, the queue is {state['state']}."
    else:
        text = "Nothing is downloading."
    return {"time": now, "queue": state["state"], "text": text, "active": active, "queued": queued,
            "finished": finished}
