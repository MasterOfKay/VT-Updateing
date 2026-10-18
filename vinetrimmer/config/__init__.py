import os
import tempfile
from types import SimpleNamespace
from pathlib import Path

import yaml
from appdirs import AppDirs
from requests.utils import CaseInsensitiveDict

from vinetrimmer.objects.vaults import Vault
from vinetrimmer.utils.collections import merge_dict


class Config:
    @staticmethod
    def load_vault(vault):
        return Vault(**{
            "type_" if k == "type" else k: v for k, v in vault.items()
        })


class Directories:
    def __init__(self):
        self.app_dirs = AppDirs("vinetrimmer", False)
        self.package_root = Path(__file__).resolve().parent.parent
        self.configuration = self.package_root / "config"
        self.user_configs = self.package_root
        self.service_configs = self.user_configs / "services"
        self.data = self.package_root
        self.downloads = Path(__file__).resolve().parents[2] / "downloads"
        self.temp = Path(__file__).resolve().parents[2] / "temp"
        self.cache = self.package_root / "cache"
        self.cookies = self.data / "cookies"
        # Font files to attach to muxed files, so ASS subtitles render with the
        # family they ask for rather than whatever the player substitutes.
        self.fonts = Path(__file__).resolve().parents[2] / "assets" / "fonts"
        self.logs = self.package_root / "logs"
        self.devices = self.data / "devices"

class Filenames:
    def __init__(self):
        self.log = os.path.join(directories.logs, "vinetrimmer_{time}.log")
        self.root_config = os.path.join(directories.configuration, "vinetrimmer.yml")
        self.user_root_config = os.path.join(directories.user_configs, "vinetrimmer.yml")
        self.service_config = os.path.join(directories.configuration, "services", "{service}.yml")
        self.user_service_config = os.path.join(directories.service_configs, "{service}.yml")
        self.subtitles = os.path.join(directories.temp, "TextTrack_{id}_{language_code}.srt")
        self.chapters = os.path.join(directories.temp, "{filename}_chapters.txt")


directories = Directories()
filenames = Filenames()
with open(filenames.root_config) as fd:
    config = yaml.safe_load(fd)
with open(filenames.user_root_config) as fd:
    user_config = yaml.safe_load(fd)
merge_dict(config, user_config)
config = SimpleNamespace(**config)
credentials = config.credentials

def setup_paths():
    downloads_path = config.directories.get('downloads')
    temp_path = config.directories.get('temp')
    fonts_path = config.directories.get('fonts')

    if downloads_path and Path(downloads_path).is_dir():
        directories.downloads = Path(downloads_path)

    if fonts_path and Path(fonts_path).is_dir():
        directories.fonts = Path(fonts_path)

    if temp_path and Path(temp_path).is_dir():
        directories.temp = Path(temp_path)
        filenames.subtitles = os.path.join(directories.temp, "TextTrack_{id}_{language_code}.srt")
        filenames.chapters = os.path.join(directories.temp, "{filename}_chapters.txt")


setup_paths()

# This serves two purposes:
# - Allow `range` to be used in the arguments section in the config rather than just `range_`
# - Allow sections like [arguments.Amazon] to work even if an alias (e.g. AMZN or amzn) is used.
#   CaseInsensitiveDict is used for `arguments` above to achieve case insensitivity.
# NOTE: The import cannot be moved to the top of the file, it will cause a circular import error.
from vinetrimmer.services import SERVICE_MAP  # noqa: E402

if "range_" not in config.arguments:
    config.arguments["range_"] = config.arguments.get("range_")
for service, aliases in SERVICE_MAP.items():
    for alias in aliases:
        config.arguments[alias] = config.arguments.get(service)
config.arguments = CaseInsensitiveDict(config.arguments)

# Setup subtitle_format configuration with service-specific settings
if hasattr(config, 'subtitle_format'):
    for service, aliases in SERVICE_MAP.items():
        for alias in aliases:
            if service in config.subtitle_format:
                config.subtitle_format[alias] = config.subtitle_format.get(service)
    config.subtitle_format = CaseInsensitiveDict(config.subtitle_format)
else:
    config.subtitle_format = CaseInsensitiveDict({'default': 'srt'})

# Same alias expansion for subtitle_style, which is looked up by the same key. Tracks
# carry a service's first alias as their source, so a section written out as
# [subtitle_style.Netflix] has to answer to "NF" as well.
if hasattr(config, 'subtitle_style') and isinstance(config.subtitle_style, dict):
    for service, aliases in SERVICE_MAP.items():
        for alias in aliases:
            if service in config.subtitle_style:
                config.subtitle_style[alias] = config.subtitle_style.get(service)
    config.subtitle_style = CaseInsensitiveDict(config.subtitle_style)
else:
    config.subtitle_style = CaseInsensitiveDict()


