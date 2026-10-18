"""
Retry failed jobs: working out where a failed run stopped, and rewriting its -w value to resume from there. 
The web UI's "Retry" button does this automatically, and the CLI's --retry option does it for a single job.
The web UI and the CLI share the same temp folder, so the web UI can retry CLI jobs that were killed or crashed, 
and the CLI can retry web UI jobs that were killed or crashed.
The retry logic is in webui/retry.py, and the web UI's "Retry" button and the CLI's --retry option are in webui/queue.py. 
The retry logic is also used by the web UI's "Retry all failed" button, which retries all failed jobs in the queue.
"""
import json
import re
import threading
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent / "webui_settings.json"
DEFAULT_SETTINGS = {
    "auto_retry": False, "auto_retry_max": 3, "cleanup_temp": True,
    # Console tab: "bars" (download progress only), "cli" (log only) or "both".
    "console_view": "bars",
    # Experimental availability check on the Download page, and posters for it/the queue.
    "experimental_check": False, "fetch_posters": False,
    # Update tab: also offer pre-release (experimental) builds.
    "update_prereleases": False,
    # Pinned shows: hours between checks, and TVmaze air dates (sends the show name).
    "pin_check_hours": 24, "fetch_air_dates": False,
}
_BOOL_SETTINGS = ("auto_retry", "cleanup_temp", "experimental_check", "fetch_posters",
                  "update_prereleases", "fetch_air_dates")
_CONSOLE_VIEWS = ("bars", "cli", "both")
_settings_lock = threading.Lock()

# Same bounds as vinetrimmer/utils/click.py's wanted_param.
MIN_EPISODE = 0
MAX_EPISODE = 9999

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# vinetrimmer/commands/dl.py logs this once per title, right before working on it:
#   "Getting tracks for {title} S{season:02}E{episode:02}{ - name} [{id}]"
# (movies get the same prefix without the SxxEyy part).
_GETTING_TRACKS = "Getting tracks for "
_EPISODE = re.compile(r" S(\d+)E(\d+)\b")
# Titles.print(): "By Season: 1 (10), 2 (10), 8 (12)"
_BY_SEASON = re.compile(r"By Season:\s*(.*)")
_TOKEN_SIDE = re.compile(r"^S(\d+)(?:E(\d+))?$", re.IGNORECASE)


def _clean(data, out):
    for key in _BOOL_SETTINGS:
        if key in data:
            out[key] = bool(data[key])
    if data.get("console_view") in _CONSOLE_VIEWS:
        out["console_view"] = data["console_view"]
    if "pin_check_hours" in data:
        try:
            out["pin_check_hours"] = max(1, min(24 * 14, int(data["pin_check_hours"])))
        except (TypeError, ValueError):
            pass
    if "auto_retry_max" in data:
        try:
            out["auto_retry_max"] = max(1, min(20, int(data["auto_retry_max"])))
        except (TypeError, ValueError):
            pass
    return out


def get_settings():
    with _settings_lock:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    return _clean(data if isinstance(data, dict) else {}, dict(DEFAULT_SETTINGS))


def set_settings(values):
    current = _clean(values or {}, get_settings())
    with _settings_lock:
        SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def strip_ansi(line):
    return _ANSI.sub("", line)


def find_failed_episode(output_lines):
    """
    (season, episode) of the title the run was working on when it died - the last
    "Getting tracks for ..." line in its output - or None if it never got that far
    (login/manifest errors, ...) or that title is a movie.
    """
    for line in reversed(output_lines):
        line = _ANSI.sub("", line)
        idx = line.find(_GETTING_TRACKS)
        if idx < 0:
            continue
        m = _EPISODE.search(line, idx + len(_GETTING_TRACKS) - 1)
        return (int(m.group(1)), int(m.group(2))) if m else None
    return None


def find_last_season(output_lines):
    """Highest season number from Titles.print()'s "By Season:" line, if it was logged."""
    for line in output_lines:
        m = _BY_SEASON.search(_ANSI.sub("", line))
        if m:
            seasons = [int(s) for s in re.findall(r"(?:^|,\s*)(\d+)\s*\(", m.group(1))]
            if seasons:
                return max(seasons)
    return None


def scan_line(line):
    """
    For tracking a running job's position line by line (so it can be saved, and
    survives a crash of the web UI itself):
    ("current_episode", (season, episode) or None for a movie) for a "Getting tracks for" line,
    ("last_season", n) for the "By Season:" line, otherwise None.
    """
    if _GETTING_TRACKS in line:
        return "current_episode", find_failed_episode([line])
    if "Stop requested - stopped before " in line:
        m = _EPISODE.search(strip_ansi(line))
        if m:
            return "current_episode", (int(m.group(1)), int(m.group(2)))
    if "By Season:" in line:
        last = find_last_season([line])
        if last is not None:
            return "last_season", last
    return None


def wanted_start(wanted):
    """The first episode a -w value asks for, e.g. "S02E05-S03" -> (2, 5), "S04" -> (4, 1);
    None if it doesn't name one (empty, exclusions only, invalid)."""
    for token in re.split(r"\s*[,;]\s*", (wanted or "").strip()):
        if not token or token.startswith("-"):
            continue
        parsed = _parse_token(token)
        if parsed:
            s, e = parsed[0]
            return s, e or 1
    return None


def _parse_token(token):
    """'S02E03-S05' -> ((2, 3), (5, 9999), 'S05'), or None if it isn't valid syntax."""
    sides = re.split(r"[:-]", token)
    if len(sides) > 2:
        return None
    if len(sides) == 1:
        sides.append(sides[0])
    parsed = [_TOKEN_SIDE.match(s.strip()) for s in sides]
    if any(p is None for p in parsed):
        return None
    left, right = parsed
    start = (int(left.group(1)), int(left.group(2)) if left.group(2) is not None else MIN_EPISODE)
    end = (int(right.group(1)), int(right.group(2)) if right.group(2) is not None else MAX_EPISODE)
    return start, end, sides[1].strip()


def resume_wanted(wanted, failed, last_season=None):
    """
    The -w value for a retry that resumes at `failed` (season, episode).

    - No -w: "S08E04-S<last season>" (everything from the failed episode on).
    - -w given: ranges already finished entirely before the failed episode are
      dropped, and the range containing it gets its start (min) moved up to it;
      later ranges and exclusions ("-S03") are kept as they were.
      So "S01-S10" -> "S08E04-S10", and a single "S01E03" that failed stays "S01E03".

    Returns the original value unchanged if `failed` is None or it can't be applied.
    """
    if not failed:
        return wanted
    s, e = failed
    marker = f"S{s:02}E{e:02}"

    if not (wanted or "").strip():
        # Upper bound: the show's last season as vinetrimmer printed it. Not a huge
        # placeholder like S999 - vinetrimmer expands every range into a list of
        # 10,000 episodes per season, so S08-S999 would build ~10 million entries.
        end = max(s, last_season if last_season is not None else s + 50)
        return f"{marker}-S{end:02}"

    tokens = [t for t in re.split(r"\s*[,;]\s*", wanted.strip()) if t]
    out = []
    kept_any = False
    for token in tokens:
        if token.startswith("-"):
            out.append(token)
            continue
        parsed = _parse_token(token)
        if parsed is None:
            return wanted  # let vinetrimmer report its own syntax error, as before
        start, end, right_text = parsed
        if end < (s, e):
            continue  # finished before the failure
        kept_any = True
        if start < (s, e):
            out.append(f"{marker}-{right_text}")
        else:
            out.append(token)
    if not kept_any:
        return wanted
    return ",".join(out)
