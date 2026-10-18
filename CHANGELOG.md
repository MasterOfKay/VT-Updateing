# Changelog

## 1.0.0

First versioned release of the web UI.

- Queue: saved to disk (survives crashes / restarts), run order view, pause after the current download,
  retry with resume at the failed episode, optional auto retry, temp cleanup after failed downloads.
- Console: download progress / CLI / both view, header with name + poster, "Stop after this episode"
  with Continue for shows.
- Pinned shows: checked for new episodes on their own and downloaded; optional TVmaze air dates.
- Experimental availability check: codecs, ranges, resolutions, audio and subtitles before downloading
  (Netflix: per codec/range, key check for 4K).
- Update tab: installed version, published versions from GitHub, optional pre-release builds.
- vinetrimmer `dl`: `--first-title`, `--list-json`, `--audio-pick`, `--stop-file`.
