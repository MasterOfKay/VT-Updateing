import logging

import coloredlogs
from rich.console import Console
from rich.text import Text

# Shared between vinetrimmer.py's logging setup and dl.py's Live-rendered download progress
# tree, so that regular log lines and the tree go through the same Console. Rich only knows to
# pause/resume a Live redraw around other output if that output goes through the exact Console
# instance the Live is bound to - a plain logging.StreamHandler writing straight to stderr has
# no such coordination and corrupts the tree whenever a log line lands mid-render (most
# noticeable with --debug, which unmutes asyncio/urllib3 chatter).
console = Console()


class ColoredConsoleHandler(logging.Handler):
    """
    Formats log records with coloredlogs (the original console color scheme: green asctime,
    blue logger name, level-colored message, etc.) and prints the resulting ANSI text through
    the shared Rich Console above, instead of coloredlogs' own StreamHandler writing straight to
    stderr. Console.print() is what lets Rich pause/resume a Live redraw around other output -
    a raw ANSI write straight to the stream wouldn't coordinate with it and would corrupt the
    progress tree instead.
    """

    def __init__(self, fmt, datefmt, style):
        super().__init__()
        self._formatter = coloredlogs.ColoredFormatter(fmt=fmt, datefmt=datefmt, style=style)

    def emit(self, record):
        try:
            console.print(Text.from_ansi(self._formatter.format(record)))
        except Exception:
            self.handleError(record)
