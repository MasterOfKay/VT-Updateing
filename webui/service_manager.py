"""
The Services tab: replace a service's script or its .yml, or add a new one.

vinetrimmer loads every script in vinetrimmer/services/ when it starts, so one broken
file stops all downloads. An upload is therefore checked in a separate process right
after it's written, and taken back if vinetrimmer can't start with it. What was there
before is kept, and can be restored.
"""
import ast
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

from vinetrimmer import services as vt_services
from vinetrimmer.config import directories

from . import config_io, discover, paths, service_checks, services_info

SERVICES_DIR = Path(vt_services.__file__).resolve().parent
CONFIG_DIR = Path(directories.configuration) / "services"
# Names of uploaded scripts: the Docker image's start script leaves these alone.
UPLOAD_LIST = ".webui-uploads"
MAX_BYTES = 3 * 1024 * 1024
MAX_BACKUPS = 10
_RESERVED = {"__init__", "baseservice"}

_lock = threading.RLock()


def _backups_dir():
    return paths.data_file("service_backups")


# ---------------------------------------------------------------- the upload list

def _uploads():
    try:
        return set((SERVICES_DIR / UPLOAD_LIST).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def _set_uploaded(name, uploaded):
    names = _uploads()
    names.add(name) if uploaded else names.discard(name)
    (SERVICES_DIR / UPLOAD_LIST).write_text("".join(f"{n}\n" for n in sorted(names)), encoding="utf-8",
                                            newline="\n")


# ---------------------------------------------------------------- looking at a script

def _missing_packages(tree):
    wanted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            wanted.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            wanted.add(node.module.split(".")[0])
    missing = []
    for name in sorted(wanted - set(sys.stdlib_module_names) - {"vinetrimmer"}):
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return missing


def analyze(source):
    """What a service script defines, without running it. Raises ValueError if it
    can't be a service."""
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("The script isn't UTF-8 text.")
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        raise ValueError(f"The script has a Python error on line {e.lineno}: {e.msg}.")
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and any(getattr(base, "id", getattr(base, "attr", None)) == "BaseService" for base in node.bases)]
    if len(classes) != 1:
        raise ValueError("A service script defines exactly one class based on BaseService; "
                         f"this one defines {len(classes)}.")
    cls = classes[0]
    assigned = {target.id for node in cls.body if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}
    if "ALIASES" not in assigned:
        raise ValueError(f"The class {cls.name} has no ALIASES list.")
    if cls.name.lower() in _RESERVED or cls.name.startswith("_"):
        raise ValueError(f"A service can't be called {cls.name}.")
    methods = {node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "cli" not in methods:
        raise ValueError(f"The class {cls.name} has no cli() command, so it can't be started.")
    if "super().__init__(" not in text:
        raise ValueError("The script must call super().__init__(ctx) in its __init__: vinetrimmer reads a "
                         "script up to that line when it starts.")
    return {
        "name": cls.name,
        "text": text,
        "missing_packages": _missing_packages(tree),
        "uses_config": "self.config" in text,
        "missing_methods": [m for m in ("get_titles", "get_tracks", "get_chapters", "license") if m not in methods],
    }


def _check_yaml(content):
    try:
        data = yaml.safe_load(content.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise ValueError("The .yml file isn't UTF-8 text.")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise ValueError("The .yml file can't be read" + (f" (line {mark.line + 1})." if mark else "."))
    if data is not None and not isinstance(data, dict):
        raise ValueError("The .yml file must hold settings as name: value pairs.")


# The start of every download, then this one service; prints one line of JSON.
_PROBE = """
import importlib, json, sys
result = {"loads": False, "imports": False, "error": ""}
try:
    import vinetrimmer.config  # first, as in a download: it and the services import each other
    from vinetrimmer import services
    result["loads"] = sys.argv[1] in services.SERVICE_MAP
    if not result["loads"]:
        result["error"] = "vinetrimmer doesn't list the service after loading it"
    else:
        module = importlib.import_module("vinetrimmer.services." + sys.argv[1].lower())
        getattr(module, sys.argv[1]).cli
        result["imports"] = True
except BaseException as e:
    result["error"] = f"{type(e).__name__}: {e}"
print("\\n" + json.dumps(result))
"""


def _probe(name):
    try:
        run = subprocess.run([sys.executable, "-c", _PROBE, name], cwd=str(config_io.PROJECT_ROOT),
                             capture_output=True, text=True, errors="replace", timeout=90)
        return json.loads(run.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {"loads": False, "imports": False, "error": "vinetrimmer didn't start with this script"}


def refresh():
    """Make the running web UI see the services folder as it is now."""
    package = vt_services.__name__
    for attribute, value in list(vars(vt_services).items()):
        # the service classes: built from the scripts' headers, or imported since
        if isinstance(value, type) and attribute != "BaseService" and value.__module__.startswith(package):
            delattr(vt_services, attribute)
    for module in [m for m in sys.modules if m.startswith(f"{package}.") and m != f"{package}.BaseService"]:
        del sys.modules[module]
        attribute = module[len(package) + 1:]
        if attribute in vars(vt_services) and not isinstance(vars(vt_services)[attribute], type):
            delattr(vt_services, attribute)
    # Compiled copies are matched by time and size only, which a quick replacement can fool.
    for name in _uploads():
        Path(importlib.util.cache_from_source(str(SERVICES_DIR / name))).unlink(missing_ok=True)
    importlib.invalidate_caches()
    importlib.reload(vt_services)
    services_info.get_service_options.cache_clear()


# ---------------------------------------------------------------- the list

def _script_path(name):
    return SERVICES_DIR / f"{name.lower()}.py"


def _yml_path(name):
    return CONFIG_DIR / f"{name.lower()}.yml"


def _backup_list(name):
    folder = _backups_dir() / name.lower()
    return sorted((p for p in folder.glob("*") if (p / "backup.json").is_file()), reverse=True) \
        if folder.is_dir() else []


def _file_info(path):
    if not path.is_file():
        return None
    stat = path.stat()
    return {"name": path.name, "size": stat.st_size, "modified": stat.st_mtime}


def describe(name):
    uploads = _uploads()
    script = _script_path(name)
    override = SERVICES_DIR / f"{name.lower()}.yml"
    backups = _backup_list(name)
    added = False
    if script.name in uploads:
        # a service with no earlier version anywhere was added here
        oldest = backups[-1] if backups else None
        added = bool(oldest and not json.loads((oldest / "backup.json").read_text(encoding="utf-8"))["py"])
    return {
        "name": name,
        "aliases": list(vt_services.SERVICE_MAP.get(name, [])),
        "script": _file_info(script),
        "config": _file_info(_yml_path(name)),
        "override": override.is_file(),
        "uploaded": script.name in uploads,
        "added": added,
        "can_restore": bool(backups),
        "discover": name in set(discover.PACKAGES.values()),
    }


def listing():
    return [describe(s["name"]) for s in services_info.list_services()]


def report(name):
    """What a service needs and what works with it, as far as it can be told."""
    out = {"service": describe(name), "missing_packages": [], "limits": [], "check": None, "error": ""}
    try:
        source = _script_path(name).read_bytes()
        found = analyze(source)
        out["missing_packages"] = found["missing_packages"]
        if found["uses_config"] and not _yml_path(name).is_file():
            out["limits"].append(f"The script reads settings, but there's no {name.lower()}.yml for it. "
                                 "Upload one, or it will most likely fail.")
        if found["missing_methods"]:
            out["limits"].append("The script doesn't define " + ", ".join(f"{m}()" for m in found["missing_methods"])
                                 + ", so parts of a download may not work.")
    except (OSError, ValueError) as e:
        out["error"] = str(e)
    try:
        out["check"] = service_checks.check_service(name)
    except Exception as e:
        out["error"] = out["error"] or f"The service can't be loaded: {type(e).__name__}: {e}"
    if out["check"] and not out["check"]["authorization"]:
        out["limits"].append("The script has no \"Authorization:\" line in its description, so the requirement "
                             "check can't tell which login it needs.")
    if not out["service"]["discover"]:
        out["limits"].append("The Discover tab can't send titles to this service: paste the link or ID on the "
                             "Download tab.")
    if name != "Netflix" and out["service"]["added"]:
        out["limits"].append("The availability check works if the script lists its tracks the usual way. It has no "
                             "special handling for this service, so some codec or range choices may find nothing.")
    return out


# ---------------------------------------------------------------- changing

def _backup(name):
    folder = _backups_dir() / name.lower() / time.strftime("%Y%m%d-%H%M%S")
    suffix = 1
    while folder.exists():
        suffix += 1
        folder = folder.with_name(f"{folder.name.split('_')[0]}_{suffix}")
    folder.mkdir(parents=True)
    script, config = _script_path(name), _yml_path(name)
    info = {"name": name, "py": script.is_file(), "yml": config.is_file(), "uploaded": script.name in _uploads()}
    for path, there in ((script, info["py"]), (config, info["yml"])):
        if there:
            shutil.copy2(path, folder / path.name)
    (folder / "backup.json").write_text(json.dumps(info), encoding="utf-8")
    return folder


def _put_back(folder):
    info = json.loads((folder / "backup.json").read_text(encoding="utf-8"))
    name = info["name"]
    Path(importlib.util.cache_from_source(str(_script_path(name)))).unlink(missing_ok=True)
    for path, there in ((_script_path(name), info["py"]), (_yml_path(name), info["yml"])):
        if there:
            shutil.copyfile(folder / path.name, path)
        else:
            path.unlink(missing_ok=True)
    _set_uploaded(_script_path(name).name, info["uploaded"])
    return name


def _prune(name):
    # the oldest one stays: it's the version from before the first upload
    backups = _backup_list(name)
    for folder in backups[MAX_BACKUPS - 1:-1]:
        shutil.rmtree(folder, ignore_errors=True)


def install(files, service=None):
    """files: [(file name, content)], one .py and/or one .yml. service: the one being
    replaced, or None to add a new one."""
    scripts = [f for f in files if f[0].lower().endswith(".py")]
    configs = [f for f in files if f[0].lower().endswith((".yml", ".yaml"))]
    if len(scripts) > 1 or len(configs) > 1 or len(scripts) + len(configs) != len(files) or not files:
        raise ValueError("Drop one .py file, one .yml file, or one of each.")
    if any(len(content) > MAX_BYTES for _, content in files):
        raise ValueError("A file is larger than 3 MB: that's no service file.")
    with _lock:
        known = {s["name"] for s in services_info.list_services()}
        if service and service not in known:
            raise ValueError("Choose a service from the list.")
        found = analyze(scripts[0][1]) if scripts else None
        if configs:
            _check_yaml(configs[0][1])
        if service:
            name = service
            if found and found["name"] != service:
                raise ValueError(f"This script is for {found['name']}, not {service}. "
                                 + (f"Choose {found['name']} in the list to replace it." if found["name"] in known
                                    else "Use \"Add a new service\" for it."))
        else:
            if not found:
                raise ValueError("A new service needs its .py script (and usually a .yml).")
            name = found["name"]
            clash = next((k for k in known if k.lower() == name.lower()), None)
            if clash or _script_path(name).exists():
                raise ValueError(f"{clash or name} is already there. Choose it in the list to replace it.")
            taken = {a.lower(): k for k, aliases in vt_services.SERVICE_MAP.items() for a in [k, *aliases]}
            tree = ast.parse(found["text"])
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ALIASES" for t in node.targets):
                    try:
                        aliases = ast.literal_eval(node.value)
                    except ValueError:
                        aliases = []
                    for alias in aliases:
                        if str(alias).lower() in taken:
                            raise ValueError(f"Its alias '{alias}' already belongs to {taken[str(alias).lower()]}.")

        folder = _backup(name)
        saved = []
        try:
            if scripts:
                _set_uploaded(_script_path(name).name, True)
                _script_path(name).write_bytes(scripts[0][1])
                saved.append(f"services/{_script_path(name).name}")
            if configs:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                _yml_path(name).write_bytes(configs[0][1])
                saved.append(f"config/services/{_yml_path(name).name}")
            result = _probe(name)
            if not result["loads"]:
                raise ValueError("Nothing was changed: vinetrimmer couldn't start with this file. "
                                 + result["error"][:400])
        except Exception:
            _put_back(folder)
            shutil.rmtree(folder, ignore_errors=True)
            raise
        _prune(name)
        refresh()
        out = report(name)
        out["saved"] = saved
        if not result["imports"]:
            out["error"] = f"Saved, but this service won't run yet: {result['error'][:400]}"
        return out


def restore(name):
    """Back to the version from before the latest upload."""
    with _lock:
        backups = _backup_list(name)
        if not backups:
            raise ValueError("There's no earlier version of this service.")
        _put_back(backups[0])
        shutil.rmtree(backups[0], ignore_errors=True)
        refresh()
    return {"service": name, "removed": not _script_path(name).is_file()}
