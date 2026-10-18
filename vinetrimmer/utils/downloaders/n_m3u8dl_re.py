import os
import re
import shutil
import logging
import subprocess
from pathlib import Path
from itertools import chain
from urllib.parse import urlparse, urlunparse

from vinetrimmer.utils.downloaders.aria2c import aria2c
from vinetrimmer.utils.collections import as_list

AUDIO_CODEC_MAP = {"AAC": "mp4a", "aac": "mp4a", "AC3": "ac-3", "EC3": "ec-3", "mp4a": "mp4a", "ec-3": "ec-3", "ac-3": "ac-3"}
VIDEO_CODEC_MAP = {"H.264": "avc1", "avc1": "avc1", "hev1": "hev1", "hvc1": "hvc1", "H.265": "hvc1", "AV1": "av01", "av1": "av01", "av01": "av01", "dvhe": "dvh", "HLG": "hev"}
SUBTITLES_CODEC_MAP = [".srt", ".txt", ".ttml", ".ttml2", ".dfxp", ".xml", ".ssa", ".ass", ".sbv", ".vtt", ".lrc", ".idx", ".sub", ".usf", ".rt", ".smi", ".sami", ".mpl2", ".pjs", ".psb", ".dks", ".stl"]


def track_selection(track, source):
    adaptation_set = track.extra[1]
    representation = track.extra[0]

    track_type = track.__class__.__name__
    codec = track.codec
    bitrate = track.bitrate // 1000 if track_type != "TextTrack" else None
    
    width = track.width if track_type == "VideoTrack" else None
    if track_type == "VideoTrack":
        range = "HDR" if track.hdr10 else "HLG" if track.hlg else "DV" if track.dv else "SDR"
    else:
        range = None
    
    global langs
    langs = None

    if track_type == "AudioTrack":
        codecs = AUDIO_CODEC_MAP.get(codec)
        langs = adaptation_set.get('lang')
        track_ids = list(set(
            v for x in chain(adaptation_set, representation)
            for v in (x.get("audioTrackId"), x.get("id"))
            if v is not None
        ))
        roles = adaptation_set.findall("Role") + representation.findall("Role")
        main_role = next((i for i in roles if i.get("value", "").lower() == "main"), None)
        role = ":role=main" if main_role is not None else ""
        bandwidth = f"bwMin={bitrate - 1}:bwMax={bitrate + 1}"
        
        if langs is not None:
            track_selection = ["-sa", f"lang={langs}:codecs={codecs}:{bandwidth}{role}"]
        elif len(track_ids) == 1:
            track_selection = ["-sa", f"id={track_ids[0]}"]
        else:
            track_selection = ["-sa", f"for=best{role}"]
        return track_selection

    if track_type == "VideoTrack":
        # adjust codec based on range
        codec_adjustments = {
            ("HEVC1", "DV"): "DV",
            ("HEVC1", "HDR"): "HDR",
            ("HEVC1", "HLG"): "HLG",
            ("SDR"): "SDR"
        }
        codec = codec_adjustments.get((codec.upper(), range.upper()), codec)
        
        codecs = VIDEO_CODEC_MAP.get(codec)

        langs = adaptation_set.get('lang')
        
        bandwidth = f"bwMin={int(bitrate - 1)}:bwMax={int(bitrate + 1)}"
        track_selection = ["-sv", f"codecs={codecs}:{bandwidth}"]
        return track_selection
    
    if track_type == "TextTrack":
        langs = adaptation_set.get('lang')
        if langs is not None:
            track_selection = ["-ss", f"lang={langs}:for=all"]
        else:
            track_selection = ["-ss", "all"]
        track_selection.extend(["--sub-format", "SRT"])
        return track_selection


def track_selection_ism(track, source=None):
    track_type = track.__class__.__name__
    codec = track.codec
    bitrate = track.bitrate // 1000 if track_type != "TextTrack" else None
    
    width = track.width if track_type == "VideoTrack" else None
    if track_type == "VideoTrack":
        range = "HDR" if track.hdr10 else "HLG" if track.hlg else "DV" if track.dv else "SDR"
    else:
        range = None
    
    global langs
    langs = None

    if track_type == "AudioTrack":
        codecs = AUDIO_CODEC_MAP.get(codec)
        langs = track.extra[1]
        
        # For Amazon, use simpler selection without strict bandwidth matching
        if source == "AMZN":
            if langs is not None:
                track_selection = ["-sa", f"lang={langs}"]
            else:
                track_selection = ["-sa", "best"]
        else:
            bandwidth = f"bwMin={bitrate - 1}:bwMax={bitrate + 1}"
            if langs is not None:
                track_selection = ["-sa", f"lang={langs}:codecs={codecs}:{bandwidth}"]
            else:
                track_selection = ["-sa", "all"]
        return track_selection

    if track_type == "VideoTrack":
        # For Amazon, use simpler selection like the old code did
        if source == "AMZN":
            track_selection = ["-sv", "best"]
        else:
            # adjust codec based on range
            codec_adjustments = {
                ("HEVC", "DV"): "DV",
                ("HEVC", "HLG"): "HLG",
                ("SDR"): "SDR"
            }
            codec = codec_adjustments.get((codec, range), codec)
            codecs = VIDEO_CODEC_MAP.get(codec)
            
            bandwidth = f"bwMin={int(bitrate - 1)}:bwMax={int(bitrate + 1)}"
            track_selection = ["-sv", f"codecs={codecs}:{bandwidth}"]
        return track_selection
    
    if track_type == "TextTrack":
        langs = str(track.extra[1])
        if langs is not None:
            track_selection = ["-ss", f"lang={langs}:for=all"]
        else:
            track_selection = ["-ss", "all"]
        track_selection.extend(["--sub-format", "SRT"])
        return track_selection


def n_m3u8dl(uri, out, track, source, headers=None, proxy=None, quiet=False, progress_callback=None):
    from urllib.parse import urlparse, urlunparse
    
    log = logging.getLogger("N_m3u8DL-RE")
    executable = shutil.which("m3u8re") or shutil.which("N_m3u8DL-RE")
    ffmpeg_executable = shutil.which("ffmpeg")
    if not executable:
        raise EnvironmentError("N_m3u8DL-RE executable not found...")
    if not ffmpeg_executable:
        raise EnvironmentError("Ffmpeg executable not found...")
    
    out = Path(out)

    if headers:
        headers.update({k: v for k, v in headers.items() if k.lower() != "accept-encoding"})
    
    # For subtitles with URL descriptor, use aria2c directly (more reliable than N_m3u8DL-RE)
    if track.descriptor == track.Descriptor.URL and track.__class__.__name__ == "TextTrack":
        aria2c(uri, out, headers, proxy, quiet)
        return
    
    # For URL descriptor with list of segments, fall back to aria2c
        # For URL descriptor with list of segments, fall back to aria2c
        if isinstance(uri, list):
            log.warning("N_m3u8DL-RE does not support multiple URI segments, falling back to aria2c...")
            aria2c(
                uri,
                out,
                headers,
                proxy,
                quiet
            )
            return
        else:
            raise ValueError(f"Unsupported Track")

    # For ALL tracks (not just ISM), check if URL is a direct file first
    # This catches cases where tracks have ISM descriptor but direct file URLs
    url_to_check = None
    if isinstance(uri, list) and len(uri) > 0:
        url_to_check = uri[0]
    elif isinstance(uri, str):
        url_to_check = uri
    
    if url_to_check:
        # Parse URL to get just the path without query parameters
        parsed = urlparse(url_to_check)
        path = parsed.path
        # If it's a direct media file (regardless of descriptor), use aria2c
        if re.search(r'\.(mp4|m4a|m4v)$', path, re.IGNORECASE):
            aria2c(uri, out, headers, proxy, quiet)
            return

    # Special handling for ISM tracks with segment lists (e.g., Amazon)
    # Extract the manifest URL from the segment URLs
    if isinstance(uri, list) and track.descriptor == track.Descriptor.ISM:
        if source == "AMZN" and len(uri) > 0 and 'ism/' in uri[0]:
            # Extract manifest URL from segment URL
            pattern = r"(https?://.*?\.ism/)"
            match = re.search(pattern, uri[0])
            if match:
                # Replace uri with the manifest URL
                uri = match.group(1) + "Manifest"
            else:
                # If we can't extract manifest, fall back to aria2c
                log.warning("Could not extract ISM manifest URL, falling back to aria2c...")
                aria2c(uri, out, headers, proxy, quiet)
                return
        else:
            # For non-Amazon ISM with segment lists, fall back to aria2c
            log.warning("N_m3u8DL-RE does not support segmented ISM downloads, falling back to aria2c...")
            aria2c(uri, out, headers, proxy, quiet)
            return
    
    # After ISM extraction, uri should be a single manifest URL string
    # Update url variable for use in arguments
    if track.descriptor == track.Descriptor.M3U and isinstance(uri, list):
        url = uri[0]
    elif isinstance(uri, list):
        # This shouldn't happen now, but just in case
        url = uri[0]
    else:
        url = uri
    
    # For MPD with list of segments (not manifest URLs), fall back to aria2c
    if isinstance(uri, list) and track.descriptor == track.Descriptor.MPD:
        log.warning("N_m3u8DL-RE does not support segmented MPD downloads, falling back to aria2c...")
        aria2c(uri, out, headers, proxy, quiet)
        return
    
    # All list cases should be handled above, so we can proceed with the extracted manifest URL
    output_dir = str(out.parent)
    filename = out.name.replace('.mp4','')
    
    # Create unique temp directory for each track to avoid conflicts during concurrent downloads
    import tempfile
    unique_tmp_dir = tempfile.mkdtemp(prefix=f"n_m3u8dl_{filename}_", dir=output_dir)

    arguments = [
        executable,
        str(url),
        "--save-dir", output_dir,
        "--tmp-dir", unique_tmp_dir,
        "--save-name", filename,
        "--ffmpeg-binary-path", ffmpeg_executable,
        "--log-level", "ERROR",
        "--no-log",
        "--no-ansi-color",  # Disable ANSI colors to make parsing easier
    ]

    if proxy is not None:
        arguments.extend(["--custom-proxy", proxy])

    if headers is not None:
        header_args = [arg for k, v in headers.items() for arg in ("-H", f"{k}: {v}")]
        arguments.extend(header_args)

    if track.descriptor == track.Descriptor.MPD:
        selected_track = track_selection(track, source)
        arguments.extend(selected_track)
    
    if track.descriptor == track.Descriptor.ISM:
        selected_track = track_selection_ism(track, source)
        arguments.extend(selected_track)
        # For audio tracks, explicitly disable video download
        if track.__class__.__name__ == "AudioTrack":
            arguments.extend(["--no-video"])
        # For video tracks, explicitly disable audio download
        elif track.__class__.__name__ == "VideoTrack":
            arguments.extend(["--no-audio"])
        
    try:
        if progress_callback:
            # Capture output to parse progress (N_m3u8DL-RE outputs progress info)
            import threading
            
            # Send initial total like unshackle does
            progress_callback(total=100, downloaded="Starting...")
            
            # Suppress stderr to prevent manifest URLs and track lists from appearing
            # Only capture stdout for progress parsing
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                bufsize=1,  # Line buffered
                encoding="utf-8",  # N_m3u8DL-RE writes UTF-8; the OS locale codec (e.g. cp1252 on
                errors="replace"   # Windows) can't decode it and silently kills the reader thread
            )
            
            last_percentage = 0
            
            def read_progress():
                nonlocal last_percentage
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break
                    
                    # N_m3u8DL-RE outputs progress like:
                    # "Vid ... 1/64 1.56% 5.07MB/324.23MB 56.25KBps 00:17:39"
                    percent_matches = re.findall(r'(\d+(?:\.\d+)?)%', line)
                    speed_matches = re.findall(r'(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|KB|MB|GB)ps)', line)
                    size_matches = re.findall(r'(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|KB|MB|GB)\s*/\s*\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|KB|MB|GB))', line)
                    
                    if percent_matches:
                        # Get the last percentage in the line (most recent)
                        percentage = float(percent_matches[-1])
                        if percentage != last_percentage:
                            last_percentage = percentage
                            try:
                                # Build status update like unshackle
                                status_update = {"completed": int(percentage)}
                                
                                # Add speed and size info if available
                                if speed_matches and size_matches:
                                    status_update["downloaded"] = f"{speed_matches[-1]} {size_matches[-1]}"
                                elif speed_matches:
                                    status_update["downloaded"] = speed_matches[-1]
                                elif size_matches:
                                    status_update["downloaded"] = size_matches[-1]
                                else:
                                    status_update["downloaded"] = f"{percentage:.1f}%"
                                
                                progress_callback(**status_update)
                            except Exception:
                                pass
            
            reader_thread = threading.Thread(target=read_progress, daemon=True)
            reader_thread.start()
            
            process.wait()
            reader_thread.join(timeout=2)
            
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, arguments)
            progress_callback(completed=100, downloaded="Downloaded")
        else:
            # Run N_m3u8DL-RE with output completely suppressed for non-progress tracks
            # Suppress both stdout and stderr to prevent manifest URLs and other output
            subprocess.run(arguments, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        # Clean up temp directory on error
        if os.path.exists(unique_tmp_dir):
            shutil.rmtree(unique_tmp_dir, ignore_errors=True)
        if os.path.isfile(url):
            os.unlink(url)
        
        if not (track.descriptor == track.Descriptor.URL and track.__class__.__name__ == "TextTrack"):
            raise ValueError("N_m3u8DL-RE failed too many times, aborting")
        else:
            if track.descriptor == track.Descriptor.URL and track.__class__.__name__ == "TextTrack":
                if not os.path.exists(out) or os.stat(out).st_size <= 3:  # Empty UTF-8 BOM == 3 bytes
                    log.warning("Download failed, retrying with aria2c.")
                    # Only clean up the specific subtitle file and its temp dir, not the entire output_dir
                    if os.path.exists(out):
                        os.unlink(out)
                    if os.path.exists(unique_tmp_dir):
                        shutil.rmtree(unique_tmp_dir, ignore_errors=True)
                    import asyncio
                    aria2c(
                        uri,
                        out,
                        headers,
                        proxy,
                        quiet
                    )
    finally:
        # Always clean up unique temp directory after download completes
        if os.path.exists(unique_tmp_dir):
            shutil.rmtree(unique_tmp_dir, ignore_errors=True)
    
    # os.replace, not os.rename: on Windows renaming onto an existing file raises
    # WinError 183, so a leftover file from an earlier run would fail the track.
    if track.descriptor == track.Descriptor.MPD or track.descriptor == track.Descriptor.ISM:
        for file in os.listdir(output_dir):
            if langs:
                if file == f"{filename}.{langs}.mp4":
                    audiopath = os.path.join(output_dir, file)
                    os.replace(audiopath, audiopath.replace(f".{langs}", ""))
                if file == f"{filename}.{langs}.m4a":
                    audiopath = os.path.join(output_dir, file)
                    os.replace(audiopath, audiopath.replace(f".{langs}", ""))
                if file == f"{filename}.{langs}.srt":
                    textpath = os.path.join(output_dir, file)
                    os.replace(textpath, textpath.replace(f".{langs}", ""))
                if file == f"{filename}.{langs}.vtt":
                    textpath = os.path.join(output_dir, file)
                    os.replace(textpath, textpath.replace(f".{langs}", ""))

    for filename_check in os.listdir(os.path.dirname(out)):
        if filename_check == out.name.replace('.mp4','.m4a'):
            os.replace(str(out).replace(".mp4", ".m4a"), out)
        if filename_check == out.name.replace(".mp4", ".ts"):
            os.replace(str(out).replace(".mp4", ".ts"), out)
        if filename_check == out.name.replace(".mp4", ".srt"):
            os.replace(str(out).replace(".mp4", ".srt"), out)
        if filename_check == out.name.replace(".mp4", ".vtt"):
            os.replace(str(out).replace(".mp4", ".vtt"), out)
    
    if isinstance(url, Path) and os.path.isfile(url):
        os.unlink(url)

    if not quiet:
        print()
