# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Release notes for GitHub Releases are taken from the matching `## [vX.Y.Z]` section below (see `.github/workflows/release.yml`).

## [v0.5.0] - 2026-10-27

### Added

- Files are now saved as `Artist - Title` instead of `uploader - title`. The uploader of a re-upload is a throwaway channel (`foleywarlocks - Сектор Газа - Туман.opus`), so the artist is taken from the track's own metadata when YouTube publishes it (`Acid Bath - Bleed Me an Ocean.opus`), otherwise from the video title (`Silent Circle - Egyptian Eyes.opus`); bracketed junk (`(Official Video)`, `[HD Remaster]`, `(2011)`) is dropped from the name. The `.png` cover and `.txt`/`.lrc` lyrics sidecars, the library rows and the `≡ Lyrics` re-fetch all use the same name. The same artist/title guess is now also the first lyrics search query, which improves matching for re-uploads. Files already in the library keep their old names.

### Fixed

- Jobs running at the same time no longer fight over one output file while they are being post-processed (a playlist that lists the same song twice, or official audio + lyric video of it). Such copies are now saved as `Artist - Title (2)`, ` (3)`… instead of one job failing with "Error opening output files".

## [v0.4.0] - 2026-10-25

### Added

- Lyrics alongside the audio: a "Save lyrics as .txt (+ .lrc when timed)" checkbox (single download and playlist modal, on by default). Writes `<same name>.txt` next to the audio file, plus `<same name>.lrc` when the source has timestamps. Sources are tried in order — lrclib.net, Genius, lyrics.ovh — and a match is only accepted when artist AND title agree with the track (duration is used as a tie-breaker), so a wrong song is never written; when nothing is found, no lyrics file is created.
- Lyrics now keep song structure tags — `[Intro]`, `[Verse 1]`, `[Pre-Chorus]`, `[Chorus]`, `[Bridge]`, `[Instrumental Break]`, `[Guitar Solo]`, `[Outro]` — and they are always written in English: source tags in another language (`Куплет 2`, `후렴`, `Estribillo`, `Pause instrumentale`) are translated, and page-title lines (`Текст песни «Туман»`, `Paroles de "California"`) are removed. Since only Genius publishes structure tags, a lyrics source without them (lrclib, lyrics.ovh) is now superseded by the Genius text when the match is confirmed; the timed `.lrc` from the other source is still kept. If no source has tags, none are invented.
- Lyrics lookups are cached in `downloads/.lyrics/`, so a re-download or a second format of the same track costs no extra requests; cutting a track copies its `.txt`/`.lrc`, and deleting a track deletes them with it.
- `≡ Lyrics` button on each library row re-fetches the lyrics of an already downloaded track (no audio re-download) — handy after the tag fix above; the file is left as is when nothing is found.
- Optional `"synth_tags": true` in `config.json` (off by default): when *no* source publishes structure tags for a track, `[Chorus]` is placed on the longest block repeated at least twice and the gaps get `[Verse N]`. Tracks without a repeating block are left without tags.

## [v0.3.1] - 2026-10-21

### Fixed

- The app no longer shows an outdated interface after an update: the page is now revalidated with the server on every load instead of being kept in the browser cache for hours (assets and API responses are still cached as before).

## [v0.3.0] - 2026-10-21

### Added

- Search inside the downloaded library — fulltext filter on filename, case- and diacritic-insensitive (`#libSearch`, ✕ to clear, Esc clears).
- Listen preview before downloading — 15 s clip streamed from the cheapest audio stream, no full download, no cookies needed.
- Preview songs inside the playlist download modal (play button per row).
- Waveform view in the audio cutter, with hover feedback.
- Staggered entrance animation for library rows.

### Changed

- Playlist download is resilient for very large playlists — the modal list is virtualized (~24 rows in the DOM regardless of size), thumbnails lazy-loaded, selection kept across scrolling.
- Deleted/private videos are unchecked by default in the playlist selection.
- Playlist modal styling aligned with the main view; consistent input heights, accent color on focused inputs, text color fixes.
- Library paging is smoother: pages inserted in one batch, next page prefetched 240 px early, no scroll jump when searching or returning to the top.
- Checkbox outline fixed.

### Fixed

- Only the last page remained visible after scrolling to the top of the library.
- Small horizontal shift when the search box was active.
- Playback errors when previewing songs in the playlist download modal.
- Playlist download failed on initial load of large playlists.

## [v0.2.0] - 2026-10-19

### Added

- Library: infinite scroll (50 items per page), "scroll to top" button, loading spinner, silent refresh when returning to the top.
- Delete action for library files (replaces the save button), including the sidecar `.png` cover.
- Cover art rendered next to each file name and inside the audio cutter modal.
- Custom app icon (Windows `.ico`, macOS `.icns`, favicon).
- MIT license.

## [v0.1.0] - 2026-10-18

### Added

- Download best-quality audio from YouTube and any yt-dlp-supported site: Opus (default, lossless remux), best (raw WebM), m4a, mp3.
- Playlist downloads with per-video selection.
- Optional cover art saved as a separate `.png`.
- Audio cutter: waveform preview, moving trim cursor, seek, fullscreen modal, timeline, lossless `-c copy` cut that keeps the original file.
- Custom `download_dir` via `config.json`.
- Release pipeline: PyInstaller onedir builds for win64 / macos-arm64 / linux64 with `ffmpeg`, `ffprobe` and `qjs` bundled, frozen-exe smoke test, GitHub Release assets.

[Unreleased]: https://github.com/rybandrei2014/yt-mp3/compare/v0.3.1...HEAD
[v0.3.1]: https://github.com/rybandrei2014/yt-mp3/compare/v0.3.0...v0.3.1
[v0.3.0]: https://github.com/rybandrei2014/yt-mp3/compare/v0.2.0...v0.3.0
[v0.2.0]: https://github.com/rybandrei2014/yt-mp3/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/rybandrei2014/yt-mp3/releases/tag/v0.1.0
