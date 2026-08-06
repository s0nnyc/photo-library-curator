"""Move a generated cleanup recommendation into a recoverable bin."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from scan_media import sha256


def apply_actions(manifest: dict[str, object], actions: list[dict[str, object]], recovery_root: Path, execute: bool) -> Path:
    """Apply a supplied subset of a manifest's actions, always recoverably."""
    source_root = Path(manifest["source_root"]).resolve(strict=True)
    run_root = recovery_root / str(manifest["plan_id"])
    if not execute:
        print(f"Dry run: {len(actions)} files would move to {run_root}")
        print("Re-run with --apply to perform the recoverable move.")
        return run_root

    prepared: list[tuple[Path, Path, dict[str, object]]] = []
    for action in actions:
        source = Path(action["source_path"]).resolve(strict=True)
        try:
            relative = source.relative_to(source_root)
        except ValueError as error:
            raise ValueError(f"Refusing a path outside the source library: {source}") from error
        if source.stat().st_size != action["expected_size_bytes"]:
            raise ValueError(f"File changed since analysis: {source}")
        expected_hash = action.get("expected_sha256")
        if expected_hash and sha256(source) != expected_hash:
            raise ValueError(f"File content changed since analysis: {source}")
        destination = run_root / relative
        if destination.exists():
            raise FileExistsError(f"Recovery destination already exists: {destination}")
        prepared.append((source, destination, action))

    moved: list[dict[str, str]] = []

    def write_log() -> None:
        log = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "moves": moved}
        (run_root / "undo_log.json").write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")

    try:
        for source, destination, _ in prepared:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved.append({"source": str(source), "recovery_path": str(destination)})
            # A log after every move makes an interrupted run recoverable.
            write_log()
    except BaseException:
        if moved:
            write_log()
        raise
    print(f"Moved {len(moved)} files to {run_root}")
    print(f"Undo log: {run_root / 'undo_log.json'}")
    return run_root


def apply(manifest_path: Path, recovery_root: Path, execute: bool) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return apply_actions(manifest, manifest["actions"], recovery_root, execute)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=project_root / "data" / "ai_cleanup_manifest.json")
    parser.add_argument("--recovery-root", type=Path)
    parser.add_argument("--apply", action="store_true", help="Perform the recoverable move; otherwise only show a dry run.")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source_root = Path(manifest["source_root"]).resolve(strict=True)
    recovery_root = args.recovery_root or source_root.parent / f"{source_root.name}-photo-curator-recovery"
    apply(args.manifest, recovery_root, args.apply)


if __name__ == "__main__":
    main()
