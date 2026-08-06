"""Automatically reorganize a scanned library by media type, date, and offline location."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def safe_name(value: str) -> str:
    return "".join("_" if character in '/\\\0' else character for character in value).strip() or "Unknown"


def unique_destination(directory: Path, filename: str, occupied: set[Path], current: Path) -> Path:
    candidate = directory / filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    number = 2
    while candidate in occupied or (candidate.exists() and candidate != current):
        candidate = directory / f"{stem}__{number}{suffix}"
        number += 1
    occupied.add(candidate)
    return candidate


def destination(root: Path, row: sqlite3.Row, city: str | None, country: str | None, occupied: set[Path]) -> Path:
    kind = row["media_kind"]
    current = Path(row["path"])
    if row["size_bytes"] == 0:
        return unique_destination(root / "Other files" / "Empty files", current.name, occupied, current)
    if kind == "sidecar":
        return unique_destination(root / "Edits and sidecars", current.name, occupied, current)
    if row["filename_group"] == "screenshot":
        return unique_destination(root / "Screenshots", current.name, occupied, current)
    if kind not in {"image", "video"}:
        return unique_destination(root / "Other files", current.name, occupied, current)
    category = "Photos" if kind == "image" else "Videos"
    year = row["captured_at"][:4] if row["captured_at"] else "Unknown date"
    place = safe_name(f"{city} ({country})") if city else "No location"
    return unique_destination(root / category / year / place, current.name, occupied, current)


def sort(source: Path, database: Path, apply: bool) -> tuple[int, Path]:
    source = source.resolve(strict=True)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        scan_id = connection.execute("SELECT MAX(scan_id) FROM scan_runs WHERE completed_at IS NOT NULL").fetchone()[0]
        location_count = connection.execute("SELECT COUNT(*) FROM media_locations WHERE scan_id = ?", [scan_id]).fetchone()[0]
        if not location_count:
            raise ValueError("The latest scan has no GPS enrichment. Run scripts/enrich_locations.py before sorting.")
        rows = connection.execute(
            """
            SELECT media.path, media.relative_path, media.media_kind, media.filename_group, media.size_bytes, media.captured_at,
                   location.nearest_city, location.country_code
            FROM media_files AS media LEFT JOIN media_locations AS location
              ON location.scan_id = media.scan_id AND location.path = media.path
            WHERE media.scan_id = ? ORDER BY media.relative_path
            """,
            [scan_id],
        ).fetchall()
    occupied: set[Path] = set()
    moves: list[tuple[Path, Path]] = []
    for row in rows:
        origin = Path(row["path"])
        target = destination(source, row, row["nearest_city"], row["country_code"], occupied)
        if origin.resolve() != target.resolve():
            moves.append((origin, target))

    run_id = datetime.now(timezone.utc).strftime("sort-%Y%m%dT%H%M%SZ")
    undo_root = source.parent / f"{source.name}-photo-curator-undo" / run_id
    if not apply:
        print(f"Dry run: {len(moves):,} files would be reorganized directly inside {source}")
        print(f"Undo log would be written to {undo_root / 'undo_log.json'}")
        return len(moves), undo_root

    completed: list[dict[str, str]] = []
    def write_log() -> None:
        undo_root.mkdir(parents=True, exist_ok=True)
        (undo_root / "undo_log.json").write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "moves": completed}, indent=2) + "\n", encoding="utf-8")
    for number, (origin, target) in enumerate(moves, start=1):
        if not origin.is_file():
            raise FileNotFoundError(f"Source changed or is missing: {origin}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(origin), str(target))
        completed.append({"source": str(origin), "destination": str(target)})
        write_log()
        if number % 250 == 0 or number == len(moves):
            print(f"Moved {number:,} / {len(moves):,} files")
    print(f"Reorganized {len(completed):,} files. Undo log: {undo_root / 'undo_log.json'}")
    return len(completed), undo_root


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=project / "data" / "photo_catalogue.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sort(args.source, args.database, args.apply)


if __name__ == "__main__":
    main()
