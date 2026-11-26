# Changelog

## V1.0.6.1

- Docker: updates install from the Update tab and stay when the container is recreated; a rebuild is only needed for releases with new Python packages or tools. Needs one `docker compose up -d --build` first

## V1.0.6

- Phone layout: on a phone, the pages get a bottom navigation bar, larger controls, the Download buttons always within reach, the queue as cards and full-screen dialogs. On a computer nothing changes
- VPN switch with a country list on the Download page: the check and the whole download go through your VPN provider (NordVPN, Surfshark or your own HTTP / HTTPS proxies). It's remembered per service, pinned shows keep their route, and the Queue tab can change it
- Command line: `--proxy-downloads` sends the tracks through the proxy too; `.prd` PlayReady devices are loaded
- Credentials tab: service logins and cookies in one place, plus a VPN section with a connection test
- Settings tab rebuilt: everything in vinetrimmer.yml (downloader per service, devices found in your devices folder, remote CDMs, key vaults, file names, subtitles, default options, profiles, headers) and a folder picker for the download, temp and fonts folders
- New Services tab: what each service needs, replacing its files, or adding a new service by uploading its .py and .yml
- Download, Console, Credentials, Settings and Update pages use the whole window
- External API: `vpn` and `location` in a request, and `GET /api/v1/vpn`
- The web UI refuses changes sent by other websites
- Fixed: a track downloaded with N_m3u8DL-RE was downloaded again with aria2c, which overwrote it with the playlist text
- Fixed: a proxy login containing characters such as `@` didn't work, and the login no longer appears in logs

## 1.0.5
- Discover "All": browse everything on the chosen services, in JustWatch's order or sorted by title A to Z / Z to A (up to 40,000 titles)
- Discover: colored badges show whether an offer is included in the subscription, free, an extra channel or rent / buy, and a filter shows only subscription & free or only rent & buy; DVD / Blu-ray offers are marked as physical discs, with only a link to the shop

## 1.0.4

- YouTube update: current yt-dlp (needed for AV1 and dubbed audio tracks); run `poetry install` after updating. Python 3.11 or newer is now required
- YouTube tracks are downloaded by yt-dlp itself, which is much faster
- Fixed the YouTube auto-crop from 1.0.3, which never detected the black bars
- A failed download no longer ends in a "file not found" error 
- New Discover tab (JustWatch): see what's new or popular on your services, search, and send a movie, season or episode to the Download page or straight to the queue
- External API with its own port and key: a script or Discord bot asks for a service, title and quality, gets what's available (SDR as fallback for missing HDR), confirms it, and follows the download progress (see API.md; example client and Discord bot in examples/)
- Docker setup, runs on any system with Docker (see DOCKER.md)
- New browser icon
- The "a download is running" warning on checks now only appears for the same service, and can be turned off in Settings

## 1.0.3

- YouTube cropping added
- Que pictures are now bigger
- Que doesn't jitter anymore
- Que pinned option expended
- Downloader experimental updated bugs with wrong showing codecs and flas positives
- Better logging options
- Cli/Consol shows now total downloaded and how much is still will download.
- Posters get now set after a download starts (If not checked with the experimental Availability checker already)
- Posters now in Consol/Cli

## 1.0.2

- Fixed a Problem where LInks could not be read as the internal code read it diffrently.
- Fixed a bug where prefered language was not set as the brwser defaulted back to nothing.
- Fixed that the first 1.0.0 release was not in the list of the updates to revert back to.

## 1.0.1

Updated how a Error gets handled so that a retry is possible even if part of it was downloaded (wich lead to a succses)

## 1.0.0

First Release of the UI

- Expermintal settings for Checking and pictures
- Que
- Download view Bars/CLI/Bars and CLI
- Experimental credential managment

Many more features are planed.
