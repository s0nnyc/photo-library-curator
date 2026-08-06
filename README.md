# Photo Library Curator

A local-first tool for safely auditing, deduplicating, and organising a personal photo and video library. Your media stays on your computer: this project has no cloud account, upload, or telemetry.

It is designed for the very common problem of a large, unsorted collection copied from phones, cameras, and old drives.

## What it does

- inventories image, video, sidecar, and other files into a local SQLite catalogue
- finds byte-for-byte duplicates with SHA-256 hashes
- uses an optional local GPU model to suggest high-confidence visual duplicates
- reads capture dates and GPS coordinates, then assigns offline city labels
- organises files into a clear date and location layout
- moves unwanted files only to a recoverable sibling folder, with an undo log

## Safety model

Analysis commands are read-only. Commands that move files require `--apply`.
Nothing is permanently deleted by this project: duplicate copies are moved to a recovery folder beside the source library, and every move is recorded in `undo_log.json`.

Back up your library and let any copy operation finish before scanning it. Start on a small test folder if you are unsure.

## Requirements

- [uv](https://docs.astral.sh/uv/) for reproducible Python environments
- Python 3.11 or newer (installed automatically by `uv` when needed)
- `ffprobe` from FFmpeg for video metadata (optional, but recommended)
- NVIDIA GPU only for the optional visual-similarity stage; CPU works for the rest

On Ubuntu/Debian, install video support with:

```bash
sudo apt install ffmpeg
```

## Quick start: audit and exact duplicates

Clone the repository, enter it, and create the standard environment:

```bash
uv sync --extra dev
```

Scan a library. Quoting the path makes spaces safe. `--hash` reads each file's contents so the scan can identify exact duplicates; it does not change any files.

```bash
uv run python scripts/scan_media.py --source "/path/to/photo-library" --hash
```

Preview the recoverable duplicate move first:

```bash
uv run python scripts/cleanup_exact_duplicates.py --source "/path/to/photo-library"
```

When the preview looks right, perform it:

```bash
uv run python scripts/cleanup_exact_duplicates.py --source "/path/to/photo-library" --apply
```

For example, a source at `/mnt/data/photos` creates recovery folders such as `/mnt/data/photos-photo-curator-recovery/exact-scan-…/`. Do not run the cleanup command twice against the same scan; scan again after the first move.

## Quick start: organise by date and location

The organiser uses only embedded GPS coordinates and the offline GeoNames `cities500` data. Files without a reliable date or location are still placed in predictable folders.

```bash
uv run python scripts/download_geodata.py
uv run python scripts/scan_media.py --source "/path/to/photo-library"
uv run python scripts/enrich_locations.py
uv run python scripts/sort_library.py --source "/path/to/photo-library"
```

The final command is a dry run. Add `--apply` only after checking it:

```bash
uv run python scripts/sort_library.py --source "/path/to/photo-library" --apply
```

The resulting layout is:

```text
photo-library/
├── Photos/2024/Bratislava (SK)/
├── Videos/2024/No location/
├── Screenshots/
├── Edits and sidecars/
└── Other files/
```

Each organisation run writes an undo log to a sibling `*-photo-curator-undo/` directory.

## Optional: local visual-duplicate review

This stage runs an OpenCLIP vision model locally. It prepares a local browser page where you can accept the recommended keeper, choose a different keeper, keep all, or remove a whole group to recovery.

```bash
uv sync --extra vision
uv run python scripts/curate.py --source "/path/to/photo-library"
```

Open the local address printed in the terminal. Reopen an existing review without rescanning:

```bash
uv run python scripts/curate.py --serve
```

## Useful commands

Run the automated checks:

```bash
uv run pytest
```

Browse the latest local catalogue without moving anything:

```bash
uv run python scripts/catalog_server.py
```

Open `http://127.0.0.1:8767` in your browser.

## How it works

```text
media folder
    │
    ├─ scan metadata / optional SHA-256 hashes ──> local SQLite catalogue
    │                                                │
    ├─ offline GPS city lookup ──────────────────────┤
    ├─ optional local GPU embeddings ────────────────┤
    │                                                │
    └─ review or apply ──> organised folders / recovery folder + undo log
```

Project-generated catalogues, reports, thumbnails, model caches, and local environments are excluded from Git. Never commit a personal library, report thumbnails, SQLite catalogue, or recovery folder.

## Development

See [the contribution guide](CONTRIBUTING.md) and [the safety policy](docs/safety-policy.md). The project is released under the [MIT License](LICENSE).

GeoNames data is downloaded separately and remains subject to its [CC BY 4.0 attribution](https://download.geonames.org/export/dump/readme.txt).
