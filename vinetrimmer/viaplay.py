from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import click

from vinetrimmer.objects import MenuTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Viaplay(BaseService):
    """
    Service code for Viaplay's on-demand streaming service
    (https://viaplay.se, .no, .dk, .fi, .pl). 

    \b
    Authorization: Credentials
    Security: L3 for UHD

    Author: @MasterOfKay

    THX to Mike for a LveTV code, that helped getting login and api calls working super fast. 
    Also THX to Chris20 for showing me a 4K movie so i could patch 4K request and War_Minister_DK for also api endpoints and infos.
    Without them it would have taken mutch longer to creat this service code.

    This code was only tested with a swedisch (.sv) account. The rest should work the same, but i can't test it.
    """

    ALIASES = ["VPLAY", "Viaplay"]

    TITLE_RE = re.compile(
        r"https?://(?:www\.)?viaplay\.(?P<country>se|no|dk|fi|pl|is|ee|lv|lt)(?P<path>/[^\s?#]+)"
    )

    @staticmethod
    @click.command(name="Viaplay", short_help="https://viaplay.se")
    @click.argument("title", type=str)
    @click.option("-c", "--country", default=None,
                  help="Viaplay market (se, no, dk, fi, pl, is, ee, lv, lt). "
                       "Auto-detected from the title URL if omitted, required if TITLE is a raw guid.")
    @click.option("--device-key-prefix", default=None,
                  help="Override the deviceKey prefix used for the stream/manifest request only "
                       "(e.g. try 'androidtv-' or 'atvexo-' instead of the default 'androidexo-' "
                       "to see if a TV-class profile is offered higher quality/HDR). Experimental.")
    @click.option("--device-name", default=None,
                  help="Override the deviceName used for the stream/manifest request only. Experimental.")
    @click.option("--device-type", default=None,
                  help="Override the deviceType used for the stream/manifest request only "
                       "(e.g. try 'Tv' or 'SmartTv' instead of the default 'Mobile'). Experimental.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Viaplay(ctx, **kwargs)

    def __init__(self, ctx, title: str, country: str = None,
                 device_key_prefix: str = None, device_name: str = None, device_type: str = None):
        self.title = title
        self.country = country.lower() if country else None
        self.path = None

        self.stream_device_key_prefix = device_key_prefix
        self.stream_device_name = device_name
        self.stream_device_type = device_type

        super().__init__(ctx)

        self.cdm = ctx.obj.cdm

        self.auth = None
        self.license_api = None
        self.stream = None

        self.configure()


    def get_titles(self):
        node = self.get_node(self.path)

        # If the node is a season-list, collect all embedded products (episodes) and return them
        products = self._collect_season_products(node)
        if products:
            return self._dedupe_titles(products)

        product = self._extract_product(node)
        if not product:
            raise self.log.exit(" - Unable to find any playable content at this URL")

        return [self._product_to_title(product)]

    def _dedupe_titles(self, products):
        seen_guids = set()
        titles = []
        for p in products:
            guid = p.get("system", {}).get("guid")
            if guid in seen_guids:
                continue
            seen_guids.add(guid)
            titles.append(self._product_to_title(p))
        return titles

    def _blocks(self, node):
        return (node.get("_embedded") or {}).get("viaplay:blocks") or []

    def _extract_product(self, node):
        for block in self._blocks(node):
            if block.get("type") == "product":
                product = (block.get("_embedded") or {}).get("viaplay:product")
                if product:
                    return product
        if node.get("type") in ("movie", "episode") and "system" in node:
            return node
        return None

    def _collect_season_products(self, node):
        """
        Get all Episodes and Seasos from the Block list that Viaplay returns.
        """
        products = []

        for block in self._blocks(node):
            if block.get("type") != "season-list":
                continue

            embedded = (block.get("_embedded") or {}).get("viaplay:products")
            if embedded is None:
                href = (block.get("_links") or {}).get("self", {}).get("href")
                if not href:
                    continue
                device_key = self.config["device"]["device_key_prefix"] + self.country
                season_path = re.sub(rf"^https?://[^/]+/{re.escape(device_key)}", "", href).split("{", 1)[0]
                season_node = self.get_node(season_path)
                embedded = self._collect_products(season_node)

            products.extend(embedded or [])

        return products

    @staticmethod
    def _strip_query_param(url, key):
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != key.lower()]
        return urlunsplit(parts._replace(query=urlencode(query)))

    def get_tracks(self, title: Title) -> Tracks:
        headers = {
            "connection": "Keep-Alive",
            "host": f"play.viaplay.{self.country}",
            "nent-experiments": "product_page",
            "user-agent": self.config["device"]["user_agent"],
            "x-user-agent": self.config["device"]["user_agent"],
        }

        device_key_prefix = self.stream_device_key_prefix or self.config["device"]["device_key_prefix"]
        device_name = self.stream_device_name or self.config["device"]["device_name"]
        device_type = self.stream_device_type or self.config["device"]["device_type"]

        params = {
            "deviceId": self.config["device"]["device_id"],
            "deviceName": device_name,
            "deviceType": device_type,
            "userAgent": self.config["device"]["user_agent"],
            "deviceKey": device_key_prefix + self.country,
            "cse": "true",
            "guid": title.id,
            "defaultAvailabilityContext": "svod",
        }

        response = self.session.get(
            url=f"https://play.viaplay.{self.country}/api/stream/byguid",
            cookies=self.auth["cookies"],
            headers=headers,
            params=params
        )

        try:
            stream = response.json()
        except ValueError:
            raise self.log.exit(f" - Unexpected stream response: {response.text[:300]}")

        self.stream = stream

        links = stream.get("_links", {})
        playlist = links.get("viaplay:encryptedPlaylist") or links.get("viaplay:playlist")
        if not playlist:
            raise self.log.exit(f" - No playable manifest returned: {json.dumps(stream)[:300]}")

        if "viaplay:encryptedPlaylist" not in links:
            self.log.warning(
                " + Only an unencrypted/preview manifest was returned - this account may not have "
                "an active entitlement for this title (subtitles may still be usable regardless)."
            )

        self.license_api = (links.get("viaplay:widevineLicense") or {}).get("href")

        manifest_url = playlist["href"]
        quality_filter = next(iter(re.findall(r"[?&]filter=([^&]+)", manifest_url)), None)
        if quality_filter:
            # Drop the avc 1080p querry to get EAC3 audio and if avalaible H265 and p to 4k. 
            # No auth required for filtering. Funny
            self.log.info(f" + Removing server-side manifest quality filter: {quality_filter[:150]}")
            manifest_url = self._strip_query_param(manifest_url, "filter")

        tracks = Tracks.from_mpd(
            url=manifest_url,
            session=self.session,
            source=self.ALIASES[0]
        )

        for track in tracks:
            track.needs_proxy = True

        return tracks

    def get_chapters(self, title: Title) -> list[MenuTrack]:
        # stream["chapters"] is a flat list of {title, startTime (ms), endTime}. Most
        # entries are actually former ad-break cue points ("ad-default-N"). I tried to ignore them and only add what seems real, wich is mostly chpter end credits
        raw_chapters = self.stream.get("chapters") if self.stream else None
        if not raw_chapters:
            self.log.debug(" + No chapter/marker data found in the stream response")
            return []

        LABEL_MAP = {
            "endcredits": "Credits",
            "credits": "Credits",
            "recap": "Recap",
            "intro": "Intro",
        }

        entries = []
        for marker in raw_chapters:
            ms = marker.get("startTime")
            name = LABEL_MAP.get(str(marker.get("title") or "").lower())
            if ms is None or name is None:
                continue
            entries.append((int(ms), name))

        if not entries:
            return []

        entries.sort(key=lambda x: x[0])
        if entries[0][0] > 0:
            entries.insert(0, (0, "Scene"))

        return [
            MenuTrack(number=i + 1, title=name, timecode=self._ms_to_timecode(ms))
            for i, (ms, name) in enumerate(entries)
        ]

    @staticmethod
    def _ms_to_timecode(ms: int) -> str:
        s = ms / 1000
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        s = s % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    def certificate(self, challenge, **_):
        return self.license(challenge)

    def license(self, challenge: bytes, **_) -> bytes:
        if not self.license_api:
            raise self.log.exit(" - No license URL available - was get_tracks called first?")

        return self.session.post(
            url=self.license_api,
            headers={
                "connection": "Keep-Alive",
                "content-type": "application/octet-stream",
                "user-agent": self.config["device"]["license_user_agent"],
            },
            data=challenge
        ).content

    def configure(self) -> None:
        if not self.credentials:
            raise self.log.exit(" - Viaplay requires Credentials for Authentication")

        self._resolve_target()
        self.log.info(f" + Using Viaplay market: {self.country}")

        self.auth = self.get_auth()

    def _resolve_target(self):
        match = self.TITLE_RE.search(self.title)
        if match:
            self.country = match.group("country")
            self.path = match.group("path")
            return

        if not self.country:
            raise self.log.exit(
                " - Unable to determine the Viaplay market - pass a full viaplay.<market> URL, "
                "or use --country/-c together with a raw guid"
            )
        # assume title is a raw content guid
        self.path = f"/product/{self.title}"

    def get_auth(self):
        cache_path = self.get_cache(f"auth_{self.country}_{self.credentials.sha1}.json")

        if os.path.isfile(cache_path):
            with open(cache_path, encoding="utf-8") as fd:
                cached = json.load(fd)
            if self._verify_auth(cached):
                self.log.info(" + Using cached session")
                return cached
            self.log.info(" + Cached session is no longer valid, logging in again")

        auth = self.login()

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fd:
            json.dump(auth, fd)

        return auth

    def _verify_auth(self, auth):
        try:
            device_key = self.config["device"]["device_key_prefix"] + self.country
            response = self.session.get(
                url=f"https://content.viaplay.{self.country}/{device_key}",
                cookies=auth["cookies"],
                headers={
                    "host": f"content.viaplay.{self.country}",
                    "user-agent": self.config["device"]["user_agent"],
                    "x-user-agent": self.config["device"]["user_agent"],
                },
                params={"profileId": auth["userData"]["userId"]}
            )
            return response.ok
        except Exception:
            return False

    def login(self):
        self.log.info(" + Logging in...")

        login_host = f"login.viaplay.{self.country}"
        device_key = self.config["device"]["device_key_prefix"] + self.country

        headers = {
            "connection": "Keep-Alive",
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "host": login_host,
            "user-agent": self.config["device"]["user_agent"],
            "x-user-agent": self.config["device"]["user_agent"],
        }

        params = {
            "deviceKey": device_key,
            "returnurl": f"https://content.viaplay.{self.country}/{device_key}?appStore=default",
            "username": self.credentials.username,
            "persistent": "false",
        }

        data = {
            "password": self.credentials.password,
        }

        response = self.session.post(
            f"https://{login_host}/api/login/v1",
            params=params,
            headers=headers,
            data=data
        )

        try:
            auth = response.json()
            auth["userData"]["accessToken"]
        except (ValueError, KeyError):
            raise self.log.exit(f" - Login failed: {response.text[:300]}")

        auth["cookies"] = response.cookies.get_dict()

        self.log.info(f" + Logged in, account country: {auth['userData']['accountCountry']}")

        return auth

    def get_node(self, path):
        device_key = self.config["device"]["device_key_prefix"] + self.country

        headers = {
            "connection": "Keep-Alive",
            "host": f"content.viaplay.{self.country}",
            "nent-experiments": "product_page",
            "user-agent": self.config["device"]["user_agent"],
            "x-user-agent": self.config["device"]["user_agent"],
        }

        params = {
            "dtg": "false",
            "profileId": self.auth["userData"]["userId"],
        }

        response = self.session.get(
            url=f"https://content.viaplay.{self.country}/{device_key}{path}",
            cookies=self.auth["cookies"],
            headers=headers,
            params=params
        )

        try:
            return response.json()
        except ValueError:
            raise self.log.exit(f" - Unexpected content response: {response.text[:300]}")

    def _collect_products(self, node, seen=None):
        """
        Recursively walk a Viaplay HAL+JSON content node collecting every embedded
        product that looks like a single playable item (a movie or an episode).

        Viaplay nests actual products under varying `_embedded` keys depending on
        the page type (`viaplay:blocks` -> `viaplay:products`, `viaplay:episodes`,
        `viaplay:seasons`). So it looks at all and uses what seems usable.
        """
        if seen is None:
            seen = []

        if isinstance(node, dict):
            if node.get("type") in ("movie", "episode") and "system" in node:
                seen.append(node)
                return seen
            for value in (node.get("_embedded") or {}).values():
                self._collect_products(value, seen)
        elif isinstance(node, list):
            for item in node:
                self._collect_products(item, seen)

        return seen

    def _product_to_title(self, product):
        content = product.get("content", {})
        system = product.get("system", {})
        original_lang = self.config.get("original_lang", {}).get(self.country, "en")

        if product.get("type") == "movie":
            return Title(
                id_=system["guid"],
                type_=Title.Types.MOVIE,
                name=content.get("title"),
                year=content.get("production", {}).get("year"),
                original_lang=original_lang,  # TODO: Don't assume, not exposed directly by the API
                source=self.ALIASES[0],
                service_data=product
            )

        series = content.get("series", {})
        return Title(
            id_=system["guid"],
            type_=Title.Types.TV,
            name=series.get("title") or content.get("originalTitle"),
            season=(series.get("season") or {}).get("seasonNumber"),
            episode=series.get("episodeNumber"),
            episode_name=content.get("title"),
            original_lang=original_lang,  # TODO: Don't assume, not exposed directly by the API
            source=self.ALIASES[0],
            service_data=product
        )
