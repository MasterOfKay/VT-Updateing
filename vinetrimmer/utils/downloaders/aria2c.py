"""Aria2c downloader wrapper - moved from io.py for better organization"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from vinetrimmer.utils.collections import as_list


def aria2c(uri, out, headers=None, proxy=None, quiet=False):
    """
    Downloads file(s) using Aria2(c).

    Synchronous on purpose: it is called from download worker threads, where an
    event loop per track is pure overhead. See vinetrimmer.utils.io.aria2c.

    Parameters:
        uri: URL to download. If uri is a list of urls, they will be downloaded and
          concatenated into one file.
        out: The output file path to save to.
        headers: Headers to apply on aria2c.
        proxy: Proxy to apply on aria2c.
        quiet: If True, suppress all console output from aria2c.
    """
    # Import here to avoid circular import
    from vinetrimmer.config import config
    
    executable = shutil.which("aria2c") or shutil.which("aria2")
    if not executable:
        raise EnvironmentError("Aria2c executable not found...")

    arguments = [
        executable,
        "-c",  # Continue downloading a partially downloaded file
        "--remote-time",  # Retrieve timestamp of the remote file from the and apply if available
        "-o", os.path.basename(out),  # The file name of the downloaded file, relative to -d
        "-x", "16",  # The maximum number of connections to one server for each download
        "-j", "16",  # The maximum number of parallel downloads for every static (HTTP/FTP) URL
        "-s", "16",  # Download a file using N connections.
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--retry-wait", "5",  # Set the seconds to wait between retries.
        "--max-tries", "15",
        "--max-file-not-found", "15",
        "--summary-interval", "0",
        "--file-allocation", "none" if sys.platform == "win32" else "falloc",
        "--console-log-level", "error" if quiet else "warn",
        "--download-result", "hide"
    ]

    for option, value in config.aria2c.items():
        arguments.append(f"--{option.replace('_', '-')}={value}")

    for header, value in (headers or {}).items():
        if header.lower() == "accept-encoding":
            # we cannot set an allowed encoding, or it will return compressed
            # and the code is not set up to uncompress the data
            continue
        arguments.extend(["--header", f"{header}: {value}"])

    segmented = isinstance(uri, list)
    segments_dir = f"{out}_segments"

    if segmented:
        uri = "\n".join([
            f"{url}\n"
            f"\tdir={segments_dir}\n"
            f"\tout={i:08}.mp4"
            for i, url in enumerate(uri)
        ])

    if proxy:
        arguments.extend(["--all-proxy", proxy])

    try:
        stdout_arg = subprocess.DEVNULL if quiet else None
        stderr_arg = subprocess.DEVNULL if quiet else None
        
        if segmented:
            subprocess.run(
                arguments + ["-d", segments_dir, "-i-"],
                input=as_list(uri)[0],
                encoding="utf-8",
                check=True,
                stdout=stdout_arg,
                stderr=stderr_arg
            )
        else:
            subprocess.run(
                arguments + ["-d", os.path.dirname(out), uri],
                check=True,
                stdout=stdout_arg,
                stderr=stderr_arg
            )
    except subprocess.CalledProcessError:
        raise ValueError("Aria2c failed too many times, aborting")

    if segmented:
        # merge the segments together
        with open(out, "wb") as ofd:
            for file in sorted(os.listdir(segments_dir)):
                file = os.path.join(segments_dir, file)
                with open(file, "rb") as ifd:
                    data = ifd.read()
                # Apple TV+ needs this done to fix audio decryption
                data = re.sub(b"(tfhd\x00\x02\x00\x1a\x00\x00\x00\x01\x00\x00\x00)\x02", b"\\g<1>\x01", data)
                ofd.write(data)
        shutil.rmtree(segments_dir)
