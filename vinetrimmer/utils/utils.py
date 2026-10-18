from __future__ import annotations


def print_section_header(title: str) -> None:
    """Print a simple console section header used by service/command logs."""
    if title is None:
        title = ""
    text = str(title).strip()
    line = "=" * 12
    print(f"\n{line} {text} {line}")
