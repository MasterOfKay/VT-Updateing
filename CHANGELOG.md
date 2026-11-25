# Changelog

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
