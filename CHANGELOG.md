# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Release notes for GitHub Releases are taken from the matching `## [vX.Y.Z]` section below (see `.github/workflows/release.yml`).

## [Unreleased]

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
