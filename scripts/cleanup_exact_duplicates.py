"""Move exact duplicate copies to a recoverable folder, keeping one copy per hash."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def cleanup(source: Path, database: Path, apply: bool) -> None:
    source = source.resolve(strict=True)
    with sqlite3.connect(database) as connection:
        scan_id = connection.execute("SELECT MAX(scan_id) FROM scan_runs WHERE completed_at IS NOT NULL").fetchone()[0]
        rows = connection.execute(
            "SELECT sha256, path, relative_path, size_bytes FROM media_files WHERE scan_id = ? AND sha256 IS NOT NULL ORDER BY sha256, relative_path",
            [scan_id],
        ).fetchall()
    groups: dict[str, list[tuple[str, str, int]]] = {}
    for digest, path, relative_path, size_bytes in rows:
        groups.setdefault(digest, []).append((path, relative_path, size_bytes))
    extras = [copy for copies in groups.values() for copy in copies[1:]]
    run_root = source.parent / f"{source.name}-photo-curator-recovery" / f"exact-scan-{scan_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if not apply:
        print(f"Dry run: {len(extras):,} exact duplicate copies would move to {run_root}")
        return
    moves: list[dict[str, str]] = []
    def write_log() -> None:
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "undo_log.json").write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "moves": moves}, indent=2) + "\n", encoding="utf-8")
    for number, (path, relative_path, size_bytes) in enumerate(extras, start=1):
        origin = Path(path)
        if not origin.is_file() or origin.stat().st_size != size_bytes:
            raise ValueError(f"File changed since hash scan: {origin}")
        target = run_root / relative_path
        if target.exists():
            raise FileExistsError(f"Recovery target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(origin), str(target))
        moves.append({"source": str(origin), "recovery_path": str(target)})
        write_log()
        if number % 250 == 0 or number == len(extras): print(f"Moved {number:,} / {len(extras):,} duplicate copies")
    print(f"Moved {len(moves):,} exact duplicate copies to {run_root}")


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=project / "data" / "photo_catalogue.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(); cleanup(args.source, args.database, args.apply)


if __name__ == "__main__": main()
