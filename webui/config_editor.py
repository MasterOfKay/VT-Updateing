"""
The Settings page's view of vinetrimmer.yml.

read() returns every section as the command line would see it (the built-in
config/vinetrimmer.yml merged with yours); save() writes one section into yours,
keeping comments and anything it doesn't know. Passwords and keys are write-only:
the page learns whether one is saved, never what it is.
"""
import re
import shutil
import string
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from vinetrimmer.config import directories, filenames

from . import command_builder, config_io, service_checks, services_info

KEEP = "__keep__"  # a value the page can't edit (e.g. a CDM mapped per profile)
_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,60}$")

DOWNLOADERS = ["aria2c", "N_m3u8DL-RE"]
SUBTITLE_FORMATS = ["srt", "vtt", "ass"]
DECRYPTERS = ["packager", "mp4decrypt"]
TEMPLATE_FIELDS = {
    "movies": ["title", "year", "quality", "source", "audio", "video", "tag"],
    "series": ["title", "season_episode", "episode_name", "quality", "source", "audio", "video", "tag"],
}
# aria2c options that only tune the download. Others can run programs or write files
# elsewhere, so they stay a job for the text editor.
ARIA2C_OPTIONS = [
    "always-resume", "async-dns", "async-dns-server", "check-certificate", "connect-timeout", "console-log-level",
    "disable-ipv6", "disk-cache", "enable-http-pipelining", "file-allocation", "http-accept-gzip",
    "lowest-speed-limit", "max-concurrent-downloads", "max-connection-per-server", "max-download-limit",
    "max-file-not-found", "max-overall-download-limit", "max-resume-failure-tries", "max-tries", "min-split-size",
    "min-tls-version", "piece-length", "remote-time", "retry-wait", "split", "stream-piece-selector", "timeout",
    "uri-selector", "user-agent",
]
VAULT_FIELDS = {
    "local": ["path"],
    "remote": ["host", "port", "database", "username", "password"],
    "http": ["host", "key", "username", "password"],
    "httpapi": ["host", "password"],
}
REMOTE_CDM_FIELDS = ["name", "type", "device", "system_id", "security_level", "host", "challenge_path",
                     "license_path", "key"]
REMOTE_CDM_TYPES = ["CHROME", "ANDROID", "PLAYREADY"]
SECRETS = ("password", "key", "ticket")


# ---------------------------------------------------------------- reading

def devices():
    """Every CDM the command line can load, as it would be named in cdm:."""
    out = []
    root = Path(directories.devices)
    entries = sorted(root.iterdir(), key=lambda p: p.name.lower()) if root.is_dir() else []
    for entry in entries:
        if entry.is_dir():
            if (entry / "zgpriv.dat").is_file() and (entry / "bgroupcert.dat").is_file():
                out.append({"name": entry.name, "type": "PlayReady", "source": "folder"})
            elif (entry / "wv.json").is_file() and (entry / "device_client_id_blob").is_file():
                out.append({"name": entry.name, "type": "Widevine", "source": "folder"})
        elif entry.suffix.lower() in (".wvd", ".prd"):
            kind = "Widevine" if entry.suffix.lower() == ".wvd" else "PlayReady"
            out.append({"name": entry.stem, "type": kind, "source": entry.suffix.lower()[1:] + " file"})
    for api in service_checks._cfg().get("cdm_api") or []:
        if api.get("name"):
            out.append({"name": str(api["name"]), "type": "Remote", "source": "remote API"})
    return out


def _per_service(value, fallback=None):
    """A setting that is one value for all, or a default plus exceptions per service."""
    if not isinstance(value, dict):
        return {"default": value if value is not None else fallback, "per": {}}
    per = {str(k): (KEEP if isinstance(v, dict) else v) for k, v in value.items() if k != "default"}
    default = value.get("default", fallback)
    return {"default": KEEP if isinstance(default, dict) else default, "per": per}


def _downloader_name(value):
    if str(value).lower() in ("n_m3u8dl-re", "m3u8dl-re", "m3u8dl"):
        return "N_m3u8DL-RE"
    return value


def _masked(entry, fields):
    out = {}
    for field in fields:
        if field in SECRETS:
            out[field] = ""
            out[f"{field}_saved"] = bool(entry.get(field))
        else:
            value = entry.get(field)
            out[field] = "" if value is None else value
    return out


def _vaults(cfg):
    out = []
    for index, vault in enumerate(cfg.get("key_vaults") or []):
        kind = str(vault.get("type") or "").lower()
        out.append({"from": index, "name": str(vault.get("name") or ""), "type": kind,
                    **_masked(vault, VAULT_FIELDS.get(kind, []))})
    return out


def _remote_cdms(cfg):
    return [{"from": index, **_masked(api, REMOTE_CDM_FIELDS)} for index, api in enumerate(cfg.get("cdm_api") or [])]


def read():
    cfg = service_checks._cfg()
    downloads = cfg.get("downloads") or {}
    downloader = _per_service(cfg.get("downloader"), "aria2c")
    downloader["default"] = _downloader_name(downloader["default"])
    downloader["per"] = {k: _downloader_name(v) for k, v in downloader["per"].items()}
    arguments = cfg.get("arguments") or {}
    output = cfg.get("output_template") or {}
    styles = cfg.get("subtitle_style") or {}
    services = [s["name"] for s in services_info.list_services()]
    aria = cfg.get("aria2c") or {}
    return {
        "sections": {
            "general": {
                "tag": cfg.get("tag") or "",
                "tag_sd": cfg.get("tag_sd") or "",
                "generate_logs": bool(cfg.get("generate_logs", True)),
                "decrypter": cfg.get("decrypter") or "packager",
            },
            "downloader": {
                "downloader": downloader,
                "concurrent_tracks": _per_service(downloads.get("concurrent_tracks"), 1),
                "concurrent_subtitle_downloads": _per_service(downloads.get("concurrent_subtitle_downloads"), 0),
                "progress_display": _per_service(downloads.get("progress_display"), True),
                "crop": _per_service((cfg.get("crop") or {}).get("mode"), "none"),
                "aria2c": {str(k).replace("_", "-"): v for k, v in aria.items()},
            },
            "devices": {"cdm": _per_service(cfg.get("cdm")), "cdm_api": _remote_cdms(cfg)},
            "vaults": {"key_vaults": _vaults(cfg)},
            "output": {
                "movies": output.get("movies") or "",
                "series": output.get("series") or "",
                "use_last_audio": bool(output.get("use_last_audio", False)),
                "subtitle_format": _per_service(cfg.get("subtitle_format"), "srt"),
                "subtitle_style": {
                    "default": dict(styles.get("default") or {}),
                    "per": {str(k): dict(v) for k, v in styles.items() if k != "default" and isinstance(v, dict)},
                },
                "attach_fonts": bool(cfg.get("attach_fonts", True)),
                "download_fonts": bool(cfg.get("download_fonts", True)),
            },
            "defaults": {
                "global": {str(k): v for k, v in arguments.items() if not isinstance(v, dict) and v is not None},
                "services": {str(k): dict(v) for k, v in arguments.items() if isinstance(v, dict)},
            },
            "profiles": {"profiles": _per_service(cfg.get("profiles"), "default")},
            "network": {"headers": dict(cfg.get("headers") or {})},
        },
        "choices": {
            "services": services,
            "devices": devices(),
            "downloaders": DOWNLOADERS,
            "subtitle_formats": SUBTITLE_FORMATS,
            "decrypters": DECRYPTERS,
            "template_fields": TEMPLATE_FIELDS,
            "aria2c_options": ARIA2C_OPTIONS,
            "vault_fields": VAULT_FIELDS,
            "remote_cdm_types": REMOTE_CDM_TYPES,
            "global_options": command_builder.GLOBAL_OPTIONS,
        },
    }


# ---------------------------------------------------------------- writing

def _section(data, name):
    if not isinstance(data.get(name), dict):
        data[name] = CommentedMap()
    return data[name]


def _sync(target, new):
    """Make a mapping hold exactly `new`, touching only what differs (keeps comments)."""
    for key in [k for k in target if k not in new]:
        del target[key]
    for key, value in new.items():
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = CommentedMap()
            _sync(target[key], value)
        elif key not in target or target[key] != value or type(target[key]) is not type(value):
            target[key] = value


def _name(value, what="name"):
    value = str(value or "").strip()
    if not _NAME.match(value):
        raise ValueError(f"'{value}' isn't a usable {what}: use letters, numbers, dots, dashes or underscores.")
    return value


def _build_per_service(sent, old, convert):
    """The mapping to write for a per-service setting."""
    if not isinstance(sent, dict):
        raise ValueError("Unexpected data.")
    old = old if isinstance(old, dict) else {}
    new = {}
    entries = [("default", sent.get("default"))] + list((sent.get("per") or {}).items())
    for key, value in entries:
        key = "default" if key == "default" else _name(key, "service name")
        if value == KEEP:
            if key in old:
                new[key] = old[key]
            continue
        if value is None or value == "":
            continue
        new[key] = convert(value)
    return new


def _int(low, high, what):
    def convert(value):
        try:
            number = int(str(value).strip())
        except ValueError:
            number = None
        if number is None or not low <= number <= high:
            raise ValueError(f"{what}: use a number from {low} to {high}.")
        return number
    return convert


def _choice(allowed, what):
    def convert(value):
        if value not in allowed:
            raise ValueError(f"{what}: choose one of {', '.join(allowed)}.")
        return value
    return convert


def _text(value):
    value = str(value).strip()
    if len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError("That value is too long or holds line breaks.")
    return value


def _crop(value):
    value = str(value).strip().lower()
    if value not in ("none", "auto") and not re.fullmatch(r"\d{1,4}(,\d{1,4}){3}", value):
        raise ValueError("Crop: use none, auto or four pixel counts such as 0,140,0,140 (left,top,right,bottom).")
    return value


def _save_general(data, sent):
    data["decrypter"] = _choice(DECRYPTERS, "Decrypter")(sent.get("decrypter"))
    data["generate_logs"] = bool(sent.get("generate_logs"))
    for key in ("tag", "tag_sd"):
        value = _text(sent.get(key) or "")
        if value and not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value):
            raise ValueError("The group tag may hold letters, numbers, dots, dashes and underscores.")
        data[key] = value


def _save_downloader(data, sent):
    merged = service_checks._cfg()
    downloads_old = merged.get("downloads") or {}
    parts = (
        ("downloader", data, "downloader", merged.get("downloader"), _choice(DOWNLOADERS, "Downloader")),
        ("concurrent_tracks", "downloads", "concurrent_tracks", downloads_old.get("concurrent_tracks"),
         _int(1, 32, "Tracks at the same time")),
        ("concurrent_subtitle_downloads", "downloads", "concurrent_subtitle_downloads",
         downloads_old.get("concurrent_subtitle_downloads"), _int(0, 64, "Subtitles at the same time")),
        ("progress_display", "downloads", "progress_display", downloads_old.get("progress_display"), bool),
        ("crop", "crop", "mode", (merged.get("crop") or {}).get("mode"), _crop),
    )
    for field, parent, key, old, convert in parts:
        if field not in sent:
            continue
        if field == "progress_display":
            value = sent[field]
            new = {"default": bool(value.get("default")),
                   **{_name(k, "service name"): bool(v) for k, v in (value.get("per") or {}).items()}}
        else:
            new = _build_per_service(sent[field], old, convert)
        target = parent if isinstance(parent, dict) else _section(data, parent)
        _sync(_section(target, key), new)
    if "aria2c" in sent:
        target = _section(data, "aria2c")
        keep = {k: v for k, v in target.items() if str(k).replace("_", "-") not in ARIA2C_OPTIONS}
        new = dict(keep)
        for option, value in (sent["aria2c"] or {}).items():
            option = str(option).strip().lstrip("-").replace("_", "-")
            underscored = option.replace("-", "_")
            if option not in ARIA2C_OPTIONS:
                if underscored in keep or option in keep:
                    continue  # set by hand: stays as it is
                raise ValueError(f"aria2c option '{option}' can only be set in vinetrimmer.yml itself.")
            value = _text(value)
            if value:
                new[underscored] = value
        _sync(target, new)


def _rebuild_list(data, key, sent, build):
    old = service_checks._cfg().get(key) or []
    entries = CommentedSeq()
    names = set()
    for row in sent[:50]:
        index = row.get("from")
        base = dict(old[index]) if isinstance(index, int) and 0 <= index < len(old) else {}
        entry = build(row, base)
        if entry["name"] in names:
            raise ValueError(f"The name '{entry['name']}' is used twice.")
        names.add(entry["name"])
        item = CommentedMap()
        item.update(entry)
        entries.append(item)
    data[key] = entries


def _apply_fields(row, base, fields, label):
    for field in fields:
        value = row.get(field)
        if field in SECRETS:
            if value:
                base[field] = _text(value)
            continue
        if value is None or value == "":
            base.pop(field, None)
        elif field in ("port", "system_id", "security_level"):
            base[field] = _int(0, 10 ** 9, f"{label}: {field}")(value)
        else:
            base[field] = _text(value)


def _build_vault(row, base):
    name = _text(row.get("name") or "")
    if not name:
        raise ValueError("Every vault needs a name.")
    kind = _choice(list(VAULT_FIELDS), "Vault type")(str(row.get("type") or "").lower())
    if str(base.get("type") or "").lower() != kind:
        # another type of vault: the old one's settings don't carry over
        for fields in VAULT_FIELDS.values():
            for field in fields:
                base.pop(field, None)
    base["name"], base["type"] = name, kind
    _apply_fields(row, base, VAULT_FIELDS[kind], name)
    required = {"local": ["path"], "remote": ["host", "database", "username"], "http": ["host"],
                "httpapi": ["host"]}[kind]
    missing = [f for f in required if not base.get(f)]
    if missing:
        raise ValueError(f"Vault '{name}' needs: {', '.join(missing)}.")
    return base


def _build_remote_cdm(row, base):
    name = _name(row.get("name"), "CDM name")
    row = {**row, "type": row.get("type") or "ANDROID"}
    _choice(REMOTE_CDM_TYPES, "CDM type")(row["type"])
    _apply_fields(row, base, REMOTE_CDM_FIELDS, name)
    base["name"] = name
    missing = [f for f in ("system_id", "security_level", "host", "challenge_path", "license_path", "key")
               if base.get(f) in (None, "")]
    if missing:
        raise ValueError(f"Remote CDM '{name}' needs: {', '.join(missing)}.")
    return base


def _save_devices(data, sent):
    if "cdm_api" in sent:
        _rebuild_list(data, "cdm_api", sent["cdm_api"] or [], _build_remote_cdm)
    if "cdm" in sent:
        new = _build_per_service(sent["cdm"], service_checks._cfg().get("cdm"), _text)
        if "default" not in new:
            raise ValueError("Choose a default CDM: downloads don't start without one.")
        _sync(_section(data, "cdm"), new)


def _save_vaults(data, sent):
    _rebuild_list(data, "key_vaults", sent.get("key_vaults") or [], _build_vault)


def _template(value, kind):
    value = _text(value)
    if not value:
        raise ValueError(f"The {kind} file name pattern can't be empty.")
    allowed = TEMPLATE_FIELDS[kind]
    try:
        parts = list(string.Formatter().parse(value))
    except ValueError:
        raise ValueError(f"The {kind} pattern has an unmatched {{ or }}.")
    for _, field, spec, conversion in parts:
        if field is not None and (field not in allowed or spec or conversion):
            raise ValueError(f"The {kind} pattern can use: " + ", ".join("{%s}" % f for f in allowed) + ".")
    if "/" in value or "\\" in value:
        raise ValueError("A file name pattern can't contain slashes.")
    return value


def _save_output(data, sent):
    output = _section(data, "output_template")
    output["movies"] = _template(sent.get("movies"), "movies")
    output["series"] = _template(sent.get("series"), "series")
    output["use_last_audio"] = bool(sent.get("use_last_audio"))
    data["attach_fonts"] = bool(sent.get("attach_fonts"))
    data["download_fonts"] = bool(sent.get("download_fonts"))
    formats = _build_per_service(sent.get("subtitle_format") or {}, service_checks._cfg().get("subtitle_format"),
                                 _choice(SUBTITLE_FORMATS, "Subtitle format"))
    _sync(_section(data, "subtitle_format"), formats)
    if "subtitle_style" in sent:
        style = sent["subtitle_style"] or {}
        new = {}
        entries = [("default", style.get("default") or {})] + list((style.get("per") or {}).items())
        for key, value in entries:
            key = "default" if key == "default" else _name(key, "service name")
            entry = {}
            if str(value.get("font") or "").strip():
                entry["font"] = _text(value["font"])
            if str(value.get("size") or "").strip():
                entry["size"] = _int(1, 200, "Font size")(value["size"])
            if entry:
                new[key] = entry
        _sync(_section(data, "subtitle_style"), new)


def _argument_values(values, options):
    """Checked defaults for command line options. options: {key: option description}."""
    out = {}
    for key, value in (values or {}).items():
        option = options.get(key)
        if value is None or value == "" or value is False:
            continue
        if not option:
            raise ValueError(f"'{key}' isn't an option here.")
        if option["kind"] == "flag":
            out[key] = True
        elif option["kind"] == "int":
            out[key] = _int(0, 10 ** 9, key)(value)
        else:
            out[key] = _text(value)
    return out


def _save_defaults(data, sent):
    arguments = _section(data, "arguments")
    known = {o["key"]: o for o in command_builder.GLOBAL_OPTIONS}
    old_global = {k: v for k, v in arguments.items() if not isinstance(v, dict)}
    # options set by hand that this page doesn't list stay
    new = {k: v for k, v in old_global.items() if k not in known}
    given = {k: v for k, v in (sent.get("global") or {}).items() if k not in new}
    new.update(_argument_values(given, known))
    for service, values in (sent.get("services") or {}).items():
        service = _name(service, "service name")
        info = services_info.get_service_options(service)
        old = arguments.get(service) if isinstance(arguments.get(service), dict) else {}
        options = {o["name"]: o for o in (info or {}).get("options", [])}
        entry = {k: v for k, v in old.items() if k not in options}
        entry.update(_argument_values({k: v for k, v in values.items() if k not in entry}, options))
        if entry:
            new[service] = entry
    _sync(arguments, new)


def _save_profiles(data, sent):
    merged = service_checks._cfg().get("profiles")
    new = _build_per_service(sent.get("profiles") or {}, merged,
                             lambda v: False if v is False else _name(v, "profile name"))
    _sync(_section(data, "profiles"), new)


def _save_network(data, sent):
    headers = {}
    for key, value in (sent.get("headers") or {}).items():
        key = str(key).strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", key):
            raise ValueError(f"'{key}' isn't a header name.")
        value = _text(value)
        if value:
            headers[key] = value
    _sync(_section(data, "headers"), headers)


_SAVERS = {
    "general": _save_general, "downloader": _save_downloader, "devices": _save_devices, "vaults": _save_vaults,
    "output": _save_output, "defaults": _save_defaults, "profiles": _save_profiles, "network": _save_network,
}


def _backup():
    """Keep the file as it was before the page's last change."""
    path = Path(filenames.user_root_config)
    if path.is_file():
        real = path.resolve()  # in Docker the file is a link into the data volume
        shutil.copy2(real, real.with_name(real.name + ".before-ui"))


def save(section, sent):
    saver = _SAVERS.get(section)
    if not saver or not isinstance(sent, dict):
        raise ValueError("Unknown settings section.")
    with config_io._config_lock:
        data = config_io._read_user_config()
        try:
            saver(data, sent)
        except (AttributeError, TypeError, KeyError):
            raise ValueError("Unexpected data.")
        _backup()
        config_io._write_user_config(data)
    return read()


def check_vaults():
    """Open every vault the way a download does."""
    from vinetrimmer.config import Config

    results = []
    for vault in service_checks._cfg().get("key_vaults") or []:
        name = str(vault.get("name") or "?")
        try:
            loaded = Config.load_vault(dict(vault))
            if loaded.con is not None:
                loaded.con.close()
            results.append({"name": name, "ok": True, "message": "Opens fine."})
        except Exception as e:
            text = str(e)
            for field in SECRETS:
                if vault.get(field):
                    text = text.replace(str(vault[field]), "***")
            results.append({"name": name, "ok": False, "message": f"{type(e).__name__}: {text[:200]}"})
    return results
