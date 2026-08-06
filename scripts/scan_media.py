"""Create a read-only SQLite catalogue and audit report for a media library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id INTEGER PRIMARY KEY,
    source_root TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    hash_contents INTEGER NOT NULL CHECK (hash_contents IN (0, 1)),
    file_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media_files (
    scan_id INTEGER NOT NULL REFERENCES scan_runs(scan_id),
    path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    extension TEXT NOT NULL,
    media_kind TEXT NOT NULL CHECK (media_kind IN ('image', 'video', 'sidecar', 'other')),
    filename_group TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_at TEXT NOT NULL,
    captured_at TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    camera_make TEXT,
    camera_model TEXT,
    sha256 TEXT,
    metadata_error TEXT,
    PRIMARY KEY (scan_id, path)
);

CREATE INDEX IF NOT EXISTS idx_media_files_scan_kind ON media_files(scan_id, media_kind);
CREATE INDEX IF NOT EXISTS idx_media_files_scan_capture ON media_files(scan_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_media_files_sha256 ON media_files(sha256) WHERE sha256 IS NOT NULL;
"""


def media_kind(extension: str) -> str:
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension == ".aae":
        return "sidecar"
    return "other"


def filename_group(filename: str, kind: str) -> str:
    name = filename.lower()
    if kind == "sidecar":
        return "iphone_edit_sidecar"
    if "screenshot" in name or name.startswith("screen shot"):
        return "screenshot"
    if name.startswith(("signal-", "whatsapp", "telegram")):
        return "messaging_export"
    if name.startswith(("img_", "dsc", "gopr", "pict")):
        return "camera_generated"
    return "other"


def iso_from_exif(value: object) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        return None


def image_metadata(path: Path) -> tuple[str | None, int | None, int | None, str | None, str | None, str | None]:
    """Return capture time, dimensions, camera, and any safe-to-report decoder error."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            captured_at = next(
                (iso_from_exif(exif.get(tag)) for tag in (36867, 36868, 306) if iso_from_exif(exif.get(tag))),
                None,
            )
            return (
                captured_at,
                image.width,
                image.height,
                str(exif.get(271)) if exif.get(271) else None,
                str(exif.get(272)) if exif.get(272) else None,
                None,
            )
    except (OSError, UnidentifiedImageError, ValueError) as error:
        return None, None, None, None, None, f"{type(error).__name__}: {error}"


def video_metadata(path: Path) -> tuple[str | None, int | None, int | None, float | None, str | None]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:format_tags=creation_time:stream=width,height",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
        result = json.loads(completed.stdout)
        stream = next((item for item in result.get("streams", []) if item.get("width")), {})
        duration = result.get("format", {}).get("duration")
        created = result.get("format", {}).get("tags", {}).get("creation_time")
        return created, stream.get("width"), stream.get("height"), float(duration) if duration else None, None
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, ValueError) as error:
        return None, None, None, None, f"{type(error).__name__}: {error}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: Path) -> Iterator[Path]:
    for directory, _, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            yield Path(directory) / filename


def initialise_database(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA)
    return connection


def write_report(connection: sqlite3.Connection, scan_id: int, report_path: Path) -> None:
    rows = connection.execute(
        "SELECT media_kind, COUNT(*), COALESCE(SUM(size_bytes), 0) FROM media_files WHERE scan_id = ? GROUP BY media_kind ORDER BY COUNT(*) DESC",
        [scan_id],
    ).fetchall()
    groups = connection.execute(
        "SELECT filename_group, COUNT(*) FROM media_files WHERE scan_id = ? GROUP BY filename_group ORDER BY COUNT(*) DESC",
        [scan_id],
    ).fetchall()
    dated, total = connection.execute(
        "SELECT COUNT(captured_at), COUNT(*) FROM media_files WHERE scan_id = ? AND media_kind IN ('image', 'video')",
        [scan_id],
    ).fetchone()
    errors = connection.execute(
        "SELECT COUNT(*) FROM media_files WHERE scan_id = ? AND metadata_error IS NOT NULL",
        [scan_id],
    ).fetchone()[0]
    duplicate_groups, extra_duplicate_files, reclaimable_bytes = connection.execute("""
        SELECT
            COUNT(*),
            COALESCE(SUM(copy_count - 1), 0),
            COALESCE(SUM(total_bytes - one_copy_bytes), 0)
        FROM (
            SELECT sha256, COUNT(*) AS copy_count, SUM(size_bytes) AS total_bytes,
                   MIN(size_bytes) AS one_copy_bytes
            FROM media_files
            WHERE scan_id = ? AND sha256 IS NOT NULL
            GROUP BY sha256
            HAVING COUNT(*) > 1
        )
    """, [scan_id]).fetchone()

    def size_text(size: int) -> str:
        return f"{size / 1024**3:.2f} GiB"

    lines = [
        "# Photo library audit",
        "",
        f"Scan ID: `{scan_id}`",
        "",
        "## Files by type",
        "",
        "| Type | Files | Size |",
        "| --- | ---: | ---: |",
        *[f"| {kind} | {count:,} | {size_text(size)} |" for kind, count, size in rows],
        "",
        "## Filename signals",
        "",
        "| Signal | Files |",
        "| --- | ---: |",
        *[f"| {group} | {count:,} |" for group, count in groups],
        "",
        f"Capture date present: **{dated:,} / {total:,}** image and video files.",
        f"Metadata decoder errors: **{errors:,}**.",
    ]
    if duplicate_groups:
        lines.extend([
            f"Exact duplicate content groups: **{duplicate_groups:,}**.",
            f"Extra duplicate files: **{extra_duplicate_files:,}**.",
            f"Potential space reclaim after human review: **{size_text(reclaimable_bytes)}**.",
        ])
    else:
        lines.append("Exact duplicate detection was not run; use `--hash` in a later read-only pass.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scan(source: Path, database: Path, report: Path, hash_contents: bool) -> None:
    source = source.resolve(strict=True)
    started_at = datetime.now(timezone.utc).isoformat()
    files = list(iter_files(source))
    print(f"Found {len(files):,} files. Reading metadata only; source files will not be modified.")

    with initialise_database(database) as connection:
        scan_id = connection.execute(
            "INSERT INTO scan_runs (source_root, started_at, hash_contents) VALUES (?, ?, ?)",
            [str(source), started_at, int(hash_contents)],
        ).lastrowid

        for number, path in enumerate(files, start=1):
            try:
                stat = path.stat()
                extension = path.suffix.lower()
                kind = media_kind(extension)
                captured_at = width = height = duration = camera_make = camera_model = error = None
                if kind == "image":
                    captured_at, width, height, camera_make, camera_model, error = image_metadata(path)
                elif kind == "video":
                    captured_at, width, height, duration, error = video_metadata(path)

                connection.execute(
                    """
                    INSERT INTO media_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        scan_id, str(path), str(path.relative_to(source)), extension, kind,
                        filename_group(path.name, kind), stat.st_size,
                        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        captured_at, width, height, duration, camera_make, camera_model,
                        sha256(path) if hash_contents and kind in {"image", "video"} else None,
                        error,
                    ],
                )
            except OSError as error:
                print(f"Skipped unreadable file: {path} ({error})", file=sys.stderr)
            if number % 250 == 0 or number == len(files):
                connection.commit()
                print(f"Catalogued {number:,} / {len(files):,} files")

        connection.execute(
            "UPDATE scan_runs SET completed_at = ?, file_count = ? WHERE scan_id = ?",
            [datetime.now(timezone.utc).isoformat(), len(files), scan_id],
        )
        write_report(connection, scan_id, report)

    print(f"Catalogue: {database}")
    print(f"Report:    {report}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Photo/video directory to inspect read-only")
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--report", type=Path, default=project_root / "reports" / "inventory.md")
    parser.add_argument("--hash", action="store_true", help="Read media content to calculate exact duplicate hashes")
    args = parser.parse_args()
    scan(args.source, args.database, args.report, args.hash)


if __name__ == "__main__":
    main()
