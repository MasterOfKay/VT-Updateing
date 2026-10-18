"""
Builds the exact argv vinetrimmer's own CLI expects, copied 1:1 from the `dl` click
group's options in vinetrimmer/commands/dl.py (see GLOBAL_OPTIONS below) - this list
is the single source of truth the Download page's form, the "Advanced options" panel,
and the command preview are all rendered from.
"""
import shlex

GLOBAL_OPTIONS = [
    {"key": "profile", "flag": "-p", "kind": "text", "section": "advanced",
     "help": "Profile to use when multiple profiles are defined for a service.", "placeholder": "default"},
    {"key": "quality", "flag": "-q", "kind": "text", "section": "primary",
     "help": "Download Resolution, defaults to best available.", "placeholder": "e.g. 2160, 1080, SD, HD720"},
    {"key": "vcodec", "flag": "-v", "kind": "choice", "section": "primary",
     "help": "Video Codec, defaults to H264.", "choices": ["h264", "h265", "vp9", "av1"], "default": "h264"},
    {"key": "acodec", "flag": "-a", "kind": "choice", "section": "advanced",
     "help": "Audio Codec", "choices": ["aac", "ac3", "eac3", "vorbis", "opus"]},
    {"key": "vbitrate", "flag": "-vb", "kind": "int", "section": "advanced",
     "help": "Video Bitrate, defaults to Max."},
    {"key": "abitrate", "flag": "-ab", "kind": "int", "section": "advanced",
     "help": "Audio Bitrate, defaults to Max."},
    {"key": "atmos", "flag": "-aa", "kind": "flag", "section": "advanced",
     "help": "Prefer Atmos Audio"},
    {"key": "channels", "flag": "-ch", "kind": "choice", "section": "advanced",
     "help": "Audio Channels", "choices": ["2.0", "5.1", "7.1", "atmos"]},
    {"key": "range_", "flag": "-r", "kind": "choice", "section": "primary",
     "help": "Video Color Range, defaults to SDR.",
     "choices": ["sdr", "hdr10", "hlg", "dv", "dvhdr", "hybrid", "hybridplus"], "default": "sdr"},
    {"key": "wanted", "flag": "-w", "kind": "text", "section": "primary",
     "help": "Wanted episodes, e.g. `S01-S05,S07`, `S01E01-S02E03`, `S02-S02E03`, defaults to all.",
     "placeholder": "e.g. S01, S01E03-S08E89"},
    {"key": "alang", "flag": "-al", "kind": "text", "section": "primary",
     "help": "Language wanted for audio.", "placeholder": "orig"},
    {"key": "slang", "flag": "-sl", "kind": "text", "section": "primary",
     "help": "Language wanted for subtitles.", "placeholder": "all"},
    {"key": "delay", "flag": "--delay", "kind": "int", "section": "advanced",
     "help": "Delay between title processing"},
    {"key": "proxy", "flag": "--proxy", "kind": "text", "section": "advanced",
     "help": "Proxy URI to use. If a 2-letter country is provided, it will try get a proxy from the config.",
     "placeholder": "e.g. us, us1234, https://host:port"},
    {"key": "audio_only", "flag": "-A", "kind": "flag", "section": "advanced",
     "help": "Only download audio tracks."},
    {"key": "subs_only", "flag": "-S", "kind": "flag", "section": "advanced",
     "help": "Only download subtitle tracks."},
    {"key": "chapters_only", "flag": "-C", "kind": "flag", "section": "advanced",
     "help": "Only download chapters."},
    {"key": "no_subs", "flag": "-ns", "kind": "flag", "section": "advanced",
     "help": "Do not download subtitle tracks."},
    {"key": "no_audio", "flag": "-na", "kind": "flag", "section": "advanced",
     "help": "Do not download audio tracks."},
    {"key": "no_video", "flag": "-nv", "kind": "flag", "section": "advanced",
     "help": "Do not download video tracks."},
    {"key": "no_chapters", "flag": "-nc", "kind": "flag", "section": "advanced",
     "help": "Do not download chapters tracks."},
    {"key": "audio_description", "flag": "-ad", "kind": "flag", "section": "advanced",
     "help": "Download audio description tracks."},
    {"key": "list_", "flag": "--list", "kind": "flag", "section": "advanced",
     "help": "Skip downloading and list available tracks and what tracks would have been downloaded."},
    {"key": "selected", "flag": "--selected", "kind": "flag", "section": "advanced",
     "help": "List selected tracks and what tracks are downloaded."},
    {"key": "cdm", "flag": "--cdm", "kind": "text", "section": "advanced",
     "help": "Override the CDM that will be used for decryption.", "placeholder": "override CDM name"},
    {"key": "keys", "flag": "--keys", "kind": "flag", "section": "primary",
     "help": "Skip downloading, retrieve the decryption keys (via CDM or Key Vaults) and print them."},
    {"key": "cache", "flag": "--cache", "kind": "flag", "section": "advanced",
     "help": "Disable the use of the CDM and only retrieve decryption keys from Key Vaults."},
    {"key": "no_cache", "flag": "--no-cache", "kind": "flag", "section": "advanced",
     "help": "Disable the use of Key Vaults and only retrieve decryption keys from the CDM."},
    {"key": "no_proxy", "flag": "--no-proxy", "kind": "flag", "section": "advanced",
     "help": "Force disable all proxy use."},
    {"key": "force_proxy", "flag": "--force-proxy", "kind": "flag", "section": "advanced",
     "help": "Force using proxy even if current region matches."},
    {"key": "no_mux", "flag": "-nm", "kind": "flag", "section": "advanced",
     "help": "Do not mux the downloaded and decrypted tracks."},
    {"key": "mux", "flag": "--mux", "kind": "flag", "section": "advanced",
     "help": "Force muxing when using --audio-only/--subs-only/--chapters-only."},
    {"key": "worst", "flag": "--worst", "kind": "flag", "section": "advanced",
     "help": "Choose the worst available video tracks rather than the best"},
    {"key": "no_forced", "flag": "-nf", "kind": "flag", "section": "advanced",
     "help": "Do not download forced subtitle tracks."},
    {"key": "audio_pick", "flag": "--audio-pick", "kind": "text", "section": "advanced",
     "help": "Audio track per language: LANG=CODEC/CHANNELS/KBPS, comma separated, any part optional "
             "(filled in by the availability check).",
     "placeholder": "e.g. en=AAC/2.0/128,de=E-AC3/5.1"},
    {"key": "first_title", "flag": "--first-title", "kind": "flag", "section": "advanced",
     "help": "Only process the first (wanted) title."},
    {"key": "tag", "flag": "--tag", "kind": "text", "section": "advanced",
     "help": "Add group tag to filenames and file metadata (e.g., 'MasterOfKay').",
     "placeholder": "e.g. MasterOfKay"},
]


class CommandError(ValueError):
    pass


def build_argv(form, service_name, service_options):
    """
    form: dict of submitted values keyed by GLOBAL_OPTIONS[*]["key"], plus "service",
          "title", "debug", and "svc__<option name>" for the selected service's own
          special options (as returned by services_info.get_service_options).
    Returns the argv list to run as:
        [sys.executable, "-m", "vinetrimmer.vinetrimmer", *build_argv(...)]
    """
    if not service_name:
        raise CommandError("A service must be selected.")

    argv = ["dl"]
    if form.get("debug"):
        argv.append("--debug")

    for opt in GLOBAL_OPTIONS:
        raw = form.get(opt["key"])
        if opt["kind"] == "flag":
            if raw:
                argv.append(opt["flag"])
            continue
        if raw is None or raw == "":
            continue
        argv.extend([opt["flag"], str(raw)])

    argv.append(service_name)

    for opt in service_options or []:
        raw = form.get(f"svc__{opt['name']}")
        flag = max(opt["opts"], key=len)  # prefer the long form for a readable command preview
        if opt["kind"] == "flag":
            if raw:
                argv.append(flag)
            continue
        if raw is None or raw == "":
            continue
        argv.extend([flag, str(raw)])

    title = (form.get("title") or "").strip()
    if title:
        argv.append(title)

    return argv


def format_command(argv):
    return "python -m vinetrimmer.vinetrimmer " + " ".join(shlex.quote(a) for a in argv)
