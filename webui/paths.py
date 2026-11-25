"""
Where the web UI keeps its own files (queue, settings, pins, API key, caches).

By default that's the webui/ folder itself. VT_WEBUI_DATA_DIR moves it elsewhere - the
Docker image points it at its data volume, so nothing is lost when the container is
rebuilt.
"""
import os
from pathlib import Path


def data_file(name):
    root = os.environ.get("VT_WEBUI_DATA_DIR")
    directory = Path(root) if root else Path(__file__).resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def in_docker():
    return bool(os.environ.get("VT_DOCKER"))
