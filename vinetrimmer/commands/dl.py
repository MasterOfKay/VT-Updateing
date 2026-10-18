# Updated 2024-06
# Added by MasterOfKay: Hybrid, HybridPlus, Multi download function
import base64
import contextlib
import itertools
import html
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import traceback
from zlib import crc32
from pathlib import Path
from datetime import datetime, timedelta
from http.cookiejar import MozillaCookieJar
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import click
import requests
from appdirs import AppDirs
from langcodes import Language
from pymediainfo import MediaInfo
from Crypto.Random import get_random_bytes
import math
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, DownloadColumn, TransferSpeedColumn, SpinnerColumn
from rich.live import Live
from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich.padding import Padding
from rich.markup import escape as rich_escape
from rich.text import Text
from functools import partial

from vinetrimmer.utils.richconsole import console
from vinetrimmer import services
from vinetrimmer.config import Config, config, credentials, directories, filenames
from vinetrimmer.objects import AudioTrack, Credential, TextTrack, Title, Titles, VideoTrack, Track
from vinetrimmer.objects.vaults import InsertResult, Vault, Vaults
from vinetrimmer.utils import Cdm, is_close_match
from vinetrimmer.utils.click import (AliasedGroup, ContextData, acodec_param, language_param, quality_param,
                                     range_param, vcodec_param, wanted_param, channels_param)
from vinetrimmer.utils.collections import as_list, merge_dict
from vinetrimmer.utils.io import load_yaml
from vinetrimmer.utils import track_export
from vinetrimmer.utils.widevine.device import LocalDevice, RemoteDevice
from pyplayready.cdm import Cdm as Cdm_PR
from pyplayready.device import Device as Device_PR
from pyplayready.system.pssh import PSSH as PSSH_PR
from pyplayready.crypto.ecc_key import ECCKey
from pyplayready.system.bcert import CertificateChain, Certificate
from vinetrimmer.vendor.pymp4.parser import Box
import tempfile
from xml.sax.saxutils import escape

# Prefix of the progress snapshot lines printed for the web UI - must match
# PROGRESS_MARKER in webui/queue_manager.py.
WEBUI_PROGRESS_MARKER = "@@VT_PROGRESS@@ "
# --stop-file: exit code when it stopped before the next title (not an error, not "done").
STOP_EXIT_CODE = 3

def apply_tag_to_file(file_path, tag):
    """Apply group tag to MKV file metadata using mkvpropedit."""
    if not tag:
        return

    mkvpropedit = shutil.which("mkvpropedit")
    if not mkvpropedit:
        log = logging.getLogger("dl")
        log.debug("mkvpropedit not found; skipping tag metadata")
        return

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Tags>',
        '  <Tag>',
        '    <Targets/>',
        f'    <Simple><Name>ENCODED_BY</Name><String>{escape(tag)}</String></Simple>',
        '  </Tag>',
        '</Tags>',
    ]

    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write("\n".join(xml_lines))
        tmp_path = Path(f.name)

    try:
        subprocess.run(
            [mkvpropedit, str(file_path), "--tags", f"global:{tmp_path}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log = logging.getLogger("dl")
        log.debug(f"Applied tag '{tag}' to file metadata")
    finally:
        tmp_path.unlink(missing_ok=True)

def create_device(device_dir) -> None:
    """Create a Playready Device (.prd) file from an ECC private group key and group certificate chain"""
    group_key = device_dir / 'zgpriv.dat'
    group_certificate = device_dir / 'bgroupcert.dat'
    infofile = device_dir / 'PR.json'

    if not group_key.is_file():
        raise  TypeError("group_key: Not a path to a file, or it doesn't exist.")
    if not group_certificate.is_file():
        raise TypeError("group_certificate: Not a path to a file, or it doesn't exist.")
    
    if infofile.is_file():
        with open(infofile, 'r') as file:
            info = json.loads(file.read())
        if "expiry" in info and datetime.fromisoformat(info["expiry"]) > datetime.now():
            logging.info(" + Security Level: %s", info["SecurityLevel"])
            logging.info(" + Device expiry: %s", info["expiry"])
            return device_dir / info["device"]
        else:
            infofile.unlink()
            device_prd = Path(device_dir / info["device"])
            if device_prd.is_file():
                device_prd.unlink()
            logging.warning(" + Refreshing Device...")

    encryption_key = ECCKey.generate()
    signing_key = ECCKey.generate()

    group_key = ECCKey.load(group_key)
    certificate_chain = CertificateChain.load(group_certificate)

    new_certificate = Certificate.new_leaf_cert(
        cert_id=get_random_bytes(16),
        security_level=certificate_chain.get_security_level(),
        client_id=get_random_bytes(16),
        signing_key=signing_key,
        encryption_key=encryption_key,
        group_key=group_key,
        parent=certificate_chain
    )
    certificate_chain.prepend(new_certificate)

    device = Device_PR(
        group_key=group_key.dumps(),
        encryption_key=encryption_key.dumps(),
        signing_key=signing_key.dumps(),
        group_certificate=certificate_chain.dumps(),
    )
    
    expiry = (datetime.now() + timedelta(hours=1)).isoformat()
    prd_bin = device.dumps()
    out_path = device_dir / f"{device.get_name()}_{crc32(prd_bin).to_bytes(4, 'big').hex()}.prd"

    if out_path.exists():
        logging.error(f"A file already exists at the path '{out_path}', cannot overwrite.")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(device.dumps())

    logging.info(" + Created Playready Device (.prd) file, %s", out_path.name)
    logging.info(" + Security Level: %s", device.security_level)
    logging.info(" + Device expiry: %s", expiry)

    with open(infofile, 'w') as file:
        json.dump({"expiry": expiry, "device": out_path.name, "SecurityLevel": device.security_level}, file)

    return out_path


def get_cdm(service, profile=None, cdm_name=None):
    """
    Get CDM Device (either remote or local) for a specified service.
    Raises a ValueError if there's a problem getting a CDM.
    """
    if not cdm_name:
        cdm_name = config.cdm.get(service) or config.cdm.get("default")
    if not cdm_name:
        raise ValueError("A CDM to use wasn't listed in the vinetrimmer.yml config")
    if isinstance(cdm_name, dict):
        if not profile:
            raise ValueError("CDM config is mapped for profiles, but no profile was chosen")
        cdm_name = cdm_name.get(profile) or config.cdm.get("default")
        if not cdm_name:
            raise ValueError(f"A CDM to use was not mapped for the profile {profile}")
    try:
        device_dir = Path(directories.devices) / cdm_name
        # Check if it's a PlayReady device directory by looking for required files
        if device_dir.is_dir() and (device_dir / 'zgpriv.dat').is_file() and (device_dir / 'bgroupcert.dat').is_file():
            device_path = create_device(device_dir)
            return Device_PR.load(device_path)
        # Check if it's a direct path to a PlayReady device file
        if "sl2000" in cdm_name.lower() or "sl3000" in cdm_name.lower():
            return Device_PR.load(os.path.join(directories.devices, f"{cdm_name}.prd"))
        else:
            # Otherwise, treat as a Widevine device
            return LocalDevice.load(os.path.join(directories.devices, f"{cdm_name}.wvd"))
    except FileNotFoundError:
        dirs = [
            os.path.join(directories.devices, cdm_name),
            os.path.join(AppDirs("pywidevine", False).user_data_dir, "devices", cdm_name),
            os.path.join(AppDirs("pywidevine", False).site_data_dir, "devices", cdm_name),
        ]

        for d in dirs:
            try:
                # Try to treat as a Widevine device directory
                return LocalDevice.from_dir(d)
            except FileNotFoundError:
                pass

        cdm_api = next(iter(x for x in config.cdm_api if x["name"] == cdm_name), None)
        if cdm_api:
            return RemoteDevice(**cdm_api)

        raise ValueError(f"Device {cdm_name!r} not found")

def get_service_setting(section, key, service_name, fallback):
    """
    Resolve a config value that may either be a plain scalar (applies to all
    services) or a dict of per-service overrides plus a 'default', mirroring
    the existing `downloader:` config pattern.
    """
    section_config = getattr(config, section, {}) if hasattr(config, section) else {}
    value = section_config.get(key, fallback)
    if isinstance(value, dict):
        return value.get(service_name, value.get('default', fallback))
    return value


def get_service_config(service):
    """Get both service config and service secrets as one merged dictionary."""
    service_config = load_yaml(filenames.service_config.format(service=service.lower()))

    user_config = (load_yaml(filenames.user_service_config.format(service=service.lower()))
                   or load_yaml(filenames.user_service_config.format(service=service)))

    if user_config:
        merge_dict(service_config, user_config)

    return service_config


def get_profile(service):
    """
    Get the default profile for a service from the config.
    """
    profile = config.profiles.get(service)
    if profile is False:
        return None  # auth-less service if `false` in config
    if not profile:
        profile = config.profiles.get("default")
    if not profile:
        raise ValueError(f"No profile has been defined for '{service}' in the config.")

    return profile


def get_cookie_jar(service, profile):
    """Get the profile's cookies if available."""
    cookie_file = os.path.join(directories.cookies, service.lower(), f"{profile}.txt")
    if not os.path.isfile(cookie_file):
        cookie_file = os.path.join(directories.cookies, service, f"{profile}.txt")
    if os.path.isfile(cookie_file):
        cookie_jar = MozillaCookieJar(cookie_file)
        with open(cookie_file, "r+", encoding="utf-8") as fd:
            unescaped = html.unescape(fd.read())
            fd.seek(0)
            fd.truncate()
            fd.write(unescaped)
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        return cookie_jar
    return None


def get_credentials(service, profile="default"):
    """Get the profile's credentials if available."""
    cred = credentials.get(service, {})

    if isinstance(cred, dict):
        cred = cred.get(profile)
    elif profile != "default":
        return None

    if cred:
        if isinstance(cred, list):
            return Credential(*cred)
        else:
            return Credential.loads(cred)


@click.group(name="dl", short_help="Download from a service.", cls=AliasedGroup, context_settings=dict(
    help_option_names=["-?", "-h", "--help"],
    max_content_width=116,  # max PEP8 line-width, -4 to adjust for initial indent
    default_map=config.arguments
))
@click.option("--debug", is_flag=True, hidden=True)  # Handled by vinetrimmer.py
@click.option("-p", "--profile", type=str, default=None,
              help="Profile to use when multiple profiles are defined for a service.")
@click.option("-q", "--quality", callback=quality_param, default=None,
              help="Download Resolution, defaults to best available.")
@click.option("-v", "--vcodec", callback=vcodec_param, default="H264",
              help="Video Codec, defaults to H264.")
@click.option("-a", "--acodec", callback=acodec_param, default=None,
              help="Audio Codec")
@click.option("-vb", "--vbitrate", "vbitrate", type=int,
              default=None,
              help="Video Bitrate, defaults to Max.")
@click.option("-ab", "--abitrate", "abitrate", type=int,
              default=None,
              help="Audio Bitrate, defaults to Max.")              
@click.option("-aa", "--atmos", is_flag=True, default=False,
              help="Prefer Atmos Audio")
@click.option("-ch", "--channels", help="Audio Channels")
@click.option("-r", "--range", "range_", callback=range_param, default="SDR",
              help="Video Color Range, defaults to SDR.")
@click.option("-w", "--wanted", callback=wanted_param, default=None,
              help="Wanted episodes, e.g. `S01-S05,S07`, `S01E01-S02E03`, `S02-S02E03`, e.t.c, defaults to all.")
@click.option("-al", "--alang", callback=language_param, default="orig",
              help="Language wanted for audio.")
@click.option("-sl", "--slang", callback=language_param, default="all",
              help="Language wanted for subtitles.")
@click.option("--delay", type=int, default=None,
              help="Delay between title processing")
@click.option("--proxy", type=str, default=None,
              help="Proxy URI to use. If a 2-letter country is provided, it will try get a proxy from the config.")
@click.option("-A", "--audio-only", is_flag=True, default=False,
              help="Only download audio tracks.")
@click.option("-S", "--subs-only", is_flag=True, default=False,
              help="Only download subtitle tracks.")
@click.option("-C", "--chapters-only", is_flag=True, default=False,
              help="Only download chapters.")
@click.option("-ns", "--no-subs", is_flag=True, default=False,
              help="Do not download subtitle tracks.")
@click.option("-na", "--no-audio", is_flag=True, default=False,
              help="Do not download audio tracks.")
@click.option("-nv", "--no-video", is_flag=True, default=False,
              help="Do not download video tracks.")
@click.option("-nc", "--no-chapters", is_flag=True, default=False,
              help="Do not download chapters tracks.")
@click.option("-ad", "--audio-description", is_flag=True, default=False,
              help="Download audio description tracks.")
@click.option("--list", "list_", is_flag=True, default=False,
              help="Skip downloading and list available tracks and what tracks would have been downloaded.")
@click.option("--selected", is_flag=True, default=False,
              help="List selected tracks and what tracks are downloaded.")
@click.option("--cdm", type=str, default=None,
              help="Override the CDM that will be used for decryption.")
@click.option("--keys", is_flag=True, default=False,
              help="Skip downloading, retrieve the decryption keys (via CDM or Key Vaults) and print them.")
@click.option("--cache", is_flag=True, default=False,
              help="Disable the use of the CDM and only retrieve decryption keys from Key Vaults. "
                   "If a needed key is unable to be retrieved from any Key Vaults, the title is skipped.")
@click.option("--no-cache", is_flag=True, default=False,
              help="Disable the use of Key Vaults and only retrieve decryption keys from the CDM.")
@click.option("--no-proxy", is_flag=True, default=False,
              help="Force disable all proxy use.")
@click.option("--force-proxy", is_flag=True, default=False,
              help="Force using proxy even if current region matches.")
@click.option("-nm", "--no-mux", is_flag=True, default=False,
              help="Do not mux the downloaded and decrypted tracks.")
@click.option("--mux", is_flag=True, default=False,
              help="Force muxing when using --audio-only/--subs-only/--chapters-only.")
@click.option("--worst", is_flag=True, default=False,
              help="Choose the worst available video tracks rather than the best")
@click.option("-nf", "--no-forced", is_flag=True, default=False,
              help="Do not download forced subtitle tracks.")
@click.option("--tag", type=str, default=None,
              help="Add group tag to filenames and file metadata (e.g., 'MasterOfKay').")
@click.option("--stop-file", "stop_file", type=click.Path(dir_okay=False), default=None, envvar="VT_STOP_FILE",
              help="Stop gracefully once this file exists: the current title (episode) is finished, the next "
                   "one isn't started. Exits with code 3 when it stopped early.")
@click.option("--first-title", is_flag=True, default=False,
              help="Only process the first (wanted) title, e.g. to quickly --list what a show offers.")
@click.option("--list-json", "list_json", type=click.Path(dir_okay=False), default=None,
              help="Write every title's available tracks (and the selected ones) to this file as JSON.")
@click.option("--audio-pick", "audio_pick", type=str, default=None,
              help="Pick the audio track per language: LANG=CODEC/CHANNELS/KBPS, comma separated, "
                   "any part optional, e.g. `en=AAC/2.0/128,de=E-AC3/5.1,ja=best`.")
@click.pass_context
def dl(ctx, profile, cdm, tag, *_, **__):
    log = logging.getLogger("dl")

    service = ctx.params.get("service_name") or services.get_service_key(ctx.invoked_subcommand)
    if not service:
        raise click.ClickException(" - Unable to find service")

    profile = profile or get_profile(service)
    service_config = get_service_config(service)
    vaults = []
    for vault in config.key_vaults:
        try:
            vaults.append(Config.load_vault(vault))
        except Exception as e:
            log.error(f" - Failed to load vault {vault['name']!r}: {e}")
    vaults = Vaults(vaults, service=service)
    local_vaults = sum(v.type == Vault.Types.LOCAL for v in vaults)
    remote_vaults = sum(v.type == Vault.Types.REMOTE for v in vaults)
    http_vaults = sum(v.type == Vault.Types.HTTP for v in vaults)
    log.info(f" + {local_vaults} Local Vault{'' if local_vaults == 1 else 's'}")
    log.info(f" + {remote_vaults} Remote Vault{'' if remote_vaults == 1 else 's'}")
    log.info(f" + {http_vaults} HTTP Vault{'' if http_vaults == 1 else 's'}")

    try:
        device = get_cdm(service, profile, cdm)
    except ValueError as e:
        raise click.ClickException(f" - {e}")
    device_name = device.get_name() if "sl3000" in device.get_name() or "sl2000" in device.get_name() else device.system_id
    log.info(f" + Loaded {device.__class__.__name__}: {device_name} (L{device.security_level})")
    cdm = Cdm_PR.from_device(device) if "sl3000" in device.get_name()  or "sl2000" in device.get_name() else Cdm(device)

    if profile:
        cookies = get_cookie_jar(service, profile)
        credentials = get_credentials(service, profile)
        if not cookies and not credentials and service_config.get("needs_auth", True):
            raise click.ClickException(f" - Profile {profile!r} has no cookies or credentials")
    else:
        cookies = None
        credentials = None
    
    # Set tag from CLI or config
    if tag:
        config.tag = tag
    elif not hasattr(config, 'tag'):
        config.tag = None

    ctx.obj = ContextData(
        config=service_config,
        vaults=vaults,
        cdm=cdm,
        profile=profile,
        cookies=cookies,
        credentials=credentials,
    )


@dl.result_callback()
@click.pass_context
def result(ctx, service, quality, range_, wanted, alang, slang, audio_only, subs_only, chapters_only, audio_description,
           list_, keys, cache, no_cache, no_subs, no_audio, no_video, no_chapters, atmos, vbitrate: int, abitrate: int, channels, no_mux, worst, mux, delay, selected, no_forced,
           first_title=False, list_json=None, audio_pick=None, stop_file=None, *_, **__):
    def ccextractor():
        log.info("Extracting EIA-608 captions from stream with CCExtractor")
        track_id = f"ccextractor-{track.id}"
        # TODO: Is it possible to determine the language of EIA-608 captions?
        cc_lang = track.language
        try:
            cc = track.ccextractor(
                track_id=track_id,
                out_path=filenames.subtitles.format(id=track_id, language_code=cc_lang),
                language=cc_lang,
                original=False,
            )
        except EnvironmentError:
            log.warning(" - CCExtractor not found, cannot extract captions")
        else:
            if cc:
                title.tracks.add(cc)
                log.info(" + Extracted")
            else:
                log.info(" + No captions found")
    
    def check_dovi_tool():
        """Check if dovi_tool is available for hybrid mode."""
        import shutil
        
        # Try common names
        names = ["dovi_tool", "dovi_tool.exe"]
        for name in names:
            tool = shutil.which(name)
            if tool:
                return True
                
        # Check in binaries folder
        binaries_dir = Path(directories.temp).parent / "binaries"
        for name in names:
            tool_path = binaries_dir / name
            if tool_path.exists():
                return True
                
        return False

    log = service.log

    service_name = service.__class__.__name__

    def _audio_codec_family(track):
        codec = (getattr(track, "codec", "") or "").lower()
        if "dts" in codec:
            return "dts"
        if any(x in codec for x in ("eac-3", "ec-3", "eac3", "ddp", "atmos")):
            return "eac3"
        return "other"

    def _prefer_eac3_over_dts(selected_audios, all_audios):
        """Replace selected DTS tracks with matching E-AC3 variants when available."""
        if not selected_audios:
            return selected_audios, 0

        def _audio_key(track):
            return (
                str(getattr(track, "language", "")),
                getattr(track, "channels", None),
                bool(getattr(track, "descriptive", False)),
                bool(getattr(track, "is_original_lang", False)),
            )

        best_eac3_by_key = {}
        for audio in all_audios:
            if _audio_codec_family(audio) != "eac3":
                continue
            key = _audio_key(audio)
            current = best_eac3_by_key.get(key)
            if current is None or float(audio.bitrate or 0.0) > float(current.bitrate or 0.0):
                best_eac3_by_key[key] = audio

        swapped = 0
        updated = []
        for audio in selected_audios:
            if _audio_codec_family(audio) != "dts":
                updated.append(audio)
                continue
            replacement = best_eac3_by_key.get(_audio_key(audio))
            if replacement:
                updated.append(replacement)
                swapped += 1
            else:
                updated.append(audio)

        deduped = []
        seen_ids = set()
        for audio in updated:
            audio_id = getattr(audio, "id", None)
            if audio_id in seen_ids:
                continue
            seen_ids.add(audio_id)
            deduped.append(audio)

        return deduped, swapped
    
    # Check for dovi_tool if hybrid mode is requested
    if range_ in ["HYBRID", "HYBRIDPLUS"]:
        if not check_dovi_tool():
            log.warning("=" * 60)
            log.warning("WARNING: dovi_tool not found!")
            log.warning("Hybrid mode requires dovi_tool to be installed.")
            log.warning("Download from: https://github.com/quietvoid/dovi_tool/releases")
            log.warning("Place it in your PATH or in the binaries folder.")
            log.warning("=" * 60)
            log.exit(" - Cannot proceed with hybrid mode without dovi_tool")

    log.info("Retrieving Titles")
    try:
        titles = Titles(as_list(service.get_titles()))
    except requests.HTTPError as e:
        log.debug(traceback.format_exc())
        raise log.exit(f" - HTTP Error {e.response.status_code}: {e.response.reason}")
    if not titles:
        raise click.ClickException(" - No titles returned!")
    titles.order()
    titles.print()

    first = True

    try:
        audio_picks = track_export.parse_audio_pick(audio_pick) if audio_pick else []
    except ValueError as e:
        raise click.ClickException(f" - {e}")
    json_entries = []
    wanted_titles = titles.with_wanted(wanted)
    if first_title:
        wanted_titles = itertools.islice(wanted_titles, 1)

    stopped_early = False
    for title in wanted_titles:
        # --stop-file: checked between titles only, so the one before is always complete.
        if not first and stop_file and os.path.exists(stop_file):
            label = f"{title.name} S{title.season or 0:02}E{title.episode or 0:02}" \
                if title.type == Title.Types.TV else title.name
            log.warning(f"Stop requested - stopped before {label} [{title.id}]")
            stopped_early = True
            break
        if service_name.lower()=="crunchyroll" or service_name.lower()=="cr":
            log.info(f"Closing all sessions...")
            service.close_all_sessions()
        if not first and delay:
            d = delay + random.randint(math.floor(int(-delay/5.0)), math.floor((delay/5.0))) 
            log.info(f"Delaying for {d}s before getting next title...")
            time.sleep(d)

        first = False

        if title.type == Title.Types.TV:
            log.info("Getting tracks for {title} S{season:02}E{episode:02}{name} [{id}]".format(
                title=title.name,
                season=title.season or 0,
                episode=title.episode or 0,
                name=f" - {title.episode_name}" if title.episode_name else "",
                id=title.id,
            ))
        else:
            log.info("Getting tracks for {title}{year} [{id}]".format(
                title=title.name,
                year=f" ({title.year})" if title.year else "",
                id=title.id,
            ))

        try:
            title.tracks.add(service.get_tracks(title), warn_only=True)
            title.tracks.add(service.get_chapters(title))
            #if not next((x for x in title.tracks.videos if x.language.language in (v_lang or lang)), None) and title.tracks.videos:
             #   lang = [title.tracks.videos[0].language["language"]]
              #  log.info("Defaulting language to {lang} for tracks".format(lang=lang[0].upper()))
        except requests.HTTPError as e:
            log.debug(traceback.format_exc())
            raise log.exit(f" - HTTP Error {e.response.status_code}: {e.response.reason}")
        title.tracks.sort_videos()
        title.tracks.sort_audios(by_language=alang)
        title.tracks.sort_subtitles(by_language=slang)
        title.tracks.sort_chapters()

        for track in title.tracks:
            if track.language == Language.get("none"):
                track.language = title.original_lang
            track.is_original_lang = is_close_match(track.language, [title.original_lang])        

        if not list(title.tracks):
            log.error(" - No tracks returned!")
            continue
        if not selected:
            log.info("> All Tracks:")
            title.tracks.print()

        json_entry = None
        if list_json:
            # Written now, before selection: a range/codec that isn't available makes
            # selection fail below, but the full track list is still what was asked for.
            json_entry = track_export.title_entry(service_name, title, titles)
            json_entries.append(json_entry)
            track_export.write_json(list_json, json_entries)

        try:
            # For hybrid mode, select both HDR and DV tracks; for normal mode, select only one
            is_hybrid_mode = range_ in ["HYBRID", "HYBRIDPLUS"]
            all_audio_tracks = list(title.tracks.audios)
            #title.tracks.select_videos(by_quality=quality, by_range=range_, one_only=True)
            title.tracks.select_videos(by_quality=quality, by_vbitrate=vbitrate, by_range=range_, one_only=not is_hybrid_mode, by_worst=worst)
            title.tracks.select_audios(by_language=alang, by_bitrate=abitrate, with_atmos=atmos, with_descriptive=False, by_channels=channels)
           # title.tracks.select_audios(by_language=alang, with_descriptive=audio_description)
            title.tracks.select_subtitles(by_language=slang, with_cc=True, with_sdh=True, with_forced=False if no_forced else True)

            if service_name == "DisneyPlus" and title.tracks.audios:
                preferred_audios, swapped = _prefer_eac3_over_dts(title.tracks.audios, all_audio_tracks)
                if swapped:
                    log.info(f" + DisneyPlus audio preference: replaced {swapped} DTS track(s) with E-AC3")
                title.tracks.audios = preferred_audios
            if audio_picks:
                title.tracks.audios = track_export.apply_audio_pick(title.tracks.audios, all_audio_tracks, audio_picks)
        except ValueError as e:
            log.error(f" - {e}")
            continue

        # For hybrid mode, deduplicate audio tracks to prevent sync issues
        # Hybrid mode with 2 videos should have the same audio tracks
        if range_ in ["HYBRID", "HYBRIDPLUS"] and len(title.tracks.videos) == 2:
            # Deduplicate audio by language and codec - keep only one copy
            seen_audio = {}
            unique_audios = []
            for audio in title.tracks.audios:
                # Use language + codec as dedup key
                key = (str(audio.language) if hasattr(audio, 'language') else 'unknown',
                       audio.codec if hasattr(audio, 'codec') else 'unknown')
                if key not in seen_audio:
                    seen_audio[key] = audio
                    unique_audios.append(audio)
            
            if len(unique_audios) < len(title.tracks.audios):
                log.info(f" + Deduplicating audio tracks for hybrid mode: {len(title.tracks.audios)} -> {len(unique_audios)}")
                title.tracks.audios = unique_audios

        if no_video:
            title.tracks.videos.clear()
            
        if no_audio:
            title.tracks.audios.clear()
            
        if no_subs:
            title.tracks.subtitles.clear()
            
        if no_chapters:
            title.tracks.chapters.clear()    
        
        if audio_only or subs_only or chapters_only:
            title.tracks.videos.clear()
            if audio_only:
                if not subs_only:
                    title.tracks.subtitles.clear()
                if not chapters_only:
                    title.tracks.chapters.clear()
            elif subs_only:
                if not audio_only:
                    title.tracks.audios.clear()
                if not chapters_only:
                    title.tracks.chapters.clear()
            elif chapters_only:
                if not audio_only:
                    title.tracks.audios.clear()
                if not subs_only:
                    title.tracks.subtitles.clear()

            if not mux:
                no_mux = True

        log.info("> Selected Tracks:")
        title.tracks.print()

        if json_entry is not None:
            track_export.mark_selected(json_entry, title)
            track_export.write_json(list_json, json_entries)

        if list_:
            continue  # only wanted to see what tracks were available and chosen

        skip_title = False
        is_dsnp_extra_item = (
            service_name == "DisneyPlus"
            and getattr(title, "season", None) == 0
        )
        # ParamountPlus, AppleTVPlus, Amazon and HBOMax reuse the exact same content key
        # across every audio track of a title, so once we've licensed one audio track we can
        # skip the vault lookups/CDM round trip for the rest and just reuse the resolved key.
        # The reuse below is still gated on the KID actually matching, so a title that turns
        # out to key its audio tracks separately licenses each one as normal.
        share_audio_keys = service_name in ("ParamountPlus", "AppleTVPlus", "Amazon", "HBOMax")
        resolved_audio_keys = {}  # kid -> key
        # Every key resolved for this title (video + audio), keyed by KID. Some services
        # (seen on Amazon) declare one KID in the manifest/PSSH for licensing but embed a
        # different KID in a given representation's actual segments, so at decrypt time we
        # pass every key we hold for the title to the decrypter as extra candidates - it only
        # gets used if its KID happens to match what's actually embedded in that file.
        all_resolved_keys = {}  # kid -> key
        # get keys
        for track in title.tracks:

            # Resolve HLS encryption before licensing rather than at download
            # time, where it is too late to get a key for a track the service
            # declared clear (Disney+ audio without -ae is the usual case).
            if not isinstance(track, TextTrack) and not track.encrypted:
                if track.probe_encryption(
                    headers=service.session.headers,
                    proxy=next(iter(service.session.proxies.values()), None)
                ):
                    log.info(f"{str(track).replace('├─ ', '')}")
                    log.info(" + Its HLS playlist is encrypted, licensing it as an encrypted track")

            if (
                share_audio_keys
                and isinstance(track, AudioTrack)
                and track.encrypted
                and (track.get_pr_pssh(service.session) if ctx.obj.cdm.device.type == LocalDevice.Types.PLAYREADY else track.get_pssh(service.session))
                and track.get_kid(service.session)
                and track.kid in resolved_audio_keys
            ):
                track.key = resolved_audio_keys[track.kid]
                log.info(f"Licensing: {str(track).replace('├─ ', '')}")
                log.info(f" + KID: {track.kid}")
                log.info(f" + KEY: {track.key} (Reused, same KID as another selected audio track)")
                continue

            if track.encrypted and not isinstance(track, TextTrack):
                log.info(f"Licensing: {str(track).replace('├─ ', '')}")


                if ctx.obj.cdm.device.type == LocalDevice.Types.PLAYREADY:
                    if not track.get_pr_pssh(service.session):
                        if is_dsnp_extra_item:
                            log.warning(" - Missing PR_PSSH on DisneyPlus extra item. Treating as unprotected and downloading without decryption...")
                            track.encrypted = False
                            track.pr_pssh = None
                            track.pssh = None
                            track.kid = None
                            track.key = None
                            continue
                        raise click.ClickException(" - Failed to get PR_PSSH")
                else:
                    if not track.get_pssh(service.session):
                        if is_dsnp_extra_item:
                            log.warning(" - Missing PSSH on DisneyPlus extra item. Treating as unprotected and downloading without decryption...")
                            track.encrypted = False
                            track.pr_pssh = None
                            track.pssh = None
                            track.kid = None
                            track.key = None
                            continue
                        raise click.ClickException(" - Failed to get PSSH")
                if ctx.obj.cdm.device.type == LocalDevice.Types.PLAYREADY: 
                    log.info(f" + PR_PSSH: {track.pr_pssh}")
                else:
                    log.info(f" + WV_PSSH: {base64.b64encode(Box.build(track.pssh)).decode()}")

                if not track.get_kid(service.session):
                    if is_dsnp_extra_item:
                        log.warning(" - Missing KID on DisneyPlus extra item. Treating as unprotected and downloading without decryption...")
                        track.encrypted = False
                        track.pr_pssh = None
                        track.pssh = None
                        track.kid = None
                        track.key = None
                        continue
                    raise click.ClickException(" - Failed to get KID")
                log.info(f" + KID: {track.kid}")
        
            if track.encrypted and not isinstance(track, TextTrack):
                                         
                if track.key:
                    log.info(f" + KEY: {track.key} (Static)")
                    if share_audio_keys and isinstance(track, AudioTrack):
                        resolved_audio_keys[track.kid] = track.key
                    all_resolved_keys[track.kid] = track.key
                elif not no_cache:
                    track.key, vault_used = ctx.obj.vaults.get(track.kid, title.id)
                    if track.key:
                        log.info(f" + KEY: {track.key} (From {vault_used.name} {vault_used.type.name} Key Vault)")
                        if share_audio_keys and isinstance(track, AudioTrack):
                            resolved_audio_keys[track.kid] = track.key
                        all_resolved_keys[track.kid] = track.key
                        for vault in ctx.obj.vaults.vaults:
                            if vault == vault_used:
                                continue

                            try:
                                result = ctx.obj.vaults.insert_key(
                                    vault, service_name.lower(), track.kid, track.key, title.id, commit=True
                                )
                                if result == InsertResult.SUCCESS:
                                    log.info(f" + Cached to {vault} vault")
                                elif result == InsertResult.ALREADY_EXISTS:
                                    log.info(f" + Already exists in {vault} vault")
                            except:
                                pass
                                                                                                 
                if not track.key:
                    if cache:
                        log.warning(f" - No cached key for {str(track).replace('├─ ', '')} (--cache), skipping title")
                        skip_title = True
                        break
                    
                    if ctx.obj.cdm.device.__class__.__name__=="PLAYREADY":
                        try:
                            session_id = ctx.obj.cdm.open()
                            wrm_header = PSSH_PR(track.pr_pssh).wrm_headers[0]
                            challenge = ctx.obj.cdm.get_license_challenge(session_id, wrm_header).encode()
                            license_res=service.license(
                                        challenge=challenge,
                                        title=title,
                                        track=track,
                                        session_id=session_id
                                    )
                            if isinstance(license_res, bytes):
                                if "<License>" in license_res.decode("utf-8", errors="replace"):
                                    license_res = base64.b64encode(license_res).decode()
                            # The license server's raw SOAP/XML response isn't guaranteed to be
                            # strict UTF-8 (e.g. Windows-1252/Latin-1 punctuation in a fault
                            # message) - replace undecodable bytes instead of crashing, since we
                            # only need the XML structure/text to parse, not byte-perfect content.
                            license_res=base64.b64decode(license_res).decode("utf-8", errors="replace")
                            ctx.obj.cdm.parse_license(session_id, license_res)
                            content_keys = [
                                (x.key_id.hex, x.key.hex()) for x in ctx.obj.cdm.get_keys(session_id)
                            ]
                            ctx.obj.cdm.close(session_id)
                        except Exception as e:
                            raise log.exit(f" - Error {e}")

                    else:
                        if ctx.obj.cdm.device.type == LocalDevice.Types.PLAYREADY:
                         
                                                                                  
                            session_id = ctx.obj.cdm.open(track.pr_pssh,raw=True)
                        else:
                            session_id = ctx.obj.cdm.open(track.pssh)
                        try:
                            ctx.obj.cdm.set_service_certificate(
                                session_id,
                                service.certificate(
                                    challenge=ctx.obj.cdm.service_certificate_challenge,
                                    title=title,
                                    track=track,
                                    session_id=session_id
                                ) or ctx.obj.cdm.common_privacy_cert
                            )

                            chal = ctx.obj.cdm.get_license_challenge(session_id, service_name)
                            content_keys = [
                                (x.kid.hex(), x.key.hex())
                                for x in ctx.obj.cdm.get_keys(session_id, content_only=True)
                            ]
                            if not content_keys:
                                ctx.obj.cdm.parse_license(
                                    session_id,
                                    service.license(
                                        challenge=chal,
                                        title=title,
                                        track=track,
                                        session_id=session_id,
                                        service_name=service_name
                                    )
                                )

                        except requests.HTTPError as e:
                            log.debug(traceback.format_exc())
                            raise log.exit(f" - HTTP Error {e.response.status_code}: {e.response.reason}")
                                                             
                                                                                                          
                        content_keys = [
                            (x.kid.hex(), x.key.hex()) for x in ctx.obj.cdm.get_keys(session_id, content_only=True)
                                    
                                                                                                   
                        ]
             
                    if not content_keys:
                        raise click.ClickException(" - No content keys were returned by the CDM!")
                    log.info(f" + Obtained content keys from the {ctx.obj.cdm.device.__class__.__name__} CDM")
                                                                                      
                    for kid, key in content_keys:
                        if kid == "b770d5b4bb6b594daf985845aae9aa5f":
                            # Amazon HDCP test key
                            continue
                        log.info(f" + {kid}:{key}")
                        all_resolved_keys[kid] = key

                    # cache keys into all key vaults
                    for vault in ctx.obj.vaults.vaults:
                        try:
                            log.info(f"Caching to {vault} vault")
                            cached = 0
                            already_exists = 0
                            for kid, key in content_keys:
                                result = ctx.obj.vaults.insert_key(vault, service_name.lower(), kid, key, title.id)
                                if result == InsertResult.FAILURE:
                                    log.warning(f" - Failed to cache {kid} to {vault} vault.")
                                elif result == InsertResult.SUCCESS:
                                    cached += 1
                                elif result == InsertResult.ALREADY_EXISTS:
                                    already_exists += 1

                            ctx.obj.vaults.commit(vault)
                            log.info(f" + Cached {cached}/{len(content_keys)} keys")
                            if already_exists:
                                log.info(f" + {already_exists}/{len(content_keys)} keys already existed in vault")
                            if cached + already_exists < len(content_keys):
                                log.warning(f"    Failed to cache {len(content_keys) - cached - already_exists} keys")
                        except:
                            pass
                    # use matching content key for the tracks key id
                    track.key = next((key for kid, key in content_keys if kid == track.kid), None)
                    if track.key:
                        log.info(f" + KEY: {track.key} (From CDM)")
                        if share_audio_keys and isinstance(track, AudioTrack):
                            resolved_audio_keys[track.kid] = track.key
                    else:
                        raise click.ClickException(f" - No content key with the key ID \"{track.kid}\" was returned")

        if json_entry is not None and keys:
            # Before the skip below deletes the tracks: which keys were found.
            json_entry["keys"] = track_export.keys_status(title, complete=not skip_title)
            track_export.write_json(list_json, json_entries)

        if skip_title:
            for track in title.tracks:
                track.delete()
            continue
        if keys:
            continue

        # Check if we're in hybrid mode
        is_hybrid_mode = range_ in ["HYBRID", "HYBRIDPLUS"] and len(title.tracks.videos) == 2
        is_hybridplus_mode = range_ == "HYBRIDPLUS"
        hybrid_processed = False
        
        service_name = service.__class__.__name__

        # Get concurrent downloads setting from config (per-service override, falls back to 'default')
        concurrent_downloads = get_service_setting('downloads', 'concurrent_tracks', service_name, 1)

        # Get downloader setting from config
        downloader_config = getattr(config, 'downloader', {}) if hasattr(config, 'downloader') else {}
        downloader = downloader_config.get(service_name) or downloader_config.get('default', 'aria2c')
        log.info(f"Using downloader: {downloader}")
        
        # download and decrypt
        def download_track(track, quiet=False, progress_callback=None):
            """Download a single track (used for parallel downloads)"""
            if not keys:
                if track.needs_proxy:
                    proxy = next(iter(service.session.proxies.values()), None)
                else:
                    proxy = None
                track.download(directories.temp, headers=service.session.headers, proxy=proxy, quiet=quiet, downloader=downloader, progress_callback=progress_callback)
            return track
        
        # Create Rich progress tree similar to unshackle
        track_list = list(title.tracks)

        # The progress tree is rendered by a Rich Live background thread while the
        # download workers feed it. Every crash_*.log collected so far has faulted
        # inside that render thread (rich/table.py:738, an access violation), on
        # runs as small as 16 tracks. Setting `downloads: progress_display: false`
        # in vinetrimmer.yml swaps the tree for plain log lines, which removes the
        # render thread entirely - both as a workaround and to establish whether
        # the display is actually involved in the crashes.
        progress_display = get_service_setting('downloads', 'progress_display', service_name, True)

        # The web UI (webui/queue_manager.py) runs every job with stdout piped, where
        # the Live tree can't redraw in place and shows nothing until the very end.
        # It sets VT_WEBUI_PROGRESS=1 so the tree is skipped here and per-track
        # progress is instead printed as periodic JSON snapshot lines, which the web
        # console strips out of the log and renders as its own live progress bars.
        webui_progress = os.environ.get("VT_WEBUI_PROGRESS") == "1" and not keys
        if webui_progress:
            progress_display = False
        webui_track_states = []
        webui_state_lock = threading.Lock()

        # Build track tree with progress bars
        tree = Tree("", hide_root=True)
        progress_callables = []
        progress_slots = []

        # Group tracks by type
        video_tracks = [t for t in track_list if isinstance(t, VideoTrack)]
        audio_tracks = [t for t in track_list if isinstance(t, AudioTrack)]
        text_tracks = [t for t in track_list if isinstance(t, TextTrack)]
        
        def add_tracks_to_tree(tracks, track_type_name):
            if not tracks:
                return
            
            num_tracks = len(tracks)
            track_type_plural = track_type_name + ("s" if num_tracks != 1 else "")
            tracks_tree = tree.add(f"[repr.number]{num_tracks}[/] {track_type_plural}")
            
            for track in tracks:
                # Create progress bar for each track
                progress = Progress(
                    SpinnerColumn(finished_text=""),
                    BarColumn(),
                    "•",
                    TimeRemainingColumn(compact=True, elapsed_when_finished=True),
                    "•",
                    TextColumn("[progress.data.speed]{task.fields[downloaded]}"),
                    console=console,
                    speed_estimate_period=10,
                )
                task = progress.add_task("", total=100, downloaded="-")

                # Download threads publish plain values into this slot and never
                # touch `progress` itself; apply_pending_progress() below feeds them
                # into Rich from the render thread. Rich's Progress guards its own
                # state with a lock, but the renderer reads task attributes outside
                # it, so a worker calling update() while the Live thread renders is
                # still concurrent access to the same objects - and all three
                # crash_*.log dumps faulted in that renderer. One thread owning
                # every Rich object removes the question entirely.
                slot = {
                    "lock": threading.Lock(),
                    "pending": {},
                }
                progress_slots.append((progress, task, slot))

                def make_progress_callback(slot):
                    def callback(**kwargs):
                        updates = {
                            key: kwargs[key]
                            for key in ("completed", "total", "advance", "downloaded")
                            if key in kwargs
                        }
                        if not updates:
                            return
                        with slot["lock"]:
                            # `advance` is cumulative, everything else is absolute.
                            advance = updates.pop("advance", None)
                            if advance is not None:
                                updates["advance"] = slot["pending"].get("advance", 0) + advance
                            slot["pending"].update(updates)

                    return callback

                progress_callables.append(make_progress_callback(slot))
                
                # Add track info to tree
                track_table = Table.grid()
                track_str = str(track)
                # Remove the leading number (e.g., "1. ") if present
                if track_str and track_str[0].isdigit() and ". " in track_str:
                    track_str = track_str.split(". ", 1)[1]
                track_table.add_row(rich_escape(track_str), style="dim")
                track_table.add_row(progress)
                tracks_tree.add(track_table)
        
        def make_log_progress_callback(track):
            """Stand-in for a progress bar: report each track's outcome as a log line."""
            reported = []

            track_type = "Video" if isinstance(track, VideoTrack) else "Audio" if isinstance(track, AudioTrack) else "Subtitle"
            track_str = str(track)
            if track_str and track_str[0].isdigit() and ". " in track_str:
                track_str = track_str.split(". ", 1)[1]
            track_str = track_str.lstrip("├└│─ ")
            # "_"-prefixed keys are internal timing data, not sent to the web UI.
            webui_state = {"type": track_type, "name": track_str, "completed": 0, "total": 100, "status": "Queued",
                           "_started": None, "_finished": None, "_samples": []}
            webui_track_states.append(webui_state)

            def update_webui_state(kwargs):
                with webui_state_lock:
                    if kwargs.get("total"):
                        webui_state["total"] = kwargs["total"]
                    if "completed" in kwargs:
                        webui_state["completed"] = kwargs["completed"]
                    if "advance" in kwargs:
                        webui_state["completed"] += kwargs["advance"]
                    now = time.time()
                    if webui_state["_started"] is None and ("completed" in kwargs or "advance" in kwargs):
                        webui_state["_started"] = now
                    if webui_state["_started"] is not None and webui_state["_finished"] is None:
                        samples = webui_state["_samples"]
                        if not samples or samples[-1][1] != webui_state["completed"]:
                            samples.append((now, webui_state["completed"]))
                            # Only the last minute matters for the recent rate.
                            while len(samples) > 2 and now - samples[0][0] > 60:
                                samples.pop(0)
                    if kwargs.get("downloaded") == "Downloaded" and webui_state["_finished"] is None:
                        webui_state["_finished"] = now
                    if "downloaded" in kwargs:
                        status = str(kwargs["downloaded"])
                        try:
                            status = Text.from_markup(status).plain
                        except Exception:
                            pass
                        webui_state["status"] = status

            def callback(**kwargs):
                update_webui_state(kwargs)
                state = kwargs.get("downloaded")
                if state not in ("Downloaded", "[red]FAILED") or reported:
                    return
                reported.append(state)
                if state == "Downloaded":
                    log.info(f" + Downloaded: {track_str}")
                else:
                    log.error(f" - Failed: {track_str}")

            return callback

        if progress_display:
            # Add all track types to tree
            add_tracks_to_tree(video_tracks, "Video")
            add_tracks_to_tree(audio_tracks, "Audio")
            add_tracks_to_tree(text_tracks, "Subtitle")
        else:
            # One callable per track, same ordering, so track indexes still line up.
            progress_callables.extend(make_log_progress_callback(t) for t in track_list)

        # Display tree
        download_table = Table.grid()
        download_table.add_row(tree)

        def apply_pending_progress():
            """Push values published by download threads into Rich. Render thread only."""
            for progress_obj, task_id, slot in progress_slots:
                with slot["lock"]:
                    if not slot["pending"]:
                        continue
                    updates = slot["pending"]
                    slot["pending"] = {}
                progress_obj.update(task_id, **updates)

        class RenderThreadOwnedProgress:
            """
            Wraps the progress tree so the pending updates are applied on whichever
            thread is about to render it, keeping every Rich object single-threaded.
            """

            def __init__(self, renderable):
                self.renderable = renderable

            def __rich_console__(self, console_, options):
                apply_pending_progress()
                yield self.renderable

        @contextlib.contextmanager
        def webui_progress_context():
            stop = threading.Event()
            last_payload = [None]

            def snapshot_track(state, now):
                out = {k: v for k, v in state.items() if not k.startswith("_")}
                started, finished = state["_started"], state["_finished"]
                if started is None:
                    return out
                out["elapsed"] = int((finished or now) - started)
                completed, total = state["completed"], state["total"] or 100
                if finished or not completed or completed >= total:
                    return out
                # Like Rich's TimeRemainingColumn: remaining / rate. Percent only moves
                # in whole steps (a 4K track can sit on one value for a while), so the
                # recent rate (last minute) is averaged with the whole-track rate to
                # keep the estimate from jumping around.
                overall = completed / max(now - started, 1e-6)
                samples = state["_samples"]
                recent = 0.0
                if samples and now - samples[0][0] >= 10:
                    recent = (completed - samples[0][1]) / (now - samples[0][0])
                rate = (overall + recent) / 2 if recent > 0 else overall
                if rate > 0:
                    out["eta"] = int((total - completed) / rate)
                return out

            def emit():
                with webui_state_lock:
                    now = time.time()
                    payload = json.dumps({"tracks": [snapshot_track(s, now) for s in webui_track_states]},
                                         ensure_ascii=False)
                if payload != last_payload[0]:
                    last_payload[0] = payload
                    # soft_wrap: the web UI parses this line whole, Rich must not wrap it.
                    console.print(WEBUI_PROGRESS_MARKER + payload, soft_wrap=True, markup=False,
                                  highlight=False, emoji=False)

            def loop():
                while not stop.wait(0.5):
                    emit()

            emit()
            thread = threading.Thread(target=loop, daemon=True)
            thread.start()
            try:
                yield
            finally:
                stop.set()
                thread.join()
                emit()

        def progress_display_context():
            if not progress_display:
                return webui_progress_context() if webui_progress else contextlib.nullcontext()
            return Live(
                RenderThreadOwnedProgress(Padding(download_table, (1, 5))),
                console=console,
                refresh_per_second=5,
            )

        # Subtitles get their own independent worker pool so they start
        # downloading immediately instead of waiting for a video/audio
        # worker to free up (subtitles are small/fast but there can be many).
        subtitle_workers = get_service_setting('downloads', 'concurrent_subtitle_downloads', service_name, 0)
        subtitle_workers = min(subtitle_workers, len(text_tracks)) if text_tracks else 0

        num_av = len(video_tracks) + len(audio_tracks)
        if subtitle_workers > 0:
            av_track_list = track_list[:num_av]
            sub_track_list = track_list[num_av:]
        else:
            # Feature disabled: fold subtitles back into the normal pool/order
            av_track_list = track_list
            sub_track_list = []

        def download_with_progress(track_idx, track, failed_ref):
            """Download track with progress callback"""
            if failed_ref[0]:
                return None

            try:
                progress_callback = progress_callables[track_idx]

                if not keys:
                    if track.needs_proxy:
                        proxy = next(iter(service.session.proxies.values()), None)
                    else:
                        proxy = None
                    track.download(
                        directories.temp,
                        headers=service.session.headers,
                        proxy=proxy,
                        quiet=True,
                        downloader=downloader,
                        progress_callback=progress_callback
                    )
                return track

            except Exception as e:
                progress_callables[track_idx](downloaded="[red]FAILED")
                failed_ref[0] = True
                log.error(f"Track download FAILED: {str(e)}")
                return None

        sub_executor = None
        sub_future_to_track = {}
        sub_failed = [False]
        sub_completed = 0

        def start_subtitle_pool():
            """Kick off the dedicated subtitle worker pool immediately."""
            nonlocal sub_executor, sub_future_to_track
            sub_executor = ThreadPoolExecutor(max_workers=subtitle_workers)
            for i, track in enumerate(sub_track_list):
                idx = num_av + i
                future = sub_executor.submit(download_with_progress, idx, track, sub_failed)
                sub_future_to_track[future] = (idx, track)

        def finish_subtitle_pool():
            """Wait for the subtitle pool to finish and report results."""
            nonlocal sub_completed
            if sub_executor is None:
                return
            for future in as_completed(sub_future_to_track):
                try:
                    result = future.result(timeout=1.0)
                    if result is not None:
                        sub_completed += 1
                except Exception as e:
                    sub_failed[0] = True
                    log.error(f"Subtitle download exception: {str(e)}")
            sub_executor.shutdown(wait=False, cancel_futures=True)
            if sub_failed[0]:
                log.error(f"✗ SUBTITLES: {sub_completed}/{len(sub_track_list)} completed before failure")
            elif sub_track_list:
                log.info(f"✓ SUBTITLES: all {len(sub_track_list)} completed")

        def cancel_subtitle_pool():
            sub_failed[0] = True
            if sub_executor is not None:
                for f in sub_future_to_track:
                    f.cancel()
                sub_executor.shutdown(wait=False, cancel_futures=True)

        # Download tracks (either sequential or parallel based on config)
        if concurrent_downloads > 1:
            log.info("=" * 80)
            log.info(f"PARALLEL DOWNLOAD MODE: {len(av_track_list)} tracks | {concurrent_downloads} concurrent")
            if subtitle_workers > 0:
                log.info(f"SUBTITLE POOL: {len(sub_track_list)} tracks | {subtitle_workers} concurrent")
            log.info("=" * 80)

            completed = 0
            failed_ref = [False]

            executor = ThreadPoolExecutor(max_workers=concurrent_downloads)
            future_to_track = {}

            try:
                # Use Rich Live for real-time progress updates
                with progress_display_context():
                    if subtitle_workers > 0:
                        start_subtitle_pool()

                    # Submit download tasks
                    for i, track in enumerate(av_track_list):
                        if failed_ref[0]:
                            break
                        future = executor.submit(download_with_progress, i, track, failed_ref)
                        future_to_track[future] = (i, track)

                    # Process completed downloads
                    for future in as_completed(future_to_track):
                        if failed_ref[0]:
                            # Cancel all remaining futures
                            for f in future_to_track:
                                if not f.done():
                                    f.cancel()
                            break

                        track_idx, track = future_to_track[future]
                        try:
                            result = future.result(timeout=1.0)
                            if result is not None and not failed_ref[0]:
                                completed += 1
                            elif result is None:
                                # Download failed
                                failed_ref[0] = True
                                break
                        except KeyboardInterrupt:
                            failed_ref[0] = True
                            raise
                        except Exception as e:
                            failed_ref[0] = True
                            log.error(f"Download exception: {str(e)}")
                            break

                    if failed_ref[0]:
                        cancel_subtitle_pool()
                    else:
                        finish_subtitle_pool()

            except KeyboardInterrupt:
                failed_ref[0] = True
                console.print("\n")
                log.warning("=" * 80)
                log.warning("⚠️  DOWNLOAD CANCELLED - Stopping all downloads...")
                log.warning("=" * 80)
                # Cancel all pending futures
                for future in future_to_track:
                    future.cancel()
                log.warning(f"Stopped at {completed}/{len(av_track_list)} tracks completed")
                # Force shutdown
                executor.shutdown(wait=False, cancel_futures=True)
                cancel_subtitle_pool()
                # Cleanup and exit
                for track in title.tracks:
                    track.delete()
                return
            finally:
                # Always shutdown executor
                executor.shutdown(wait=False, cancel_futures=True)

            # Check if we completed successfully or had failures
            failed = failed_ref[0] or sub_failed[0]
            if failed:
                console.print("\n")
                log.error("=" * 80)
                log.error(f"✗ DOWNLOAD FAILED: {completed}/{len(av_track_list)} tracks completed before failure")
                log.error("=" * 80)
                # Cleanup
                for track in title.tracks:
                    track.delete()
                # Nonzero, so callers (the web UI's queue, scripts) see the failure.
                sys.exit(1)
            else:
                console.print("\n")
                log.info("=" * 80)
                log.info(f"✓ ALL COMPLETE: {len(track_list)} tracks downloaded successfully")
                log.info("=" * 80)
        else:
            # Sequential download with Rich Live progress (subtitles still run in their own pool)
            log.info("=" * 80)
            log.info(f"SEQUENTIAL DOWNLOAD MODE: {len(av_track_list)} tracks")
            if subtitle_workers > 0:
                log.info(f"SUBTITLE POOL: {len(sub_track_list)} tracks | {subtitle_workers} concurrent")
            log.info("=" * 80)

            failed_ref = [False]
            with progress_display_context():
                if subtitle_workers > 0:
                    start_subtitle_pool()

                for i, track in enumerate(av_track_list):
                    progress_callback = progress_callables[i]
                    try:
                        if not keys:
                            if track.needs_proxy:
                                proxy = next(iter(service.session.proxies.values()), None)
                            else:
                                proxy = None
                            track.download(
                                directories.temp,
                                headers=service.session.headers,
                                proxy=proxy,
                                quiet=True,
                                downloader=downloader,
                                progress_callback=progress_callback
                            )
                    except Exception as e:
                        progress_callback(downloaded="[red]FAILED")
                        log.error(f"Track download failed: {str(e)}")
                        cancel_subtitle_pool()
                        raise

                finish_subtitle_pool()

            console.print("\n")
            if sub_failed[0]:
                log.error("=" * 80)
                log.error("✗ SUBTITLE DOWNLOAD FAILED")
                log.error("=" * 80)
                for track in title.tracks:
                    track.delete()
                sys.exit(1)
            log.info("=" * 80)
            log.info(f"✓ ALL COMPLETE: {len(track_list)} tracks downloaded successfully")
            log.info("=" * 80)
        
        # Process post-download tasks (ccextractor, decryption, etc.)
        for track in title.tracks:
            if isinstance(track, VideoTrack) and track.needs_ccextractor_first and not no_subs:
                ccextractor()
            # Never decrypt subtitle tracks in hybrid flows.
            if track.encrypted and not isinstance(track, TextTrack):
                # The manifest is not the last word on whether the segments came
                # down protected: a stream advertising ContentProtection can be
                # served in the clear, and running a decrypter over it would
                # fail on a track that has no KID to match against anyway.
                if track.file_is_encrypted() is False:
                    log.info("Skipping decryption, the stream was delivered unencrypted")
                    track.encrypted = False
                elif not track.kid or not track.key:
                    raise click.ClickException(
                        f" - {str(track).replace('├─ ', '')} is encrypted but has no "
                        f"{'KID' if not track.kid else 'key'}, so it cannot be decrypted. "
                        "It was not licensed, which on Disney+ means the audio needs -ae."
                    )

            if track.encrypted and not isinstance(track, TextTrack):
                log.info("Decrypting...")
                if track.key:
                    log.info(f" + KEY: {track.key} (Static)")
                # elif not no_cache: # keys are already in track.key, so no need
                #     track.key, vault_used = ctx.obj.vaults.get(track.kid, title.id)
                #     if track.key:
                #         log.info(f" + KEY: {track.key} (From {vault_used.name} {vault_used.type.name} Key Vault)")
                #         for vault in ctx.obj.vaults.vaults:
                #             if vault == vault_used:
                #                 continue
                #             result = ctx.obj.vaults.insert_key(
                #                 vault, service_name.lower(), track.kid, track.key, title.id, commit=True
                #             )
                #             if result == InsertResult.SUCCESS:
                #                 log.info(f" + Cached to {vault} vault")
                #             elif result == InsertResult.ALREADY_EXISTS:
                #                 log.info(f" + Already exists in {vault} vault")
                # TODO: Move decryption code to Track
                if not config.decrypter:
                    raise click.ClickException(" - No decrypter specified")
                if track.smooth:
                    config.decrypter = "mp4decrypt"
                if config.decrypter == "packager":
                    platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)
                    names = ["shaka-packager", "packager", f"packager-{platform}"]
                    executable = next((x for x in (shutil.which(x) for x in names) if x), None)
                    if not executable:
                        raise click.ClickException(" - Unable to find packager binary")
                    dec = os.path.splitext(track.locate())[0] + ".dec.mp4"
                    # Some services (seen on Amazon) declare one KID for licensing but embed a
                    # different one in a given representation's actual segments. Offer every
                    # other key we hold for this title as extra label candidates so the
                    # decrypter can still find a match even if the embedded KID differs from
                    # track.kid - a label is only used if its key_id matches the file's tenc box.
                    extra_keys = {
                        kid: key for kid, key in all_resolved_keys.items() if kid != track.kid
                    }
                    try:
                        os.makedirs(directories.temp, exist_ok=True)
                        key_labels = [
                            f"label=0:key_id={track.kid.lower()}:key={track.key.lower()}",
                            # Apple TV+ needs this as shaka pulls the incorrect KID, idk why
                            f"label=1:key_id=00000000000000000000000000000000:key={track.key.lower()}",
                        ] + [
                            f"label={i}:key_id={kid.lower()}:key={key.lower()}"
                            for i, (kid, key) in enumerate(extra_keys.items(), start=2)
                        ]
                        subprocess.run([
                            executable,
                            "input={},stream={},output={}".format(
                                track.locate(),
                                track.__class__.__name__.lower().replace("track", ""),
                                dec
                            ),
                            "--enable_raw_key_decryption", "--keys",
                            ",".join(key_labels),
                            "--temp_dir", directories.temp
                        ], check=True)
                    except subprocess.CalledProcessError:
                        raise click.ClickException(" - Failed!")
                elif config.decrypter == "mp4decrypt":
                    executable = shutil.which("mp4decrypt")
                    if not executable:
                        raise click.ClickException(" - Unable to find mp4decrypt binary")
                    dec = os.path.splitext(track.locate())[0] + ".dec.mp4"
                    extra_keys = {
                        kid: key for kid, key in all_resolved_keys.items() if kid != track.kid
                    }
                    key_args = []
                    for kid, key in [(track.kid, track.key), *extra_keys.items()]:
                        key_args += ["--key", f"{kid.lower()}:{key.lower()}"]
                    try:
                        subprocess.run([
                            executable,
                            "--show-progress",
                            *key_args,
                            track.locate(),
                            dec,
                        ])
                    except subprocess.CalledProcessError:
                        raise click.ClickException(" - Failed!")
                else:
                    raise click.ClickException(f" - Unsupported decrypter: {config.decrypter}")
                track.swap(dec)
                log.info(" + Decrypted")
  

            if track.needs_repack or (config.decrypter == "mp4decrypt" and isinstance(track, (VideoTrack, AudioTrack))):
                log.info("Repackaging stream with FFmpeg (to fix malformed streams)")
                track.repackage()
                log.info(" + Repackaged")

            if isinstance(track, VideoTrack) and track.needs_ccextractor and not no_subs:
                ccextractor()

        # Process hybrid mode after all tracks are downloaded and decrypted
        if is_hybrid_mode and not hybrid_processed:
            if is_hybridplus_mode:
                log.info("=" * 60)
                log.info("Processing Hybrid AV1 HDR10+ to DV Mode (Netflix Special)")
                log.info("=" * 60)
            else:
                log.info("=" * 60)
                log.info("Processing Hybrid HDR10+DV Mode")
                log.info("=" * 60)
            
            # Import hybrid module
            from vinetrimmer.utils.hybrid import Hybrid
            
            # For HYBRIDPLUS, identify AV1 HDR10+ and H.265 DV tracks (Netflix special case)
            if is_hybridplus_mode:
                # Check if this is Netflix service
                if service_name.lower() != "netflix":
                    log.warning("HYBRIDPLUS mode is only supported for Netflix")
                    log.warning("Falling back to regular HYBRID mode")
                    is_hybridplus_mode = False
                    hdr10_track = next((v for v in title.tracks.videos if v.hdr10), None)
                    dv_track = next((v for v in title.tracks.videos if v.dv), None)
                else:
                    # For Netflix HYBRIDPLUS: Find AV1 HDR10+ track (identified as HDR10 with AV1 codec)
                    hdr10_track = next((v for v in title.tracks.videos 
                                       if 'av01' in (v.codec or '').lower() or 'av1' in (v.codec or '').lower()), None)
                    # Find H.265 DV track
                    dv_track = next((v for v in title.tracks.videos 
                                    if v.dv and ('hev' in (v.codec or '').lower() or 'h265' in (v.codec or '').lower())), None)
                    
                    if hdr10_track and dv_track:
                        log.info(f" + Found AV1 HDR10+ track: {hdr10_track.codec}")
                        log.info(f" + Found H.265 DV track: {dv_track.codec}")
                    else:
                        log.warning(f" - Could not find required tracks for HYBRIDPLUS mode")
                        if not hdr10_track:
                            log.warning("   Missing: AV1 HDR10+ track")
                        if not dv_track:
                            log.warning("   Missing: H.265 DV track")
            else:
                # For regular HYBRID, identify HDR10 and DV tracks
                hdr10_track = next((v for v in title.tracks.videos if v.hdr10), None)
                dv_track = next((v for v in title.tracks.videos if v.dv), None)
            
            if not hdr10_track or not dv_track:
                log.error(" - Hybrid mode requires both tracks")
            else:
                try:
                    primary_audio_track = title.tracks.audios[0] if title.tracks.audios else None
                    # Process hybrid tracks
                    hybrid = Hybrid(
                        hdr10_track=hdr10_track,
                        dv_track=dv_track,
                        temp_dir=directories.temp,
                        service_name=service_name,
                        audio_track=primary_audio_track
                    )
                    
                    # Get the hybrid output file
                    hybrid_output = hybrid.get_output_path()
                    
                    if hybrid_output.exists():
                        log.info(f"✓ Hybrid processing complete")
                        
                        # Store original track locations for cleanup
                        original_hdr10_location = hdr10_track._location
                        original_dv_location = dv_track._location
                        
                        # Replace HDR10 track with hybrid output
                        # Update the track location to point to the hybrid file
                        hdr10_track._location = str(hybrid_output)
                        
                        # Remove DV track from the list since it's now merged
                        title.tracks.videos = [hdr10_track]
                        
                        # Mark track as DV since it now contains DV metadata
                        hdr10_track.dv = True
                        hdr10_track.dvhdr = True  # Indicates it's a hybrid DV+HDR track
                        hdr10_track.needs_duration_fix = True
                        
                        log.info(f" + HDR10 track replaced with hybrid HDR10+DV track")
                        
                        # Clean up original track files and any intermediate files
                        subtitle_exts = {".srt", ".vtt", ".ass", ".ssa", ".ttml", ".dfxp", ".xml", ".sub"}

                        def is_subtitle_path(path):
                            return os.path.splitext(str(path))[1].lower() in subtitle_exts

                        files_to_clean = set()  # Use set to avoid duplicates
                        
                        # Original HDR10 track
                        if original_hdr10_location and os.path.exists(original_hdr10_location) and not is_subtitle_path(original_hdr10_location):
                            files_to_clean.add(original_hdr10_location)
                        
                        # Original DV track
                        if original_dv_location and os.path.exists(original_dv_location) and not is_subtitle_path(original_dv_location):
                            files_to_clean.add(original_dv_location)
                        
                        # Check for various intermediate files patterns
                        for location in [original_hdr10_location, original_dv_location]:
                            if not location:
                                continue
                            
                            # Pattern 1: filename_fixed
                            fixed_file = location + "_fixed"
                            if os.path.exists(fixed_file) and not is_subtitle_path(fixed_file):
                                files_to_clean.add(fixed_file)
                            
                            # Pattern 2: filename.ext_fixed
                            if os.path.exists(location):
                                base_dir = os.path.dirname(location)
                                base_name = os.path.basename(location)
                                
                                # Look for _fixed variants with different extensions
                                for ext in ['.mp4', '.mkv', '.hevc', '']:
                                    variant = os.path.join(base_dir, base_name + "_fixed" + ext)
                                    if os.path.exists(variant) and not is_subtitle_path(variant):
                                        files_to_clean.add(variant)
                        
                        # Also scan temp directory for any orphaned hybrid-related files
                        try:
                            temp_path = Path(directories.temp)
                            if temp_path.exists():
                                # Look for track-specific files that might be left behind
                                for pattern in ["*_enc.mp4_fixed*", "*_fixed.mkv", "*_fixed.mp4"]:
                                    for leftover in temp_path.glob(pattern):
                                        if leftover.stat().st_size == 0 and not is_subtitle_path(leftover):  # Only clean 0-byte files
                                            files_to_clean.add(str(leftover))
                        except Exception:
                            pass
                        
                        # Delete all temporary files
                        for file_path in files_to_clean:
                            if is_subtitle_path(file_path):
                                continue
                            try:
                                os.unlink(file_path)
                                log.info(f" + Cleaned up: {os.path.basename(file_path)}")
                            except Exception as e:
                                log.warning(f" - Failed to clean up {os.path.basename(file_path)}: {e}")
                        
                        hybrid_processed = True
                    else:
                        log.error(" - Hybrid output file not found")
                        
                except Exception as e:
                    log.error(f" - Hybrid processing failed: {e}")
                    log.warning(" - Continuing with original HDR10 track only")
                    # Keep only HDR10 track on failure
                    title.tracks.videos = [hdr10_track]
                    
        elif is_hybrid_mode and hybrid_processed:
            log.info("✓ Hybrid mode tracks already processed")

                    
        if not list(title.tracks) and not title.tracks.chapters:
            continue
        # mux all final tracks to a single mkv file
        if no_mux:
            if title.tracks.chapters:
                final_file_path = directories.downloads
                if title.type == Title.Types.TV:
                    final_file_path = os.path.join(final_file_path, title.show_folder(), title.season_folder())
                elif title.type == Title.Types.MOVIE:
                    final_file_path = os.path.join(final_file_path, title.movie_folder())
                    if title.is_extra:
                        final_file_path = os.path.join(final_file_path, title.extra_folder)
                os.makedirs(final_file_path, exist_ok=True)
                chapters_loc = filenames.chapters.format(filename=title.filename)
                title.tracks.export_chapters(chapters_loc)
                shutil.move(chapters_loc, os.path.join(final_file_path, os.path.basename(chapters_loc)))
            for track in title.tracks:
                media_info = MediaInfo.parse(track.locate())
                final_file_path = directories.downloads
                if title.type == Title.Types.TV:
                    final_file_path = os.path.join(
                        final_file_path, title.show_folder(), title.season_folder()
                    )
                elif title.type == Title.Types.MOVIE:
                    final_file_path = os.path.join(final_file_path, title.movie_folder())
                    if title.is_extra:
                        final_file_path = os.path.join(final_file_path, title.extra_folder)
                os.makedirs(final_file_path, exist_ok=True)
                filename = title.parse_filename(media_info=media_info)
                if isinstance(track, (AudioTrack, TextTrack)):
                    filename += f".{track.language}"
                extension = track.codec if isinstance(track, TextTrack) else os.path.splitext(track.locate())[1][1:]
                if isinstance(track, AudioTrack) and extension == "mp4":
                    extension = "m4a"
                track.move(os.path.join(final_file_path, f"{filename}.{track.id}.{extension}"))
        else:
            log.info("Muxing tracks into an MKV container")
            muxed_location, returncode = title.tracks.mux(title.filename)
            if returncode == 1:
                log.warning(" - mkvmerge had at least one warning, will continue anyway...")
            elif returncode >= 2:
                raise log.exit(" - Failed to mux tracks into MKV file")
            log.info(" + Muxed")
            
            # Apply tag to file metadata if configured
            if hasattr(config, 'tag') and config.tag:
                apply_tag_to_file(muxed_location, config.tag)
            
            for track in title.tracks:
                track.delete()
            if title.tracks.chapters:
                try:
                    os.unlink(filenames.chapters.format(filename=title.filename))
                except FileNotFoundError:
                    pass
            media_info = MediaInfo.parse(muxed_location)
            final_file_path = directories.downloads
            if title.type == Title.Types.TV:
                final_file_path = os.path.join(
                    final_file_path, title.show_folder(), title.season_folder()
                )
            elif title.type == Title.Types.MOVIE:
                final_file_path = os.path.join(final_file_path, title.movie_folder())
                if title.is_extra:
                    final_file_path = os.path.join(final_file_path, title.extra_folder)
            os.makedirs(final_file_path, exist_ok=True)
            # rename muxed mkv file with new data from mediainfo data of it
            if audio_only:
                extension = "mka"
            elif subs_only:
                extension = "mks"
            else:
                extension = "mkv"
            shutil.move(
                muxed_location,
                os.path.join(final_file_path, f"{title.parse_filename(media_info=media_info)}.{extension}")
            )


    if stopped_early:
        sys.exit(STOP_EXIT_CODE)
    log.info("Processed all titles!")


def load_services():
    for service in services.__dict__.values():
        if callable(getattr(service, "cli", None)):
            dl.add_command(service.cli)


load_services()