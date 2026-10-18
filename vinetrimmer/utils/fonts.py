"""
Work out which fonts an ASS subtitle asks for, and find them for muxing.

An ASS file names font families but never carries them, so a player that lacks the
family silently substitutes something else and the styling is lost. Matroska solves
this with attachments: libass (mpv, VLC, MPC-HC) reads fonts attached to the file in
preference to the ones installed on the machine, so a muxed release renders the same
everywhere.

Matching is done on the family name recorded inside the font, not on the filename,
because the two rarely agree - Arial Bold ships as `arialbd.ttf`.
"""

import logging
import os
import re
import struct

log = logging.getLogger("fonts")

# Fonts each service's player loads over the web, so a family it references can be
# fetched once instead of being tracked down by hand. Nothing here comes from the
# manifest - a stream carries no fonts - these are the player's own CSS @font-face
# assets, so the list is a fixed handful per service rather than per-title.
#
# The name paired with each URL is the family to fall back on when the file's own
# name table is unusable. Netflix and HBO Max both ship theirs stripped, and libass
# matches on family name, so without this they cannot be referenced at all.
#
# Only the CineCav pair are caption fonts; the rest are the site/app UI faces. Using
# them is a look you choose, not a reproduction of what the service renders.
FONT_SOURCES = {
    "NF": {
        "referer": "https://www.netflix.com/",
        "fonts": [
            ("Netflix Sans",
             "https://assets.nflxext.com/ffe/siteui/fonts/netflix-sans/v3/NetflixSans_W_Md.woff2"),
            ("Roboto",
             "https://fonts.gstatic.com/s/roboto/v48/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3yUBA.woff2"),
        ],
    },
    "DSNP": {
        "referer": "https://www.disneyplus.com/",
        "fonts": [
            ("Inspire",
             "https://static-assets.bamgrid.com/fonts/inspire/"
             "Inspire-Regular.f2199f841165c4ddbafcf177da3ef974.woff2"),
            ("Inspire",
             "https://static-assets.bamgrid.com/fonts/inspire/"
             "Inspire-Bold.4ff15ddc93445342649194969715b0b5.woff2"),
        ],
    },
    "HMAX": {
        "referer": "https://play.hbomax.com/",
        "fonts": [
            ("Handset Sans UI",
             "https://play.hbomax.com/"
             "HandsetSansUI-Regular.b97141eb372e372e3327eda6d6912ffe.sha.woff2"),
            ("Handset Sans UI",
             "https://play.hbomax.com/"
             "HandsetSansUI-Bold.84d095a9c5c41b75737cdeeacc4a8fd4.sha.woff2"),
            # CineCav is the caption face used for CEA-608/708 rendering, so unlike the
            # rest of this table it is a font actually meant for subtitles.
            ("Cinecav Casual",
             "https://play.hbomax.com/"
             "cinecav-casual-regular.746864bb267a2afaf2c2e78ade4ffdeb.sha.ttf"),
            ("Cinecav Mono",
             "https://play.hbomax.com/"
             "cinecav-mono-regular.f75c8cf01af26dc676adab86e16bbb57.sha.ttf"),
        ],
    },
    "CR": {
        # Cloudflare fronts these and rejects a plain requests handshake, so the fetch
        # falls through to curl_cffi's browser TLS fingerprint.
        "referer": "https://www.crunchyroll.com/",
        "fonts": [
            ("Crunchyroll Atyp", "https://www.crunchyroll.com/fonts/CrunchyrollAtyp-Regular.woff2"),
            ("Crunchyroll Atyp", "https://www.crunchyroll.com/fonts/CrunchyrollAtyp-Bold.woff2"),
            ("DM Sans", "https://www.crunchyroll.com/fonts/DMSans.woff2"),
        ],
    },
    "ATVP": {
        # These 404 without a Referer.
        "referer": "https://www.apple.com/",
        "fonts": [
            ("SF Pro", "https://www.apple.com/wss/fonts/SF-Pro/v4/SFPro.woff2"),
            ("SF Pro Display",
             "https://www.apple.com/wss/fonts/SF-Pro-Display/v2/sf-pro-display_semibold.woff2"),
        ],
    },
    # ParamountPlus: add entries here once the player's font requests are known.
    "PMTP": {"referer": "https://www.paramountplus.com/", "fonts": []},
}

# The first four bytes of every format sfnt comes in. Checked instead of the response
# content type, because a blocked request answers 200 with an HTML error page, and one
# of these CDNs serves a real font as application/x-www-form-urlencoded.
FONT_MAGIC = (b"wOF2", b"wOFF", b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")

# Content hash that a CDN puts in the filename, optionally followed by a `.sha` marker.
# Stripped so a cache-busting rename does not re-download a font already held.
URL_HASH = re.compile(r"\.[0-9a-f]{16,64}(\.sha)?(?=\.[a-z0-9]+$)", re.I)

# Extensions FreeType can actually load, which is what libass is built on. Web font
# wrappers are deliberately absent - see `UNUSABLE_SUFFIXES`.
FONT_SUFFIXES = {
    ".ttf": "application/x-truetype-font",
    ".otf": "application/vnd.ms-opentype",
    ".ttc": "font/collection",
    ".otc": "font/collection",
}

# WOFF/WOFF2 are compressed wrappers meant for CSS `@font-face`, which is the form a
# streaming service's player loads its fonts in. FreeType has no decoder for either,
# so attaching one directly produces a file every player ignores - but the wrapper is
# only a container around ordinary sfnt tables, so unwrapping it back to ttf/otf is
# lossless and gives a font that works everywhere.
CONVERTIBLE_SUFFIXES = {".woff", ".woff2"}

# EOT is an Internet Explorer format with no path to a usable font here.
UNUSABLE_SUFFIXES = {".eot"}

# Converted web fonts are cached in this subdirectory of the fonts folder, so the
# unwrapping happens once per file rather than once per mux, and the user's own
# folder stays as they left it.
CONVERTED_DIRNAME = ".converted"

# Families every player already has, or that are aliases rather than real files.
# Attaching these bloats the mux for no benefit.
STOCK_FAMILIES = {
    "arial", "helvetica", "times new roman", "times", "courier new", "courier",
    "verdana", "tahoma", "georgia", "trebuchet ms", "segoe ui", "calibri",
    "sans-serif", "serif", "monospace", "cursive", "fantasy", "default",
}

# `Style: Name,Fontname,Fontsize,...` - the family is the second field.
ASS_STYLE = re.compile(r"^\s*Style\s*:\s*[^,]*,\s*([^,]*)", re.M | re.I)
# `\fnSome Font` runs until the next override or the end of the block.
ASS_FONT_OVERRIDE = re.compile(r"\\fn([^\\}]+)")


def _decode_name(data, platform_id, encoding_id):
    if platform_id == 3 or (platform_id == 0):
        # Windows and Unicode platforms both store UTF-16BE.
        try:
            return data.decode("utf-16-be").strip("\x00").strip()
        except UnicodeDecodeError:
            return ""
    try:
        return data.decode("mac-roman" if platform_id == 1 else "latin-1").strip()
    except (UnicodeDecodeError, LookupError):
        return ""


def _read_name_table(fd, offset):
    """Pull the family names out of one sfnt font's `name` table."""
    fd.seek(offset)
    header = fd.read(12)
    if len(header) < 12:
        return set()
    num_tables = struct.unpack(">H", header[4:6])[0]

    name_offset = None
    for _ in range(num_tables):
        record = fd.read(16)
        if len(record) < 16:
            return set()
        tag, _checksum, table_offset, _length = struct.unpack(">4sIII", record)
        if tag == b"name":
            name_offset = table_offset
            break
    if name_offset is None:
        return set()

    fd.seek(name_offset)
    head = fd.read(6)
    if len(head) < 6:
        return set()
    _format, count, strings_offset = struct.unpack(">HHH", head)

    records = fd.read(12 * count)
    names = set()
    for index in range(count):
        chunk = records[index * 12:(index + 1) * 12]
        if len(chunk) < 12:
            break
        platform_id, encoding_id, _language_id, name_id, length, str_offset = \
            struct.unpack(">HHHHHH", chunk)
        # 1 is the family name, 16 the typographic family. A file styled as
        # "Foo Bold" has family "Foo Bold" but typographic family "Foo", and ASS
        # may reference either, so both are collected.
        if name_id not in (1, 16):
            continue
        fd.seek(name_offset + strings_offset + str_offset)
        value = _decode_name(fd.read(length), platform_id, encoding_id)
        if value:
            names.add(value)
    return names


def font_families(path):
    """
    Return every family name a font file answers to.

    Falls back to the filename if the font cannot be parsed, so an unreadable file
    is still usable when its name happens to match.
    """
    try:
        with open(path, "rb") as fd:
            tag = fd.read(4)
            if tag == b"ttcf":
                # A collection: a count, then an offset per packed font.
                count = struct.unpack(">I", fd.read(8)[4:8])[0]
                offsets = struct.unpack(f">{count}I", fd.read(4 * count))
                names = set()
                for offset in offsets:
                    names |= _read_name_table(fd, offset)
                return names
            return _read_name_table(fd, 0)
    except (OSError, struct.error, ValueError) as e:
        log.debug(" - Could not read font names from %s: %s", path, e)
        return {os.path.splitext(os.path.basename(path))[0]}


def fonts_used_by(ass_path):
    """Return the font families an ASS file references, from its styles and overrides."""
    try:
        with open(ass_path, "r", encoding="utf-8-sig", errors="replace") as fd:
            text = fd.read()
    except OSError as e:
        log.warning(" - Could not read %s to check its fonts: %s", ass_path, e)
        return set()

    families = set(ASS_STYLE.findall(text)) | set(ASS_FONT_OVERRIDE.findall(text))
    # A leading `@` requests the vertical-writing variant of the same family.
    return {f.strip().lstrip("@").strip() for f in families if f.strip().lstrip("@").strip()}


def _is_usable_family(name):
    """
    Whether a font's own name table gives something libass could match on.

    Netflix and HBO Max ship theirs stripped - one reports `.\\x7f`, the other nothing
    at all - which leaves the file unreferenceable until it is renamed.
    """
    if not name or not name.strip(". \t"):
        return False
    return all(ch.isprintable() for ch in name)


def _set_family(font, family):
    """Give a font a family name, on both the Windows and Mac name records."""
    postscript = re.sub(r"[^A-Za-z0-9]", "", family) or "Font"
    subfamily = font["name"].getDebugName(2)
    if not _is_usable_family(subfamily):
        subfamily = "Regular"
    full = family if subfamily.lower() == "regular" else f"{family} {subfamily}"
    for name_id, value in ((1, family), (2, subfamily), (4, full), (6, postscript)):
        # 3/1/0x409 is Windows/Unicode/en-US, 1/0/0 is Mac/Roman/English. Players read
        # one or the other, so both are written.
        font["name"].setName(value, name_id, 3, 1, 0x409)
        font["name"].setName(value, name_id, 1, 0, 0)


def convert_web_font(path, cache_dir, family_hint=None):
    """
    Unwrap a WOFF/WOFF2 into a plain ttf/otf that FreeType can render.

    `family_hint` renames the font when its own name table is unusable, which is the
    only way a stripped font can be referenced from an ASS style.

    The result is cached in `cache_dir` and reused until the source changes. Returns
    the converted path, or None if the font could not be read.
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        log.warning(
            " - Cannot unwrap %s: fontTools is not installed (pip install fonttools)",
            os.path.basename(path)
        )
        return None

    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as e:
        log.warning(" - Could not create the converted font cache %s: %s", cache_dir, e)
        return None

    for suffix in (".ttf", ".otf"):
        cached = os.path.join(cache_dir, stem + suffix)
        if os.path.exists(cached) and os.path.getmtime(cached) >= os.path.getmtime(path):
            return cached

    try:
        font = TTFont(path)
        # A web font wraps either TrueType or PostScript outlines, and the extension
        # has to follow whichever it turned out to be.
        suffix = ".otf" if "CFF " in font or "CFF2" in font else ".ttf"
        target = os.path.join(cache_dir, stem + suffix)
        if family_hint and not _is_usable_family(font["name"].getDebugName(1)):
            log.info(
                " + %s has no usable family name, naming it %r",
                os.path.basename(path), family_hint
            )
            _set_family(font, family_hint)
        # Dropping the flavor is what turns the wrapper back into a bare sfnt.
        font.flavor = None
        font.save(target)
    except ImportError as e:
        # fontTools defers the brotli import until a woff2 actually needs decoding.
        log.warning(
            " - Cannot unwrap %s: %s (pip install brotli)", os.path.basename(path), e
        )
        return None
    except Exception as e:
        log.warning(" - Could not unwrap %s: %s", os.path.basename(path), e)
        return None

    log.info(" + Unwrapped %s to %s", os.path.basename(path), os.path.basename(target))
    return target


def _fetch_font(url, referer=None, timeout=30):
    """
    Download one font, returning its bytes or None.

    Falls back to curl_cffi's browser TLS fingerprint when a plain request is refused,
    which is what Cloudflare-fronted hosts need.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "Accept": "application/font-woff2;q=1.0,application/font-woff;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "font",
        "Sec-Fetch-Mode": "cors",
    }
    if referer:
        headers["Referer"] = referer

    try:
        import requests
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code == 200 and response.content[:4] in FONT_MAGIC:
            return response.content
        status = response.status_code
    except Exception as e:
        status = f"{type(e).__name__}: {e}"

    try:
        from curl_cffi import requests as impersonated
        response = impersonated.get(
            url, headers=headers, timeout=timeout, impersonate="chrome124"
        )
        if response.status_code == 200 and response.content[:4] in FONT_MAGIC:
            return response.content
        status = f"{status}, then {response.status_code} impersonating a browser"
    except ImportError:
        pass
    except Exception as e:
        status = f"{status}, then {type(e).__name__} impersonating a browser"

    log.warning(" - Could not fetch %s (%s)", url.rsplit("/", 1)[-1], status)
    return None


def _local_name(url):
    """Filename to cache a font URL under, with any CDN content hash removed."""
    name = url.rsplit("/", 1)[-1].split("?")[0]
    return URL_HASH.sub("", name)


def _service_sources(source):
    if not source:
        return None
    for key, entry in FONT_SOURCES.items():
        if key.lower() == str(source).lower():
            return entry
    return None


def ensure_service_fonts(source, directory):
    """
    Make sure the fonts a service's player uses are present in `directory`.

    Each file is fetched at most once ever - anything already cached is left alone -
    so this costs nothing after the first run. Returns True if anything new arrived.
    """
    entry = _service_sources(source)
    if not entry or not entry.get("fonts"):
        return False

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        log.warning(" - Could not create the fonts directory %s: %s", directory, e)
        return False

    cache_dir = os.path.join(directory, CONVERTED_DIRNAME)
    added = False
    for family, url in entry["fonts"]:
        target = os.path.join(directory, _local_name(url))
        suffix = os.path.splitext(target)[1].lower()

        if not os.path.exists(target):
            data = _fetch_font(url, entry.get("referer"))
            if not data:
                continue
            try:
                with open(target, "wb") as fd:
                    fd.write(data)
            except OSError as e:
                log.warning(" - Could not save %s: %s", os.path.basename(target), e)
                continue
            log.info(" + Fetched %s for %s", os.path.basename(target), source)
            added = True

        # Convert and/or rename now, while the family this file is meant to provide is
        # still known - the directory scan later on has no way to work that out.
        if suffix in CONVERTIBLE_SUFFIXES:
            if convert_web_font(target, cache_dir, family_hint=family):
                added = True
        elif _rename_font(target, cache_dir, family):
            added = True

    return added


def _rename_font(path, cache_dir, family):
    """Write a copy of an already-usable font format under a corrected family name."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return False

    target = os.path.join(cache_dir, os.path.basename(path))
    if os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(path):
        return False
    try:
        os.makedirs(cache_dir, exist_ok=True)
        font = TTFont(path)
        if _is_usable_family(font["name"].getDebugName(1)):
            return False
        log.info(
            " + %s has no usable family name, naming it %r",
            os.path.basename(path), family
        )
        _set_family(font, family)
        font.save(target)
    except Exception as e:
        log.warning(" - Could not rename %s: %s", os.path.basename(path), e)
        return False
    return True


def index_font_directory(directory):
    """
    Map every family name found in `directory` to the files providing it.

    A family maps to a list rather than one file, because weights ship separately -
    Regular and Bold are two files sharing one family name, and attaching only one of
    them leaves the player synthesising the other.
    """
    index = {}
    if not directory or not os.path.isdir(directory):
        return index

    cache_dir = os.path.join(directory, CONVERTED_DIRNAME)
    # Previously converted web fonts are indexed alongside the originals, so a font
    # keeps working even if fontTools later becomes unavailable.
    candidates = [os.path.join(directory, e) for e in sorted(os.listdir(directory))]
    if os.path.isdir(cache_dir):
        candidates += [os.path.join(cache_dir, e) for e in sorted(os.listdir(cache_dir))]

    for path in candidates:
        entry = os.path.basename(path)
        if not os.path.isfile(path):
            continue
        suffix = os.path.splitext(entry)[1].lower()
        if suffix in UNUSABLE_SUFFIXES:
            log.warning(
                " - Ignoring %s: %s cannot be converted to a usable font", entry, suffix
            )
            continue
        if suffix in CONVERTIBLE_SUFFIXES:
            path = convert_web_font(path, cache_dir)
            if not path:
                continue
            entry = os.path.basename(path)
        elif suffix not in FONT_SUFFIXES:
            continue
        for family in font_families(path) | {os.path.splitext(entry)[0]}:
            paths = index.setdefault(family.lower(), [])
            if path not in paths:
                paths.append(path)
    return index


def _charmap(path):
    """Every codepoint a font file can render."""
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(path, fontNumber=0, lazy=True)
        codepoints = set()
        for table in font["cmap"].tables:
            codepoints |= set(table.cmap)
        return codepoints
    except Exception as e:
        log.debug(" - Could not read the character map of %s: %s", path, e)
        return None


def family_covers(family, text, directory, threshold=0.99):
    """
    Whether `family` can actually render `text`.

    The web fonts a player ships cover the scripts that player's UI needs and nothing
    more - none of them have Hebrew, Thai or CJK, and some lack Cyrillic. Naming one
    on a subtitle it cannot render turns the track into boxes, so the caller uses this
    to fall back to a font the system will resolve instead.

    Unknown fonts pass: a family that is not in the directory is one the viewer's own
    system supplies, and second-guessing that would be worse than leaving it be.
    """
    paths = index_font_directory(directory).get(str(family).lower())
    if not paths:
        return True

    covered = set()
    for path in paths:
        codepoints = _charmap(path)
        if codepoints is None:
            return True  # unreadable, so no grounds to override the choice
        covered |= codepoints

    # Whitespace and the control characters ASS uses are never in a cmap and are not
    # drawn either, so counting them would fail every font.
    wanted = {ord(c) for c in text if c.strip() and c.isprintable()}
    if not wanted:
        return True

    missing = wanted - covered
    return (len(wanted) - len(missing)) / len(wanted) >= threshold


def resolve_fonts(ass_paths, directory, skip_stock=True):
    """
    Work out which font files to attach for a set of ASS subtitles.

    Returns (attachments, missing): a sorted list of (path, mime_type, filename) with
    each file listed once however many subtitles want it, and the families that had no
    file to back them.
    """
    wanted = set()
    for path in ass_paths:
        wanted |= fonts_used_by(path)

    if skip_stock:
        wanted = {f for f in wanted if f.lower() not in STOCK_FAMILIES}
    if not wanted:
        return [], set()

    index = index_font_directory(directory)

    attachments = {}
    missing = set()
    for family in wanted:
        paths = index.get(family.lower())
        if not paths:
            missing.add(family)
            continue
        for path in paths:
            # Keyed by path, so a file serving two families still attaches once.
            attachments[path] = (
                path,
                FONT_SUFFIXES[os.path.splitext(path)[1].lower()],
                os.path.basename(path),
            )

    return sorted(attachments.values(), key=lambda a: a[2].lower()), missing
