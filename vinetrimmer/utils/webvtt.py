"""
Direct WebVTT -> ASS conversion that keeps cue positioning and colours.

The old path (pycaption -> SRT -> pysubs2) throws both away, because SRT can
represent neither one. Reading the WebVTT document itself lets cue settings
(`line`, `position`, `align`) survive as ASS alignment plus margins, and lets
inline colour spans survive as `\\c` override tags.
"""

import html
import re

import pysubs2

# ASS coordinates are meaningless without a reference resolution, and every margin
# below is computed as a fraction of these. 1080p is the sane default: libass scales
# the whole script to the video, so the output renders identically on a 720p or 4k
# file, it just has to agree with itself.
PLAY_RES_X = 1920
PLAY_RES_Y = 1080

# Default style geometry, also expressed against PLAY_RES_*.
DEFAULT_FONT_NAME = "Arial"
DEFAULT_FONT_SIZE = 54
DEFAULT_MARGIN_H = 96
DEFAULT_MARGIN_V = 54
DEFAULT_PRIMARY = (0xFF, 0xFF, 0xFF)

# One rendered line of text, as a percentage of viewport height. WebVTT's `line`
# places the *top* of the cue box, so working out where the text's baseline sits
# means knowing how tall the box is, and that needs a line height.
LINE_HEIGHT_PCT = 100.0 * DEFAULT_FONT_SIZE / PLAY_RES_Y

# How far up the frame the ordinary caption band reaches. A cue whose box already
# ends inside this band is sitting where subtitles normally sit rather than being
# deliberately lifted clear of something, so the style's own margin should place it.
BOTTOM_SAFE_PCT = 12.0

# WebVTT's built-in cue classes. Only the foreground colours are mapped - the
# `bg_*` variants set a box behind the text, which in ASS is a per-style
# BorderStyle rather than something a single line can switch on.
# https://www.w3.org/TR/webvtt1/#default-text-color
CUE_CLASS_COLOURS = {
    "white": (0xFF, 0xFF, 0xFF),
    "lime": (0x00, 0xFF, 0x00),
    "cyan": (0x00, 0xFF, 0xFF),
    "red": (0xFF, 0x00, 0x00),
    "yellow": (0xFF, 0xFF, 0x00),
    "magenta": (0xFF, 0x00, 0xFF),
    "blue": (0x00, 0x00, 0xFF),
    "black": (0x00, 0x00, 0x00),
}

# CSS keywords that name a category rather than a font, so they carry nothing to embed.
GENERIC_FAMILIES = {
    "sans-serif", "serif", "monospace", "cursive", "fantasy", "system-ui",
    "ui-sans-serif", "ui-serif", "ui-monospace", "ui-rounded", "math", "emoji",
    "inherit", "initial", "unset", "revert",
}

CSS_COLOURS = dict(CUE_CLASS_COLOURS, **{
    "green": (0x00, 0x80, 0x00),
    "silver": (0xC0, 0xC0, 0xC0),
    "gray": (0x80, 0x80, 0x80),
    "grey": (0x80, 0x80, 0x80),
    "maroon": (0x80, 0x00, 0x00),
    "olive": (0x80, 0x80, 0x00),
    "purple": (0x80, 0x00, 0x80),
    "teal": (0x00, 0x80, 0x80),
    "navy": (0x00, 0x00, 0x80),
    "orange": (0xFF, 0xA5, 0x00),
})

CUE_TIMING = re.compile(
    r"^(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
    r"(?P<settings>.*)$"
)

# A tag is anything between angle brackets. The name is optional so that karaoke
# timestamps (`<00:00:01.000>`) fall into the same branch and get dropped.
TAG = re.compile(r"<(?P<close>/)?(?P<name>[A-Za-z0-9_]*)(?P<rest>[^>]*)>")

CUE_SELECTOR = re.compile(r"::cue(?:\((?P<selector>[^)]*)\))?\s*\{(?P<body>[^}]*)\}", re.S)

# Cues that straddle a segment boundary are emitted once per segment, so a
# concatenated segmented document repeats them. Two copies of the same text
# within this many ms of each other are the same cue, not a deliberate repeat.
DUPLICATE_GAP_MS = 200


def _timestamp_to_ms(value):
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]
    else:
        hours, minutes, seconds = parts[0], parts[1], parts[2]
    seconds, _, millis = seconds.partition(".")
    return (
        int(hours) * 3600000
        + int(minutes) * 60000
        + int(seconds) * 1000
        + int(millis.ljust(3, "0")[:3])
    )


def _rgb_to_ass(rgb):
    """ASS stores colours as &HBBGGRR&, the reverse of the usual RGB order."""
    red, green, blue = rgb
    return f"&H{blue:02X}{green:02X}{red:02X}&"


def _reset_colour():
    """
    Return to the style's colour once every colour span has closed.

    A bare `\\c` means exactly this and libass honours it, but writing the value
    out keeps older renderers happy, and the style's colour is known here because
    `build_style` sets it.
    """
    return f"{{\\c{_rgb_to_ass(DEFAULT_PRIMARY)}}}"


def _parse_css_colour(value):
    value = (value or "").strip().lower()
    if not value:
        return None
    if value in CSS_COLOURS:
        return CSS_COLOURS[value]
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value)
    if match:
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    match = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    if match:
        parts = [p.strip() for p in match.group(1).split(",")]
        if len(parts) >= 3:
            try:
                return tuple(max(0, min(255, int(float(p)))) for p in parts[:3])
            except ValueError:
                return None
    return None


def _parse_font_family(value):
    """
    First real family named by a CSS font-family declaration.

    The generic keywords are what a stylesheet falls back to rather than a font that
    exists, so a declaration naming only those is treated as naming nothing.
    """
    for part in (value or "").split(","):
        name = part.strip().strip("\"'").strip()
        if name and name.lower() not in GENERIC_FAMILIES:
            return name
    return None


def _parse_style_blocks(blocks):
    """
    Read what the document's STYLE blocks ask for.

    Returns (default_font, class_styles): the family a bare `::cue` rule sets for every
    cue, and the colour/font that `::cue(.name)` rules attach to individual classes.
    """
    default_font = None
    class_styles = {}
    for block in blocks:
        for match in CUE_SELECTOR.finditer(block):
            selector = (match.group("selector") or "").strip()
            declarations = dict(
                (k.strip().lower(), v.strip())
                for k, _, v in (d.partition(":") for d in match.group("body").split(";"))
                if k.strip()
            )
            colour = _parse_css_colour(declarations.get("color"))
            font = _parse_font_family(declarations.get("font-family"))

            # Only plain class selectors are honoured. Anything more specific
            # (`v[voice="..."]`, `#cue-id`) needs per-cue matching that the payload
            # parser has no way to resolve.
            class_names = re.findall(r"\.([A-Za-z0-9_-]+)", selector)
            if not class_names:
                # A bare `::cue` styles every cue in the document.
                if font and not selector.strip("()"):
                    default_font = font
                continue
            for class_name in class_names:
                entry = class_styles.setdefault(class_name.lower(), {})
                if colour:
                    entry["colour"] = colour
                if font:
                    entry["font"] = font
    return default_font, class_styles


def _parse_regions(blocks):
    """Map region ids to their vertical viewport anchor, as a percentage."""
    regions = {}
    for block in blocks:
        # Region settings are colon separated, same as the ones on a cue timing line.
        settings = _parse_settings(block)
        region_id = settings.get("id")
        if not region_id:
            continue
        anchor = settings.get("viewportanchor", "")
        parts = anchor.split(",")
        if len(parts) == 2:
            try:
                regions[region_id] = float(parts[1].rstrip("%"))
            except ValueError:
                pass
    return regions


def _parse_settings(text):
    settings = {}
    for token in text.split():
        key, _, value = token.partition(":")
        if value:
            settings[key.strip().lower()] = value.strip()
    return settings


def _percentage(value):
    if value is None:
        return None
    try:
        return float(str(value).split(",")[0].rstrip("%"))
    except ValueError:
        return None


def _resolve_alignment(settings, regions, line_count=1):
    """
    Work out the ASS numpad alignment and margins a cue's settings ask for.

    Returns (alignment, margin_l, margin_r, margin_v). A margin of 0 means "leave
    it to the style", which is what a cue with no relevant setting gets.

    `line_count` is how many lines the cue renders as. It matters because WebVTT's
    `line` places the top of the cue box while ASS anchors a bottom aligned event by
    its bottom, so the box's own height has to come out of the conversion. Netflix is
    the clearest case: it keeps the box's bottom edge fixed and moves `line` up by one
    line height for every extra line, so ignoring the count makes a two line cue sit a
    whole line higher than a one line cue that is meant to share its baseline.
    """
    align = (settings.get("align") or "").lower()
    if align in ("start", "left"):
        column = 1  # left
    elif align in ("end", "right"):
        column = 3  # right
    else:
        column = 2  # centre, and WebVTT's default

    # `line` is either a percentage down the viewport or a line count, where a
    # negative count is measured up from the bottom.
    line_raw = settings.get("line")
    line_pct = None
    if line_raw is not None:
        if "%" in line_raw:
            line_pct = _percentage(line_raw)
        else:
            try:
                line_no = int(float(line_raw.split(",")[0]))
            except ValueError:
                line_no = None
            if line_no is not None:
                # ~5% of viewport height per line of text.
                line_pct = 100.0 + line_no * 5.0 if line_no < 0 else line_no * 5.0
    if line_pct is None and settings.get("region") in regions:
        line_pct = regions[settings["region"]]

    # Where the bottom of the cue box lands, which is the edge that decides whether a
    # cue reads as sitting at the bottom of the frame. Classifying on `line` alone puts
    # a tall cue in the middle purely for being tall: Netflix writes `line:68.67%` for a
    # four line caption whose box still ends at 90%, and reading only the top of it
    # would drag that caption into the centre of the picture.
    box_bottom_pct = None
    if line_pct is not None:
        box_bottom_pct = line_pct + max(1, line_count) * LINE_HEIGHT_PCT

    if line_pct is None:
        row = 0  # bottom, WebVTT's default
    elif line_pct < 30:
        row = 2  # top
    elif box_bottom_pct >= 70:
        row = 0  # bottom
    else:
        row = 1  # middle

    alignment = column + row * 3

    margin_v = 0
    if line_pct is not None:
        if row == 2:
            # Top aligned, so the margin is measured from the top of the frame down to
            # the top of the box - which is what `line` already gives.
            margin_v = round(line_pct * PLAY_RES_Y / 100)
        elif row == 0:
            # Bottom aligned, so the margin is the gap left under the box. libass grows
            # the text upwards from there, which is exactly how the source stacks its
            # own lines, so the two agree however many lines the cue turns out to have.
            margin_v = round(max(0.0, 100.0 - box_bottom_pct) * PLAY_RES_Y / 100)
        margin_v = min(margin_v, PLAY_RES_Y // 2)
        if row == 0 and 100.0 - box_bottom_pct <= BOTTOM_SAFE_PCT:
            # The cue ends inside the ordinary caption band, so it is restating the
            # default position (`line:-1`, `line:90%`, and every plain Netflix cue)
            # rather than asking to be moved. Let the style own it, so the track sits
            # where the configured margin says and not at a hardcoded height.
            margin_v = 0
        elif margin_v <= DEFAULT_MARGIN_V:
            margin_v = 0

    margin_l = margin_r = 0
    position_pct = _percentage(settings.get("position"))
    # Within a couple of percent of centre is just the default restated.
    if position_pct is not None and abs(position_pct - 50.0) > 2.0:
        anchor = round(position_pct * PLAY_RES_X / 100)
        if column == 1:
            margin_l = anchor
        elif column == 3:
            margin_r = PLAY_RES_X - anchor
        else:
            # Centred text sits midway between the two margins, so shifting the
            # midpoint to `anchor` means padding whichever side is shorter.
            if anchor < PLAY_RES_X // 2:
                margin_r = PLAY_RES_X - 2 * anchor
            else:
                margin_l = 2 * anchor - PLAY_RES_X
        margin_l = max(0, min(margin_l, PLAY_RES_X - 1))
        margin_r = max(0, min(margin_r, PLAY_RES_X - 1))

    return alignment, margin_l, margin_r, margin_v


def _unescape(text):
    # &lrm;/&rlm; are the directionality marks that keep mixed RTL text readable.
    # html.unescape resolves them to invisible characters, which survive the round
    # trip but are impossible to spot in the output, so keep them as entities.
    placeholders = {"&lrm;": "\x00LRM\x00", "&rlm;": "\x00RLM\x00"}
    for entity, placeholder in placeholders.items():
        text = text.replace(entity, placeholder)
    text = html.unescape(text)
    for entity, placeholder in placeholders.items():
        text = text.replace(placeholder, entity)
    return text


def _escape_ass(text):
    """
    Neutralise the characters ASS reads as markup.

    Backslashes are left alone. ASS has no escape for one, and swapping in a
    lookalike would silently alter the text; libass renders a backslash followed
    by anything other than N/n/h literally anyway.
    """
    return text.replace("{", "\\{").replace("}", "\\}")


def _convert_payload(payload, class_styles, default_font):
    """Turn a cue payload into ASS dialogue text, mapping inline tags to overrides."""
    out = []
    # Nesting counters, so `<i>a<i>b</i>c</i>` does not close early.
    depth = {"i": 0, "b": 0, "u": 0}
    colours = []
    fonts = []

    def push(stack, value, tag, fallback):
        """Enter a span, emitting an override only when it changes anything."""
        # An unrecognised class still has to push, or its `</c>` would pop a value it
        # never set.
        stack.append(value or (stack[-1] if stack else None))
        if stack[-1] and stack[-1] != fallback:
            out.append(tag(stack[-1]))

    def pop(stack, tag, fallback):
        """Leave a span, restoring whatever the enclosing one had."""
        if not stack:
            return
        # A span that set nothing resolves to the fallback at both ends, so closing it
        # emits nothing rather than restating the style's own value.
        previous = stack.pop() or fallback
        current = (stack[-1] if stack else None) or fallback
        if current != previous:
            out.append(tag(current))

    colour_tag = lambda c: f"{{\\c{_rgb_to_ass(c)}}}"  # noqa: E731
    font_tag = lambda f: f"{{\\fn{f}}}"  # noqa: E731

    position = 0
    for match in TAG.finditer(payload):
        literal = payload[position:match.start()]
        if literal:
            out.append(_escape_ass(_unescape(literal)))
        position = match.end()

        name = match.group("name").lower()
        rest = match.group("rest") or ""
        closing = bool(match.group("close"))

        if name in depth:
            if closing:
                if depth[name]:
                    depth[name] -= 1
                    if not depth[name]:
                        out.append(f"{{\\{name}0}}")
            else:
                depth[name] += 1
                if depth[name] == 1:
                    out.append(f"{{\\{name}1}}")
        elif name == "c":
            if closing:
                pop(colours, colour_tag, DEFAULT_PRIMARY)
                pop(fonts, font_tag, default_font)
            else:
                classes = [c.lower() for c in rest.lstrip(".").split(".") if c]
                styles = [class_styles[c] for c in classes if c in class_styles]
                push(
                    colours,
                    next((s["colour"] for s in styles if "colour" in s), None),
                    colour_tag, DEFAULT_PRIMARY,
                )
                push(
                    fonts,
                    next((s["font"] for s in styles if "font" in s), None),
                    font_tag, default_font,
                )
        elif name == "font":
            if closing:
                pop(colours, colour_tag, DEFAULT_PRIMARY)
                pop(fonts, font_tag, default_font)
            else:
                attr = re.search(r"color\s*=\s*[\"']?([^\"'\s>]+)", rest, re.I)
                push(
                    colours,
                    _parse_css_colour(attr.group(1)) if attr else None,
                    colour_tag, DEFAULT_PRIMARY,
                )
                face = re.search(r"face\s*=\s*[\"']([^\"']+)|face\s*=\s*([^\s>]+)", rest, re.I)
                push(
                    fonts,
                    _parse_font_family(face.group(1) or face.group(2)) if face else None,
                    font_tag, default_font,
                )
        # Everything else - <v>, <lang>, <ruby>, <rt>, karaoke timestamps - carries
        # no styling ASS can express, so the tag goes and the text it wraps stays.

    literal = payload[position:]
    if literal:
        out.append(_escape_ass(_unescape(literal)))

    # Close anything the cue left open, so styling cannot bleed past its own line.
    for name, count in depth.items():
        if count:
            out.append(f"{{\\{name}0}}")
    # `any`, not truthiness of the list: a span left open by a malformed cue may have
    # pushed nothing at all, and there is no override to undo in that case.
    if any(colours):
        out.append(_reset_colour())
    if any(fonts):
        out.append(font_tag(default_font))

    return "".join(out).replace("\n", "\\N").strip()


def _dedupe(events):
    """Collapse the repeated cues that segmented WebVTT produces at each boundary."""
    merged = []
    for event in events:
        previous = next(
            (
                e for e in reversed(merged)
                if e.text == event.text and e.style == event.style
            ),
            None,
        )
        if (
            previous is not None
            and event.start - previous.end <= DUPLICATE_GAP_MS
            and event.start >= previous.start
        ):
            previous.end = max(previous.end, event.end)
            continue
        merged.append(event)
    return merged


def apply_script_style(subs, font_name=DEFAULT_FONT_NAME, font_size=DEFAULT_FONT_SIZE):
    """
    Give an SSAFile the script header and Default style everything here assumes.

    Every margin in this module is expressed against PLAY_RES_*, so a file without
    those headers is read at pysubs2's 384x288 default and each one lands in the wrong
    place. Applied to conversions from other formats too, for the same reason.
    """
    subs.info["PlayResX"] = str(PLAY_RES_X)
    subs.info["PlayResY"] = str(PLAY_RES_Y)
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"
    subs.styles["Default"] = build_style(font_name, font_size)
    return subs


def build_style(font_name=DEFAULT_FONT_NAME, font_size=DEFAULT_FONT_SIZE):
    style = pysubs2.SSAStyle()
    style.fontname = font_name
    style.fontsize = font_size
    style.primarycolor = pysubs2.Color(255, 255, 255)
    style.outlinecolor = pysubs2.Color(0, 0, 0)
    style.backcolor = pysubs2.Color(0, 0, 0, 128)
    style.outline = 2.4
    style.shadow = 1.0
    style.alignment = 2
    style.marginl = style.marginr = DEFAULT_MARGIN_H
    style.marginv = DEFAULT_MARGIN_V
    return style


def webvtt_to_ssa(data, font_name=None, font_size=DEFAULT_FONT_SIZE):
    """
    Parse a WebVTT document into a pysubs2 SSAFile, preserving position and colour.

    `font_name` forces a family. Left as None, the document's own `::cue` rule decides,
    and a document that names no font gets the plain default rather than one picked on
    its behalf.

    `data` may be bytes or str, and may be the concatenation of many segments that
    `TextTrack.normalize_vtt` has already collapsed into one document.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    data = data.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")

    blocks = [b for b in re.split(r"\n[ \t]*\n", data) if b.strip()]

    style_blocks = [b for b in blocks if b.lstrip().startswith("STYLE")]
    region_blocks = [b for b in blocks if b.lstrip().startswith("REGION")]
    declared_font, parsed_styles = _parse_style_blocks(style_blocks)
    class_styles = {name: {"colour": rgb} for name, rgb in CUE_CLASS_COLOURS.items()}
    for name, entry in parsed_styles.items():
        class_styles.setdefault(name, {}).update(entry)
    regions = _parse_regions(region_blocks)

    font_name = font_name or declared_font or DEFAULT_FONT_NAME
    subs = apply_script_style(pysubs2.SSAFile(), font_name, font_size)

    events = []
    for block in blocks:
        lines = block.split("\n")
        # An optional identifier may precede the timing line.
        timing = None
        payload_start = 0
        for index, line in enumerate(lines[:2]):
            match = CUE_TIMING.match(line.strip())
            if match:
                timing = match
                payload_start = index + 1
                break
        if not timing:
            continue

        payload = "\n".join(lines[payload_start:]).strip()
        if not payload:
            continue

        # The payload is converted first because the margins depend on how many lines
        # the cue ends up being, and only the converted text knows that - a payload can
        # carry line breaks that resolve away, or tags that do not add one.
        text = _convert_payload(payload, class_styles, font_name)
        if not text:
            continue

        settings = _parse_settings(timing.group("settings"))
        alignment, margin_l, margin_r, margin_v = _resolve_alignment(
            settings, regions, line_count=text.count(r"\N") + 1
        )

        event = pysubs2.SSAEvent(
            start=_timestamp_to_ms(timing.group("start")),
            end=_timestamp_to_ms(timing.group("end")),
            text=text,
        )
        event.marginl = margin_l
        event.marginr = margin_r
        event.marginv = margin_v
        if alignment != 2:
            # ASS has no per-event alignment column, so it rides along as an override.
            event.text = f"{{\\an{alignment}}}{event.text}"
        events.append(event)

    subs.events = _dedupe(events)
    subs.sort()
    return subs
