#!/usr/bin/env python3
"""
Example client for the web UI's external API (see API.md). Standard library only.

    set VT_API_KEY=vt_...                     (Linux/macOS: export VT_API_KEY=vt_...)
    python vt_api_client.py Netflix 80057281 --codec av1 --range hybridplus --quality 4k --wanted S01E01
    python vt_api_client.py Netflix 80057281 --wanted S01 --vpn id      (through the VPN, in Indonesia)
    python vt_api_client.py --progress                (what is downloading, every 5 seconds)

It asks for the check, shows what's available, and queues the download after a "y".
A Discord bot does the same three calls: create_request() -> show "message" -> confirm()
or decline(). discord_bot.py next to this file is such a bot.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


class VTApi:
    def __init__(self, url="http://127.0.0.1:8788", key=None):
        self.url = url.rstrip("/") + "/api/v1"
        self.key = key or os.environ.get("VT_API_KEY", "")

    def call(self, method, path, body=None, timeout=60):
        request = urllib.request.Request(
            self.url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            try:
                message = json.load(e).get("error")
            except ValueError:
                message = None
            raise RuntimeError(message or f"HTTP {e.code}") from None

    def create_request(self, service, link, **options):
        """options: codec, range, quality, wanted, audio_lang, sub_lang, fallback, check_keys,
        vpn (True), location (two-letter country)"""
        body = {"service": service, "link": link}
        body.update({k: v for k, v in options.items() if v is not None})
        return self.call("POST", "/requests", body)

    def wait(self, request, on_status=None):
        """Poll until the check is through. A check can take a few minutes, longer if it
        has to wait for a download of the same service."""
        message = None
        while request["status"] in ("queued", "waiting", "checking"):
            if on_status and request["message"] != message:
                message = request["message"]
                on_status(message)
            request = self.call("GET", f"/requests/{request['id']}?wait=25")
        return request

    def confirm(self, request_id, option=None, wanted=None):
        body = {k: v for k, v in (("option", option), ("wanted", wanted)) if v}
        return self.call("POST", f"/requests/{request_id}/confirm", body)

    def decline(self, request_id):
        return self.call("DELETE", f"/requests/{request_id}")

    def vpn(self):
        """Whether the VPN is ready, and the locations a request can ask for."""
        return self.call("GET", "/vpn")

    def progress(self):
        """What is downloading right now."""
        return self.call("GET", "/progress")

    def follow_progress(self, interval=5):
        """Yields progress() every `interval` seconds, as long as the web UI sends it."""
        request = urllib.request.Request(f"{self.url}/progress/stream?interval={interval}",
                                         headers={"Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(request, timeout=interval + 60) as response:
                for line in response:
                    yield json.loads(line)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}") from None


def main():
    parser = argparse.ArgumentParser(description="Ask the VT web UI for a download.")
    parser.add_argument("service", nargs="?", help="e.g. Netflix, Amazon, nf, amzn")
    parser.add_argument("link", nargs="?", help="link or ID (Netflix: the number, Amazon: ASIN or gti)")
    parser.add_argument("--progress", action="store_true", help="show what is downloading")
    parser.add_argument("--interval", type=int, default=5, help="seconds between --progress updates")
    parser.add_argument("--codec", help="h264, h265, av1, vp9 (default: H.265, else H.264)")
    parser.add_argument("--range", default="sdr", help="sdr, hdr10, dv, dvhdr, hlg, hybrid, hybridplus")
    parser.add_argument("--quality", default="best", help="best, 4k, 1080, 720, ...")
    parser.add_argument("--wanted", help="e.g. S01, S01E03, S01-S02")
    parser.add_argument("--audio-lang", help="e.g. de,en")
    parser.add_argument("--sub-lang", help="e.g. de,en")
    parser.add_argument("--vpn", metavar="COUNTRY", help="check and download through the VPN, e.g. id or us")
    parser.add_argument("--url", default=os.environ.get("VT_API_URL", "http://127.0.0.1:8788"))
    args = parser.parse_args()

    api = VTApi(args.url)
    if not api.key:
        sys.exit("Set VT_API_KEY to the key shown under Settings > External API.")
    if not args.progress and not (args.service and args.link):
        parser.error("give a service and a link, or --progress")
    try:
        if args.progress:
            try:
                for update in api.follow_progress(args.interval):
                    print(update["text"], flush=True)
            except KeyboardInterrupt:
                pass
            return 0
        request = api.create_request(
            args.service, args.link, codec=args.codec, range=args.range, quality=args.quality,
            wanted=args.wanted, audio_lang=args.audio_lang, sub_lang=args.sub_lang,
            vpn=True if args.vpn else None, location=args.vpn)
        request = api.wait(request, on_status=print)
        print(request["message"])
        for warning in request["warnings"]:
            print(f"  ! {warning}")
        if not request["options"] or request["status"] not in ("awaiting_confirmation", "unavailable"):
            return 1

        for number, option in enumerate(request["options"], 1):
            marker = "  <- offered" if request["offer"] and option["id"] == request["offer"]["id"] else ""
            print(f"  {number}. {option['label']}{marker}")
        answer = input("Download? [y = the offered one / number / n] ").strip().lower()
        option = None
        if answer.isdigit() and 1 <= int(answer) <= len(request["options"]):
            option = request["options"][int(answer) - 1]["id"]
        elif answer not in ("y", "yes", "j", "ja") or not request["offer"]:
            api.decline(request["id"])
            print("Nothing queued.")
            return 0
        print(api.confirm(request["id"], option=option)["message"])
        return 0
    except (RuntimeError, urllib.error.URLError) as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
