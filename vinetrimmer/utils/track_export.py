"""
Helpers for `dl`'s --list-json and --audio-pick options.

--list-json FILE   Writes every title's full track list (before any selection) and the
                   selection vinetrimmer made to FILE as JSON - for tools/UIs that need
                   exact codecs, ranges, bitrates and languages instead of log lines.
--audio-pick SPEC  Overrides the audio track picked for specific languages, e.g.
                   "en=AAC/2.0/128,de=E-AC3/5.1" - see parse_audio_pick().
"""
import json
import logging
import os
import re

from vinetrimmer.objects.tracks import CODEC_MAP
from vinetrimmer.utils import is_close_match

log = logging.getLogger("dl")

_IMAGE_URL = re.compile(r"^https?://\S+\.(?:jpe?g|png|webp)(?:[?#]\S*)?$", re.IGNORECASE)
_IMAGE_KEY = re.compile(r"poster|image|art|thumb|tile|cover|background", re.IGNORECASE)


def codec_name(codec):
    codec = codec or ""
    low = codec.lower()
    # Not in CODEC_MAP (which is also used for file naming, so it's left alone).
    if low.startswith(("av01", "av1")):
        return "AV1"
    if low.startswith(("vp09", "vp9")):
        return "VP9"
    return next((CODEC_MAP[x] for x in CODEC_MAP if codec.startswith(x)), codec)


def video_range(track):
    if getattr(track, "dvhdr", False):
        return "DV+HDR"
    if getattr(track, "hdr10", False):
        # AV1's HDR is HDR10+ (see Tracks.select_videos' HYBRIDPLUS handling).
        return "HDR10+" if codec_name(track.codec) == "AV1" else "HDR10"
    if getattr(track, "hlg", False):
        return "HLG"
    if getattr(track, "dv", False):
        return "DV"
    return "SDR"


def _video(t):
    return {
        "id": str(t.id), "codec": codec_name(t.codec), "range": video_range(t),
        "width": t.width, "height": t.height, "bitrate": int(t.bitrate or 0) // 1000,
        "fps": round(float(t.fps), 3) if t.fps else None, "size": t.size or None,
        "language": str(t.language) if t.language else None,
    }


def _audio(t):
    return {
        "id": str(t.id), "codec": codec_name(t.codec), "language": str(t.language),
        "channels": str(t.channels) if t.channels else None, "atmos": bool(t.atmos),
        "bitrate": int(t.bitrate or 0) // 1000, "size": t.size or None,
        "name": t.get_track_name(), "original": bool(t.is_original_lang),
        "descriptive": bool(t.descriptive),
    }


def _subtitle(t):
    return {
        "id": str(t.id), "codec": codec_name(t.codec), "language": str(t.language),
        "name": t.get_track_name(), "forced": bool(t.forced), "sdh": bool(t.sdh),
        "cc": bool(t.cc), "original": bool(t.is_original_lang),
    }


def find_image(data, depth=0):
    """Best effort: the first image URL in a service's title data under a key that
    looks like artwork (poster/image/tile/...). Services store these very differently."""
    if depth > 6:
        return None
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and _IMAGE_KEY.search(str(key)) and _IMAGE_URL.match(value):
                return value
        for value in data.values():
            if isinstance(value, (dict, list)):
                found = find_image(value, depth + 1)
                if found:
                    return found
    elif isinstance(data, list):
        for value in data[:20]:
            found = find_image(value, depth + 1)
            if found:
                return found
    return None


def title_entry(service_name, title, titles):
    seasons = {}
    for t in titles:
        if t.season:
            seasons[t.season] = seasons.get(t.season, 0) + 1
    return {
        "service": service_name,
        "title": {
            "id": str(title.id), "type": title.type.name if hasattr(title.type, "name") else str(title.type),
            "name": title.name, "year": title.year or None,
            "season": title.season or None, "episode": title.episode or None,
            "episode_name": title.episode_name,
            "original_language": str(title.original_lang) if title.original_lang else None,
            "image": find_image(getattr(title, "service_data", None)),
        },
        "titles_total": len(titles),
        "seasons": {str(k): v for k, v in sorted(seasons.items())},
        "tracks": {
            "video": [_video(t) for t in title.tracks.videos],
            "audio": [_audio(t) for t in title.tracks.audios],
            "subtitles": [_subtitle(t) for t in title.tracks.subtitles],
        },
        "selected": None,
    }


def mark_selected(entry, title):
    entry["selected"] = {
        "video": [str(t.id) for t in title.tracks.videos],
        "audio": [str(t.id) for t in title.tracks.audios],
        "subtitles": [str(t.id) for t in title.tracks.subtitles],
    }


def keys_status(title, complete):
    """--keys + --list-json: whether every selected encrypted track got its key.
    With --cache the lookup stops at the first track whose key isn't in a vault, so
    tracks after it show found=False without having been looked up."""
    tracks = []
    for t in title.tracks:
        if t.__class__.__name__ in ("TextTrack", "MenuTrack") or not getattr(t, "encrypted", False):
            continue
        tracks.append({
            "type": "video" if t.__class__.__name__ == "VideoTrack" else "audio",
            "track": str(t).replace("├─ ", ""),
            "kid": getattr(t, "kid", None),
            "found": bool(getattr(t, "key", None)),
        })
    return {"complete": bool(complete) and all(t["found"] for t in tracks), "tracks": tracks}


def write_json(path, entries):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fd:
        json.dump({"version": 1, "titles": entries}, fd, indent=1)
    os.replace(tmp, path)


def parse_audio_pick(value):
    """
    "en=AAC/2.0/128, de=E-AC3/5.1, fr-FR=best" ->
    [("en", "AAC", "2.0", 128), ("de", "E-AC3", "5.1", None), ("fr-FR", None, None, None)]
    Each part after "=" is optional and "best"/"*"/"" means "any": codec (AAC, E-AC3,
    AC3, ...), channels (2.0, 5.1, 16/JOC, or "atmos"), bitrate in kb/s.
    """
    picks = []
    for item in re.split(r"\s*,\s*", (value or "").strip()):
        if not item:
            continue
        lang, _, rest = item.partition("=")
        parts = [p.strip() for p in rest.split("/")] + [None, None, None]
        codec, channels, bitrate = [None if (p or "").lower() in ("", "best", "*", "any") else p for p in parts[:3]]
        if bitrate is not None:
            try:
                bitrate = int(float(bitrate))
            except ValueError:
                raise ValueError(f"Invalid bitrate in --audio-pick: {item!r}")
        picks.append((lang.strip(), codec, channels, bitrate))
    return picks


def _lang_matches(track, lang):
    return str(track.language).lower() == lang.lower()


def apply_audio_pick(selected, all_audios, picks):
    """Returns the selected audio list with each picked language's track replaced by
    the best (highest bitrate) track matching the pick. Languages without a match keep
    the normal selection, with a warning."""
    result = list(selected)
    for lang, codec, channels, bitrate in picks:
        candidates = [a for a in all_audios if _lang_matches(a, lang)] or \
                     [a for a in all_audios if is_close_match(a.language, [lang])]
        if codec:
            candidates = [a for a in candidates if codec_name(a.codec).lower() == codec.lower()
                          or codec.lower() in (a.codec or "").lower()]
        if channels:
            if channels.lower() == "atmos":
                candidates = [a for a in candidates if a.atmos]
            else:
                candidates = [a for a in candidates if str(a.channels) == channels]
        if bitrate is not None:
            candidates = [a for a in candidates if abs(int(a.bitrate or 0) // 1000 - bitrate) <= 2]
        if not candidates:
            log.warning(f" - --audio-pick: no {lang} audio matches {codec or 'any'}/{channels or 'any'}/"
                        f"{bitrate or 'any'} kb/s, keeping the default choice")
            continue
        non_descriptive = [a for a in candidates if not a.descriptive] or candidates
        pick = max(non_descriptive, key=lambda a: float(a.bitrate or 0))
        replaced = False
        for i, a in enumerate(result):
            if not a.descriptive and (_lang_matches(a, lang) or (a.language == pick.language)):
                if not replaced:
                    result[i] = pick
                    replaced = True
                else:
                    result[i] = None  # a second track of that language: drop it
        if not replaced:
            result.append(pick)
        log.info(f" + --audio-pick: {lang} -> {codec_name(pick.codec)} {pick.channels or ''} "
                 f"{int(pick.bitrate or 0) // 1000} kb/s")
    seen, out = set(), []
    for a in result:
        if a is not None and a.id not in seen:
            seen.add(a.id)
            out.append(a)
    return out
