# External API

Lets your own scripts (a Discord bot, a shortcut, ...) ask the web UI for downloads:
send a service, a link or ID and the quality you want, get back what's available, and
confirm it. The confirmed request is a normal job in the web UI's queue.

## Switching it on

1. Web UI → **Settings** → **External API** → *Enable the API* → *Save API settings*.
2. Copy the key shown there.

The API listens on its **own port** (default `8788`) and offers only the calls below, not
the web page. By default only programs on the same computer can reach it; choose
*Other computers too* if your bot runs elsewhere.

> Anyone with the key can make your computer log in to your accounts and download. The
> API speaks plain HTTP: over the internet, put it behind a reverse proxy with HTTPS, or
> use a VPN. Don't commit the key, and make a new one if it leaks.

Every call needs the key, as `Authorization: Bearer <key>` (or `X-API-Key: <key>`).
After 10 wrong keys within 5 minutes, further wrong attempts from that address get `429`.

## The flow

```
POST /api/v1/requests          -> status "queued" ... "checking"
GET  /api/v1/requests/<id>     -> status "awaiting_confirmation", "message": "... Download?"
POST /api/v1/requests/<id>/confirm   (yes)   -> in the download queue
DELETE /api/v1/requests/<id>         (no)
```

## Ask for a download

`POST /api/v1/requests`

```json
{
  "service": "Netflix",
  "link": "80057281",
  "codec": "av1",
  "range": "hybridplus",
  "quality": "4k",
  "wanted": "S01E01"
}
```

| Field | Meaning |
|---|---|
| `service` | Name or alias (`Netflix` / `nf`, `Amazon` / `amzn`, ...). `GET /api/v1/services` lists them. |
| `link` | Link or ID (`title` and `id` work too). Netflix: the number from the link. Amazon: ASIN or `amzn1.dv.gti.…`. Full Netflix / Amazon links are cut down to the ID. Other services: the link. |
| `codec` | `h265`, `h264`, `av1`, `vp9`. Left out: H.265 like the Download page (4K and HDR are only there or in AV1), and H.264 if a title has no H.265. With `hybridplus`: `av1`. |
| `range` | `sdr` (default), `hdr10`, `hlg`, `dv`, `dvhdr`, `hybrid`, `hybridplus` (Netflix + AV1) |
| `quality` | `best` (default), `4k`, `1440`, `1080`, `720`, ... |
| `wanted` | Shows: `S02`, `S02E05`, `S01-S03`. Without it, confirming downloads **every** episode. `season` / `episode` numbers work too. |
| `audio_lang`, `sub_lang` | e.g. `"de,en"` or `["de", "en"]` |
| `fallback` | What to try if the wanted range isn't there. Default `["sdr"]`. `[]`: nothing else. `["hdr10", "sdr"]`: this order. `[{"codec": "h265", "range": "sdr"}]`: another codec. |
| `check_keys` | Netflix 4K: also look whether the keys are in your key vaults (`"keys"` in the answer: `cached` / `missing`). |
| `wait` | Seconds (up to 600) to hold the answer until the check is done, instead of polling. |

### If the wanted quality isn't there

With the default fallback, any missing HDR variant becomes **SDR in the same codec: 4K if
there is one, otherwise 1080p**. Example: `av1` + `hybridplus` + `4k`

1. 4K AV1 Hybrid+ exists → offered (`"exact": true`).
2. Otherwise 4K AV1 SDR → offered (`"exact": false`).
3. Otherwise 1080p AV1 SDR.
4. Only if there's no SDR at all: Hybrid+ below 4K.

The codec is the one you ask for. `options` always lists everything the checks found, and
the message names the other possibilities of that codec (for example 1080p HDR next to a
4K SDR offer), so your bot can offer those instead.

On services other than Netflix, a 4K request also asks for the 4K manifest (Amazon only
sends it then), and a confirmed 4K download is queued with `-q`.

On Netflix, 1080p and below is downloaded with Widevine (always gets its keys). 4K needs
PlayReady, which only works if the keys are already in your key vaults.

## The answer

```json
{
  "id": "3f9c0d1e7a2b4c55",
  "status": "awaiting_confirmation",
  "message": "Show name: AV1 Hybrid+ (HDR10+ with DV) 2160p isn't available. Available instead: AV1 SDR 2160p. Download? (Also possible: AV1 SDR 1080p.)",
  "exact": false,
  "offer":   { "id": "av1:sdr:2160", "label": "AV1 SDR 2160p", "bitrate_kbps": 8000, "width": 3840, "height": 2160 },
  "options": [ { "id": "av1:sdr:2160", "...": "..." }, { "id": "av1:sdr:1080", "...": "..." } ],
  "title": { "name": "Show name", "year": 2024, "type": "TV", "poster": "https://...", "seasons": { "1": 8 } },
  "audio_languages": ["de", "en"],
  "subtitle_languages": ["de"],
  "warnings": [],
  "job_id": null
}
```

| `status` | Meaning |
|---|---|
| `queued`, `waiting`, `checking` | Not answered yet. `message` says why it waits. |
| `awaiting_confirmation` | `offer` can be confirmed (for 24 hours). |
| `unavailable` | Neither the wanted variant nor a fallback. You can still confirm one of `options`. |
| `failed` | The check didn't work (login, wrong ID, ...), see `message`. |
| `confirmed` | In the download queue, `job_id` is set. |
| `declined`, `expired` | Closed. |

Checks run one at a time. While a service is downloading, checks for **that** service wait
until it's done (can be turned off under Settings); other services are checked right away.

## Download progress

For a progress channel: what the queue is downloading right now (all downloads, also those
started in the web UI).

| Call | |
|---|---|
| `GET /api/v1/progress` | One snapshot. |
| `GET /api/v1/progress/stream?interval=5` | Stays open and sends one snapshot per line (JSON lines) every `interval` seconds (2-300), until you hang up. At most 5 at a time. |

```json
{
  "queue": "running",
  "text": "Show name (2024) [Netflix] S01E03 (episode 3 of 8) - 42% · 1/4 tracks · about 3m 10s left",
  "active": [ {
    "job_id": "…", "request_id": "…", "service": "Netflix", "title": "Show name", "year": 2024,
    "poster": "https://…", "episode": "S01E03", "episodes": { "current": 3, "total": 8 },
    "percent": 42.0, "eta_seconds": 190, "elapsed_seconds": 140, "tracks_done": 1, "tracks_total": 4,
    "tracks": [ { "type": "Video", "name": "…", "percent": 42.0, "state": "downloading", "eta_seconds": 190 } ]
  } ],
  "queued":   [ { "job_id": "…", "service": "Amazon", "title": "…" } ],
  "finished": [ { "job_id": "…", "title": "…", "status": "done", "finished_at": 1764000000 } ]
}
```

`text` is ready to post. `percent` is that of the video track of the current episode;
`eta_seconds` that of the slowest track still downloading. `finished` holds the last 5.

## The other calls

| Call | |
|---|---|
| `GET /api/v1/ping` | Test the key; returns the version. |
| `GET /api/v1/services` | Services and aliases. |
| `GET /api/v1/requests` | All requests, newest first. |
| `GET /api/v1/requests/<id>?wait=25` | One request, optionally waiting for the answer. |
| `POST /api/v1/requests/<id>/confirm` | Queue the offer. Body (optional): `{"option": "av1:sdr:1080", "wanted": "S01E02"}` |
| `DELETE /api/v1/requests/<id>` | Decline (also stops a running check). `POST .../decline` does the same. |
| `GET /api/v1/requests/<id>/job` | Download progress: `status`, `current_episode`, `episodes`. |

Errors come as `{"error": "..."}` with status 400, 401, 404, 409 or 429.

Requests are kept in memory: after restarting the web UI, send unconfirmed ones again.
Confirmed downloads are part of the saved queue.

## Examples

[`examples/vt_api_client.py`](examples/vt_api_client.py) needs only Python:

```bash
export VT_API_KEY=vt_...          # Windows: set VT_API_KEY=vt_...
python examples/vt_api_client.py Netflix 80057281 --codec av1 --range hybridplus --quality 4k --wanted S01E01
python examples/vt_api_client.py --progress --interval 5
```

### Discord bot

[`examples/discord_bot.py`](examples/discord_bot.py) does both channels: `/download` in a
request channel (it checks, then asks with *Download* / *No* buttons and a list of other
qualities) and one live-updating message in a progress channel.

```bash
pip install -U discord.py
export DISCORD_TOKEN=...  VT_API_KEY=vt_...
export VT_REQUEST_CHANNEL=123...  VT_PROGRESS_CHANNEL=456...  VT_ALLOWED_ROLE=Downloader
python examples/discord_bot.py
```

Set `VT_ALLOWED_ROLE`: whoever can use the command can make your accounts download.
