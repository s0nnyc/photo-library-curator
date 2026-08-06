"""Build an automatic, high-confidence visual cleanup recommendation."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from build_cleanup_plan import keeper_rank, visual_groups
from build_visual_review import candidates, group_id

DEFAULT_THRESHOLD = 0.998


def exact_duplicate_groups(connection: sqlite3.Connection, scan_id: int, visual_paths: set[str]) -> list[dict[str, object]]:
    """Return exact-copy groups that are not already covered by a visual group."""
    rows = connection.execute(
        """
        SELECT path, relative_path, size_bytes, sha256
        FROM media_files
        WHERE scan_id = ? AND media_kind IN ('image', 'video') AND sha256 IS NOT NULL
        ORDER BY relative_path
        """,
        [scan_id],
    ).fetchall()
    by_hash: dict[str, list[dict[str, object]]] = defaultdict(list)
    for path, relative_path, size_bytes, sha256 in rows:
        by_hash[sha256].append({"path": path, "relative_path": relative_path, "size_bytes": size_bytes, "sha256": sha256})

    groups: list[dict[str, object]] = []
    for number, copies in enumerate(by_hash.values(), start=1):
        if len(copies) < 2 or any(str(copy["path"]) in visual_paths for copy in copies):
            continue
        keeper = min(copies, key=lambda copy: str(copy["relative_path"]))
        groups.append({
            "id": f"exact-{number:03d}", "kind": "exact duplicate", "members": copies,
            "recommended_keeper": keeper["path"],
        })
    return groups


def actions_for_groups(groups: list[dict[str, object]], choices: dict[str, str] | None = None) -> list[dict[str, object]]:
    """Turn approved keeper choices into validated move candidates."""
    actions: list[dict[str, object]] = []
    choices = choices or {}
    for group in groups:
        selected = choices.get(str(group["id"]), str(group["recommended_keeper"]))
        if selected == "keep_all":
            continue
        members = group["members"]
        valid_paths = {str(member["path"]) for member in members}
        if selected != "delete_all" and selected not in valid_paths:
            raise ValueError(f"Invalid keeper selection for {group['id']}")
        for member in members:
            if selected != "delete_all" and str(member["path"]) == selected:
                continue
            actions.append({
                "group_id": group["id"], "source_path": member["path"], "relative_path": member["relative_path"],
                "expected_size_bytes": member["size_bytes"], "expected_sha256": member.get("sha256"),
                "reason": group["kind"],
                "keeper": "none — remove entire group" if selected == "delete_all" else next(item["relative_path"] for item in members if item["path"] == selected),
            })
    return actions


def build_recommendation(database: Path, manifest: Path, report: Path, threshold: float = DEFAULT_THRESHOLD) -> dict[str, object]:
    """Create a manifest only; this function never moves or deletes source files."""
    scan_id, model_name, items, pairs = candidates(database, threshold)
    groups = visual_groups(items, pairs)
    visual_paths = {str(items[index]["path"]) for group in groups for index in group}
    plan_groups: list[dict[str, object]] = []
    for group in groups:
        members = [items[index] for index in group]
        keeper = min(members, key=keeper_rank)
        plan_groups.append({
            "id": f"visual-{group_id(items, group)}", "kind": "high-confidence visual match",
            "members": [
                {key: item[key] for key in ("path", "relative_path", "size_bytes", "sha256", "width", "height")}
                for item in members
            ],
            "recommended_keeper": keeper["path"],
        })

    with sqlite3.connect(database) as connection:
        source_root = connection.execute("SELECT source_root FROM scan_runs WHERE scan_id = ?", [scan_id]).fetchone()[0]
        plan_groups.extend(exact_duplicate_groups(connection, scan_id, visual_paths))

    actions = actions_for_groups(plan_groups)
    total_bytes = sum(int(action["expected_size_bytes"]) for action in actions)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plan_id = f"scan-{scan_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result = {
        "version": 1,
        "plan_id": plan_id,
        "created_at": created_at,
        "scan_id": scan_id,
        "source_root": source_root,
        "policy": "high-confidence visual matches plus exact duplicates",
        "visual_similarity_threshold": threshold,
        "model": model_name,
        "groups": plan_groups,
        "actions": actions,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Cleanup recommendation",
        "",
        "> **Nothing has been changed.** This is the automatic recommendation from local AI analysis.",
        "",
        f"- **{len(actions)} files** can be moved to a recoverable bin",
        f"- **{total_bytes / 1024**2:.1f} MiB** potential recovery",
        f"- **{len(groups)} high-confidence visual groups** at similarity `{threshold:.3f}`",
        f"- Policy: {result['policy']}",
        "",
        "To execute this exact recommendation, use the single explicit apply command. It validates every file before moving it and creates an undo log.",
    ]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Recommendation: {len(actions)} files, {total_bytes / 1024**2:.1f} MiB")
    return result
