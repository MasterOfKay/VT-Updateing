import atexit
import faulthandler
import logging
import os
import sys
from datetime import datetime

import click

from vinetrimmer.config import config, directories, filenames  # isort: split
from vinetrimmer.commands import dl
from vinetrimmer.utils.richconsole import ColoredConsoleHandler

# Keep a reference so the file object outlives setup_crash_handler().
_crash_log = None


def setup_crash_handler():
    """
    Dump the Python stack of every thread when the process dies natively.

    A native crash (the 0xc0000005 access violations seen partway through large
    4K downloads) terminates the interpreter without unwinding, so there is no
    traceback, no `except` handler and no final log line - the run just stops and
    the batch file moves on to the next title. faulthandler installs a Windows
    exception handler that writes the stacks before the process goes away, which
    is the only way to see which code is at fault.

    Gets its own file rather than the normal log so it works regardless of the
    `generate_logs` setting. The file is removed again on a clean exit, so one
    only survives when there was actually a crash.
    """
    global _crash_log

    os.makedirs(directories.logs, exist_ok=True)
    path = os.path.join(directories.logs, f"crash_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
    _crash_log = open(path, "w", encoding="utf-8")
    faulthandler.enable(file=_crash_log, all_threads=True)

    def discard_if_empty():
        # Only reached on a normal exit; a native crash never runs atexit hooks,
        # so a non-empty file is left behind exactly when we want one.
        faulthandler.disable()
        _crash_log.close()
        try:
            if os.path.getsize(path) == 0:
                os.unlink(path)
        except OSError:
            pass

    atexit.register(discard_if_empty)
    return path


@click.command(context_settings=dict(
    allow_extra_args=True,
    ignore_unknown_options=True,
    max_content_width=116,  # max PEP8 line-width, -4 to adjust for initial indent
))
@click.option("--debug", is_flag=True, default=False,
              help="Enable DEBUG level logs on the console. This is always enabled for log files.")
def main(debug):
    """
    vinetrimmer is the most convenient command-line program to
    download videos from Playready and Widevine DRM-protected video platforms.
    """
    LOG_FORMAT = "{asctime} [{levelname[0]}] {name} : {message}"
    LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
    LOG_STYLE = "{"

    def log_exit(self, msg, *args, **kwargs):
        self.critical(msg, *args, **kwargs)
        sys.exit(1)

    logging.Logger.exit = log_exit

    crash_log = setup_crash_handler()

    handlers = []
    if getattr(config, "generate_logs", True):
        os.makedirs(directories.logs, exist_ok=True)
        handlers.append(logging.FileHandler(
            os.path.join(directories.logs, filenames.log.format(time=datetime.now().strftime("%Y%m%d-%H%M%S"))),
            encoding='utf-8'
        ))

    logging.basicConfig(
        level=logging.DEBUG,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        style=LOG_STYLE,
        handlers=handlers,
    )

    # Same coloredlogs color scheme as before, just printed through dl.py's shared Rich Console
    # (rather than a plain StreamHandler straight to stderr) so log lines and the Live-rendered
    # download progress tree don't fight over the terminal - Rich only pauses/resumes a Live
    # redraw for other output when that output goes through the same Console instance the Live
    # is bound to.
    console_handler = ColoredConsoleHandler(LOG_FORMAT, LOG_DATE_FORMAT, LOG_STYLE)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger().addHandler(console_handler)

    log = logging.getLogger("vt")

    log.info("vinetrimmer - Playready and Widevine DRM downloader and decrypter")
    log.info(f"[Root Config]     : {filenames.user_root_config}")
    log.info(f"[Service Configs] : {directories.service_configs}")
    log.info(f"[Cookies]         : {directories.cookies}")
    log.info(f"[CDM Devices]     : {directories.devices}")
    log.info(f"[Cache]           : {directories.cache}")
    log.info(f"[Logs]            : {directories.logs}")
    log.info(f"[Temp Files]      : {directories.temp}")
    log.info(f"[Downloads]       : {directories.downloads}")
    log.debug(f"[Crash Log]       : {crash_log}")
    
    # Prefer locally bundled tools, then fall back to the system PATH. ./binaries
    # holds the Windows .exe builds, so off Windows ./linux-binaries takes priority
    # and anything missing from it is picked up from the distro packages instead.
    bin_dirs = [os.path.abspath('./binaries')]
    if sys.platform != 'win32':
        bin_dirs.insert(0, os.path.abspath('./linux-binaries'))
    os.environ['PATH'] = os.pathsep.join([*bin_dirs, os.environ.get('PATH', '')])

    if len(sys.argv) > 1 and sys.argv[1].lower() == "dl":
        sys.argv.pop(1)

    dl()


if __name__ == "__main__":
    main()
