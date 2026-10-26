"""
Get information about available services.
"""
import importlib
import re
from functools import lru_cache

import click

from vinetrimmer import services as vt_services


def list_services():
    """All services vinetrimmer knows about (from SERVICE_MAP, already built at import time)."""
    out = [
        {
            "name": name,
            "aliases": list(aliases),
            "label": f"{name} ({aliases[0]})" if aliases else name,
        }
        for name, aliases in vt_services.SERVICE_MAP.items()
    ]
    out.sort(key=lambda s: s["name"].lower())
    return out


def _param_to_dict(param):
    if isinstance(param, click.Argument):
        return None  # "title" (and any other positional arg) is handled separately

    kind = "flag" if param.is_flag else "text"
    choices = None
    if isinstance(param.type, click.Choice):
        kind = "choice"
        choices = list(param.type.choices)

    default = param.default
    if not isinstance(default, (str, int, float, type(None))):
        default = None

    return {
        "name": param.name,
        "opts": list(param.opts),
        "help": param.help or "",
        "kind": kind,
        "choices": choices,
        "default": default,
    }


def _title_hint(cls):
    patterns = getattr(cls, "TITLE_RE", None)
    if not patterns:
        return "Title / URL / ID"
    if isinstance(patterns, (str, re.Pattern)):
        patterns = [patterns]
    first = getattr(patterns[0], "pattern", patterns[0])  # ParamountPlus uses re.compile()
    first = first.lstrip().lstrip("^")
    if "https?" in first and not first.startswith("(?:https?"):
        return "Paste the full URL"
    return "Paste the title ID, or a full URL"


@lru_cache(maxsize=None)
def get_service_options(service_name):
    """
    Import the one selected service module (lazily/cached, not all ~40 at startup)
    and read its special CLI options straight off the click.Command it registers -
    mirrors what `AliasedGroup.get_command` does for the real CLI.
    """
    key = vt_services.get_service_key(service_name)
    if not key:
        return None

    module = importlib.import_module(f"vinetrimmer.services.{key.lower()}")
    cls = getattr(module, key)
    cli = getattr(cls, "cli", None)

    options = [d for d in (_param_to_dict(p) for p in (cli.params if cli else [])) if d]

    return {
        "name": key,
        "aliases": list(cls.ALIASES),
        "geofence": list(getattr(cls, "GEOFENCE", [])),
        "options": options,
        "title_hint": _title_hint(cls),
    }
