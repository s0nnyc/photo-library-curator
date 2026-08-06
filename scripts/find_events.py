"""Create read-only candidate photo events by clustering capture timestamps."""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


def local_capture_time(value: str) -> datetime:
    """Use camera-local wall-clock time; EXIF usually has no trustworthy timezone."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def create_report(database: Path, report: Path, gap_hours: float, minimum_items: int) -> None:
    with sqlite3.connect(database) as connection:
        scan = connection.execute(
            "SELECT scan_id FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY scan_id DESC LIMIT 1"
        ).fetchone()
        if not scan:
            raise ValueError("No completed scan is available.")
        scan_id = scan[0]
        rows = connection.execute("""
            SELECT relative_path, media_kind, captured_at
            FROM media_files
            WHERE scan_id = ? AND media_kind IN ('image', 'video') AND captured_at IS NOT NULL
            ORDER BY captured_at
        """, [scan_id]).fetchall()

    clusters: list[list[tuple[str, str, datetime]]] = []
    current: list[tuple[str, str, datetime]] = []
    for relative_path, kind, captured_at in rows:
        item = (relative_path, kind, local_capture_time(captured_at))
        if current and (item[2] - current[-1][2]).total_seconds() > gap_hours * 3600:
            clusters.append(current)
            current = []
        current.append(item)
    if current:
        clusters.append(current)
    events = [cluster for cluster in clusters if len(cluster) >= minimum_items]

    lines = [
        "# Capture-time event candidates",
        "",
        f"Scan ID: `{scan_id}`  ",
        f"New event after: `{gap_hours:g}` hours without a capture  ",
        f"Minimum event size: `{minimum_items}` items",
        "",
        "Capture times without timezone data are treated as camera-local times. These are review candidates only; no source files have been changed.",
        "",
        f"Candidate events: **{len(events):,}**",
        "",
        "| Start | End | Duration | Images | Videos | Dominant folder |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for event in events:
        start, end = event[0][2], event[-1][2]
        duration = end - start
        images = sum(kind == "image" for _, kind, _ in event)
        videos = sum(kind == "video" for _, kind, _ in event)
        folders = Counter(path.split("/", 1)[0] for path, _, _ in event)
        folder = folders.most_common(1)[0][0]
        lines.append(
            f"| {start:%Y-%m-%d %H:%M} | {end:%Y-%m-%d %H:%M} | {str(duration)} | {images:,} | {videos:,} | `{folder}` |"
        )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(events):,} event candidates to {report}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--report", type=Path, default=project_root / "reports" / "event_candidates.md")
    parser.add_argument("--gap-hours", type=float, default=4)
    parser.add_argument("--minimum-items", type=int, default=3)
    args = parser.parse_args()
    if args.gap_hours <= 0 or args.minimum_items < 1:
        parser.error("--gap-hours and --minimum-items must be positive")
    create_report(args.database, args.report, args.gap_hours, args.minimum_items)


if __name__ == "__main__":
    main()

