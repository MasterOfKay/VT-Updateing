import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import threading
import time
import httpx
import pproxy
import requests
import yaml
import tqdm
import logging
from vinetrimmer import config
from pathlib import Path

try:
    from humanfriendly import format_size
except ImportError:
    # Fallback if humanfriendly not available
    def format_size(size, binary=True):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"


_active_processes = set()
_active_processes_lock = threading.Lock()


def _register_process(proc):
    with _active_processes_lock:
        _active_processes.add(proc)


def _unregister_process(proc):
    with _active_processes_lock:
        _active_processes.discard(proc)


# aria2c emits a status line roughly once a second, e.g.
#   [#1f8856 10MiB/60MiB(16%) CN:2 DL:9.8MiB ETA:5s]
# It does this even with --summary-interval 0, and it does it on a pipe as well
# as a tty, so it is a reliable progress source. Trailing fields are optional:
# CN/DL/ETA are omitted while a download is still connecting.
ARIA2C_PROGRESS_RE = re.compile(
    r"\[#(?P<gid>\w+)\s+(?P<done>[\d.]+\w*)/(?P<total>[\d.]+\w*)\((?P<percent>\d+)%\)"
    r"(?:\s+CN:(?P<connections>\d+))?"
    r"(?:\s+DL:(?P<speed>[\d.]+\w*))?"
    r"(?:\s+ETA:(?P<eta>\S+?))?\]"
)


# How long to wait for aria2c's first status line before falling back to
# measuring the file on disk. It reports about once a second.
ARIA2C_REPORT_GRACE = 5

_ARIA2C_SPEED_RE = re.compile(r"^(?P<value>[\d.]+)(?P<unit>[KMGT]?i?B)?$")
_ARIA2C_SPEED_UNITS = {
    "B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
    "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
}


def _parse_aria2c_speed(speed):
    """Convert an aria2c DL: field (e.g. '9.8MiB', '0B') to bytes/sec, or None."""
    if not speed:
        return None
    match = _ARIA2C_SPEED_RE.match(speed.strip())
    if not match:
        return None
    try:
        return float(match.group("value")) * _ARIA2C_SPEED_UNITS.get(match.group("unit") or "B", 1)
    except ValueError:
        return None


def _read_aria2c_progress(stream, state, lock):
    """
    Drain aria2c's stdout, recording the most recent progress report into `state`.

    Runs on a background thread: the status line is terminated with a carriage
    return rather than a newline, so this reads raw chunks instead of lines.
    Draining is unconditional -- if we stopped reading, aria2c would block on a
    full pipe buffer and the download would hang.
    """
    buffer = ""
    try:
        while True:
            chunk = stream.read(256)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            # Keep only the tail: a status line is far shorter than this.
            if len(buffer) > 8192:
                buffer = buffer[-8192:]
            matches = list(ARIA2C_PROGRESS_RE.finditer(buffer))
            if matches:
                with lock:
                    state["percent"] = int(matches[-1].group("percent"))
                    state["speed"] = matches[-1].group("speed")
                    state["seen"] = True
                buffer = buffer[matches[-1].end():]
    except (ValueError, OSError):
        # stream closed underneath us while the process was being torn down
        pass


def terminate_all_downloads():
    """Forcefully kill all currently running download subprocesses (used on cancel/failure)."""
    with _active_processes_lock:
        procs = list(_active_processes)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


def load_yaml(path):
    if not os.path.isfile(path):
        return {}
    with open(path) as fd:
        return yaml.safe_load(fd)


_ip_info = None


def get_ip_info(session=None, fresh=False):
    """Use extreme-ip-lookup.com to get IP location information."""
    global _ip_info

    if fresh or not _ip_info:
        # alternatives: http://www.geoplugin.net/json.gp, http://ip-api.com/json/, https://extreme-ip-lookup.com/json
        _ip_info = (session or httpx).get("https://ipwho.is/").json()

    return _ip_info


@contextlib.asynccontextmanager
async def start_pproxy(host, port, username, password):
    rerouted_proxy = "http://localhost:8081"
    server = pproxy.Server(rerouted_proxy)
    remote = pproxy.Connection(f"http+ssl://{host}:{port}#{username}:{password}")
    handler = await server.start_server(dict(rserver=[remote]))
    try:
        yield rerouted_proxy
    finally:
        handler.close()
        await handler.wait_closed()



def download_range(url, count, start=0, proxy=None):
    """Download n bytes without using the Range header due to support issues."""
    # TODO: Can this be done with Aria2c?
    executable = shutil.which("curl")
    if not executable:
        raise EnvironmentError("Track needs curl to download a chunk of data but wasn't found...")

    arguments = [
        executable,
        "-s",  # use -s instead of --no-progress-meter due to version requirements
        "-L",  # follow redirects, e.g. http->https
        "--proxy-insecure",  # disable SSL verification of proxy
        "--output", "-",  # output to stdout
        "--url", url
    ]
    if proxy:
        arguments.extend(["--proxy", proxy])

    curl = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=open(os.devnull, "wb"),
        shell=False
    )
    buffer = b''
    location = -1
    while len(buffer) < count:
        stdout = curl.stdout
        data = b''
        if stdout:
            data = stdout.read(1)
        if len(data) > 0:
            location += len(data)
            if location >= start:
                buffer += data
        else:
            if curl.poll() is not None:
                break
    curl.kill()  # stop downloading
    return buffer


def _download_via_pproxy(arguments, uri, out, segmented, segments_dir, proxy):
    """
    Route aria2c through a local pproxy instance, needed for https:// proxies.

    pproxy only has an async API, so this one branch still creates an event loop.
    Everything else in aria2c() is synchronous on purpose - see the note there.
    """
    async def run():
        auth, hostname = proxy[8:].split("@")
        async with start_pproxy(*hostname.split(":"), *auth.split(":")) as pproxy_:
            arguments.extend([pproxy_, "-d"])
            if segmented:
                arguments.extend([segments_dir, "-i-"])
                proc = await asyncio.create_subprocess_exec(*arguments, stdin=subprocess.PIPE)
                await proc.communicate(uri.encode("utf-8"))
            else:
                arguments.extend([os.path.dirname(out), uri])
                proc = await asyncio.create_subprocess_exec(*arguments)
                await proc.communicate()

    asyncio.run(run())


def aria2c(uri, out, headers=None, proxy=None, quiet=False, progress_callback=None):
    """
    Downloads file(s) using Aria2(c).

    Deliberately not a coroutine: this is called from every download worker
    thread, and wrapping it in asyncio.run() there meant building and tearing
    down a Windows Proactor event loop per track on six threads at once. The
    work here is a subprocess plus a polling loop, neither of which needs an
    event loop.

    Parameters:
        uri: URL to download. If uri is a list of urls, they will be downloaded and
          concatenated into one file.
        out: The output file path to save to.
        headers: Headers to apply on aria2c.
        proxy: Proxy to apply on aria2c.
        quiet: If True, suppress console output from aria2c.
        progress_callback: Optional callback for progress updates (Rich Progress compatible).
    """
    executable = shutil.which("aria2c") or shutil.which("aria2")
    if not executable:
        raise EnvironmentError("Aria2c executable not found...")

    arguments = [
        executable,
        "-c",  # Continue downloading a partially downloaded file
        "--remote-time",  # Retrieve timestamp of the remote file from the and apply if available
        "-o", os.path.basename(out),  # The file name of the downloaded file, relative to -d
        "-x", "4",  # The maximum number of connections to one server for each download
        "-j", "4",  # The maximum number of parallel downloads for every static (HTTP/FTP) URL
        "-s", "4",  # Download a file using N connections.
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--retry-wait", "5",  # Set the seconds to wait between retries.
        "--max-tries", "15",
        "--max-file-not-found", "15",
        "--summary-interval", "0",
        "--file-allocation", "none",
        "--console-log-level", "warn",
        "--download-result", "hide",
        # SSL/TLS bypass options
        "--check-certificate=false",
        "--disable-ipv6",
        "--min-tls-version", "TLSv1.2",
    ]
    if quiet:
        arguments[arguments.index("--console-log-level") + 1] = "error"

    for option, value in config.config.aria2c.items():
        arguments.append(f"--{option.replace('_', '-')}={value}")

    for header, value in (headers or {}).items():
        if header.lower() == "accept-encoding":
            # we cannot set an allowed encoding, or it will return compressed
            # and the code is not set up to uncompress the data
            continue
        arguments.extend(["--header", f"{header}: {value}"])

    segmented = isinstance(uri, list)
    segment_count = len(uri) if segmented else 0
    segments_dir = f"{out}_segments"

    if segmented:
        uri = [u for u in uri if "rubedocdn" not in u]
        uri = "\n".join([
            f"{url}\n"
            f"\tdir={segments_dir}\n"
            f"\tout={i:08}.mp4"
            for i, url in enumerate(uri)
        ])

    if proxy:
        arguments.append("--all-proxy")
        if proxy.lower().startswith("https://"):
            _download_via_pproxy(arguments, uri, out, segmented, segments_dir, proxy)
        else:
            arguments.append(proxy)

    # With a progress_callback we parse aria2c's own status line rather than
    # inferring progress from the file size on disk, so its output has to be
    # piped to us instead of being discarded.
    if progress_callback:
        stdout_arg = subprocess.PIPE
        stderr_arg = subprocess.STDOUT
    else:
        stdout_arg = subprocess.DEVNULL if quiet else None
        stderr_arg = subprocess.DEVNULL if quiet else None

    proc = None
    aria_state = {"percent": None, "speed": None, "seen": False}
    aria_lock = threading.Lock()
    reader_thread = None
    try:
        if segmented:
            proc = subprocess.Popen(
                arguments + ["-d", segments_dir, "-i-"],
                stdin=subprocess.PIPE,
                stdout=stdout_arg,
                stderr=stderr_arg
            )
        else:
            proc = subprocess.Popen(
                arguments + ["-d", os.path.dirname(out), uri],
                stdout=stdout_arg,
                stderr=stderr_arg
            )
        _register_process(proc)

        # Start draining stdout *before* handing aria2c the input list. That list
        # is several MB on a segmented track, far more than a pipe buffer holds,
        # and aria2c writes progress lines while it reads. With nobody reading
        # stdout it blocks on the full pipe, stops consuming stdin, and both
        # sides wait on each other forever.
        if progress_callback and proc.stdout is not None:
            reader_thread = threading.Thread(
                target=_read_aria2c_progress,
                args=(proc.stdout, aria_state, aria_lock),
                daemon=True,
            )
            reader_thread.start()

        if segmented:
            proc.stdin.write(uri.encode("utf-8"))
            proc.stdin.close()

        if progress_callback:
            progress_callback(total=100, completed=0, downloaded="Starting...")

            expected_size = None
            if not segmented and isinstance(uri, str):
                try:
                    head = requests.head(
                        uri,
                        headers=headers,
                        proxies={"all": proxy} if proxy else None,
                        allow_redirects=True,
                        timeout=10,
                    )
                    content_length = head.headers.get("Content-Length")
                    if content_length:
                        expected_size = int(content_length)
                except Exception:
                    expected_size = None

            last_size = 0
            last_time = time.time()
            stalled_count = 0
            started_at = time.time()

            while proc.poll() is None:
                time.sleep(0.5)

                try:
                    with aria_lock:
                        aria_percent = aria_state["percent"]
                        aria_speed = aria_state["speed"]
                        aria_seen = aria_state["seen"]

                    current_size = 0
                    completed_percent = None

                    if segmented:
                        # Segment count is the honest measure of overall progress here:
                        # aria2c's percentage only covers the segments it has in flight.
                        if os.path.isdir(segments_dir):
                            segment_files = [
                                f
                                for f in os.listdir(segments_dir)
                                if f.lower().endswith(".mp4")
                            ]
                            completed_segments = len(segment_files)
                            if segment_count:
                                completed_percent = min(99, int((completed_segments / segment_count) * 100))
                            current_size = sum(
                                os.path.getsize(os.path.join(segments_dir, f))
                                for f in segment_files
                                if os.path.isfile(os.path.join(segments_dir, f))
                            )
                    elif aria_seen and aria_percent is not None:
                        # Single-file download: trust aria2c. The size on disk is not a
                        # usable signal -- with --file-allocation=prealloc the file is
                        # full-size before any data arrives, and with multiple
                        # connections (-s N) aria2c writes at offsets across the whole
                        # file, so its size jumps most of the way up immediately.
                        completed_percent = min(99, aria_percent)
                    elif time.time() - started_at > ARIA2C_REPORT_GRACE and os.path.exists(out):
                        # Fallback only, and only once aria2c has had time to report:
                        # jumping in earlier would show the bogus size-derived value
                        # for a second and then snap backwards.
                        current_size = os.path.getsize(out)
                        if expected_size and expected_size > 0:
                            completed_percent = min(99, int((current_size / expected_size) * 100))

                    current_time = time.time()

                    if completed_percent is not None:
                        progress_callback(completed=completed_percent)

                    if not segmented and aria_seen and aria_speed:
                        # aria2c reports throughput directly, e.g. DL:9.8MiB. Judge
                        # stalling on that rather than on the percentage holding
                        # steady -- on a large 4K track a single percent can take
                        # minutes of perfectly healthy downloading.
                        # Segmented downloads keep the size-derived speed below:
                        # with many segments in flight this field reports only one
                        # of them, not the combined rate.
                        if _parse_aria2c_speed(aria_speed) == 0:
                            stalled_count += 1
                        else:
                            stalled_count = 0
                        if stalled_count > 4:  # 2s of genuinely zero throughput
                            progress_callback(downloaded="Stalled...")
                        else:
                            progress_callback(downloaded=f"{aria_speed}/s")
                    elif current_size > last_size:
                        time_diff = current_time - last_time
                        if time_diff > 0:
                            speed = (current_size - last_size) / time_diff
                            progress_callback(downloaded=f"{format_size(speed, binary=True)}/s")
                        last_size = current_size
                        last_time = current_time
                        stalled_count = 0
                    elif segmented or not aria_seen:
                        stalled_count += 1
                        if stalled_count > 4:  # 2 seconds of no progress
                            progress_callback(downloaded="Stalled...")
                except Exception:
                    pass

            return_code = proc.wait()

            if return_code != 0:
                progress_callback(downloaded="[red]FAILED")
                raise subprocess.CalledProcessError(return_code, arguments)
            else:
                progress_callback(downloaded="Downloaded", completed=100)
        else:
            return_code = proc.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, arguments)
    except subprocess.CalledProcessError:
        if progress_callback:
            progress_callback(downloaded="[red]FAILED")
        raise ValueError("Aria2c failed too many times, aborting")
    finally:
        if reader_thread is not None:
            # stdout hits EOF when aria2c exits, so the reader ends on its own.
            reader_thread.join(timeout=2)
        if proc is not None:
            if proc.stdout is not None:
                with contextlib.suppress(OSError):
                    proc.stdout.close()
            _unregister_process(proc)

    if segmented:
        if progress_callback:
            progress_callback(downloaded="Merging")
        # merge the segments together - only if the directory exists and has files
        if os.path.exists(segments_dir):
            segment_files = sorted(os.listdir(segments_dir))
            if segment_files:
                with open(out, "wb") as ofd:
                    for file in segment_files:
                        file = os.path.join(segments_dir, file)
                        with open(file, "rb") as ifd:
                            data = ifd.read()
                        # Apple TV+ needs this done to fix audio decryption
                        data = re.sub(b"(tfhd\x00\x02\x00\x1a\x00\x00\x00\x01\x00\x00\x00)\x02", b"\\g<1>\x01", data)
                        ofd.write(data)
                        os.unlink(file)
                os.rmdir(segments_dir)
        if progress_callback:
            progress_callback(downloaded="Downloaded", completed=100)

    if not quiet:
        print()


def saldl(uri, out, headers=None, proxy=None):
    if headers:
        headers.update({k: v for k, v in headers.items() if k.lower() != "accept-encoding"})

    executable = shutil.which("saldl") or shutil.which("saldl-win64") or shutil.which("saldl-win32")
    if not executable:
        raise EnvironmentError("Saldl executable not found...")

    arguments = [
        executable,
        # "--no-status",
        "--skip-TLS-verification",
        "--resume",
        "--merge-in-order",
        "-c8",
        "--auto-size", "1",
        "-D", os.path.dirname(out),
        "-o", os.path.basename(out),
    ]

    if headers:
        arguments.extend([
            "--custom-headers",
            "\r\n".join([f"{k}: {v}" for k, v in headers.items()])
        ])

    if proxy:
        arguments.extend(["--proxy", proxy])

    if isinstance(uri, list):
        raise ValueError("Saldl code does not yet support multiple uri (e.g. segmented) downloads.")
    arguments.append(uri)

    try:
        subprocess.run(arguments, check=True)
    except subprocess.CalledProcessError:
        raise ValueError("Saldl failed too many times, aborting")

    print()
    
def tqdm_downloader(uri, out, headers=None, proxy=None):
    proxies = {'https': f"{proxy}"} if 'https://' in proxy else {'http': f"{proxy}"}
    r = requests.get(uri, proxies=proxies, stream=True)
    file_size = int(r.headers["Content-Length"])
    chunk = 1
    chunk_size = 1024
    num_bars = int(file_size / chunk_size)

    with open(out, "wb") as fp:
        for chunk in tqdm.tqdm(
            r.iter_content(chunk_size=chunk_size),
            total=num_bars,
            unit="KB",
            desc=out,
            leave=True,  # progressbar stays
        ):
            fp.write(chunk)
    
    print()

def m3u8re(uri, out, headers=None, proxy=None):
    out = Path(out)

    if headers:
        headers.update({k: v for k, v in headers.items() if k.lower() != "accept-encoding"})

    executable = shutil.which("m3u8re") or shutil.which("N_m3u8DL-RE")
    if not executable:
        raise EnvironmentError("N_m3u8DL-RE executable not found...")

    if isinstance(uri, list):
        raise ValueError("N_m3u8DL code does not yet support multiple uri (e.g. segmented) downloads.")

    arguments = [
        executable,
        uri,
        "--tmp-dir", str(out.parent),
        "--save-dir", str(out.parent),
        "--save-name", out.name.replace('.mp4','').replace('.vtt','').replace('.m4a',''),
        "--auto-subtitle-fix", "False",
        "--thread-count", "32",
        "--download-retry-count", "100",
        "--log-level", "INFO"
    ]

    if headers:
        arguments.extend([
            "--header",
            "\r\n".join([f"{k}: {v}" for k, v in headers.items()])
        ])
        
    if proxy:
        arguments.extend(["--custom-proxy", proxy])

    try:
        subprocess.run(arguments, check=True)
    except subprocess.CalledProcessError:
        raise ValueError("N_m3u8DL-RE failed too many times, aborting")

    print()
    
