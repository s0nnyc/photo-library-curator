"""Create a read-only cleanup plan from confirmed visual review decisions."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from build_visual_review import DisjointSet, candidates, group_id
from review_server import decisions_for_scan


def visual_groups(items: list[dict[str, object]], pairs: list[tuple[int, int, float]]) -> list[list[int]]:
    groups = DisjointSet(len(items))
    for first, second, _ in pairs:
        groups.union(first, second)
    members: dict[int, list[int]] = defaultdict(list)
    for first, second, _ in pairs:
        members[groups.find(first)].extend((first, second))
    return sorted(
        (sorted(set(component)) for component in members.values()),
        key=lambda component: (-len(component), str(items[component[0]]["relative_path"])),
    )


def keeper_rank(item: dict[str, object]) -> tuple[int, int, str]:
    """Prefer detail first, then data volume; path makes ties deterministic."""
    pixels = int(item["width"] or 0) * int(item["height"] or 0)
    return (-pixels, -int(item["size_bytes"]), str(item["relative_path"]))


def describe(item: dict[str, object]) -> str:
    width, height = item["width"], item["height"]
    dimensions = f"{width}×{height}" if width and height else "dimensions unavailable"
    size_mib = int(item["size_bytes"]) / 1024**2
    return f"`{item['relative_path']}` — {dimensions}, {size_mib:.1f} MiB"


def build_plan(database: Path, report: Path, threshold: float) -> None:
    scan_id, model_name, items, pairs = candidates(database, threshold)
    groups = visual_groups(items, pairs)
    decisions = decisions_for_scan(database, scan_id)

    keep_one: list[tuple[str, dict[str, object], list[dict[str, object]], str]] = []
    remove_all: list[tuple[str, list[dict[str, object]], str]] = []
    for component in groups:
        identifier = group_id(items, component)
        decision = decisions.get(identifier)
        if not decision:
            continue
        members = [items[index] for index in component]
        if decision["decision"] == "delete_all_but_one":
            keeper = min(members, key=keeper_rank)
            removals = [item for item in members if item is not keeper]
            keep_one.append((identifier, keeper, removals, decision["note"]))
        elif decision["decision"] == "delete_group":
            remove_all.append((identifier, members, decision["note"]))

    proposed_removals = [item for _, _, removals, _ in keep_one for item in removals]
    proposed_removals.extend(item for _, members, _ in remove_all for item in members)
    total_bytes = sum(int(item["size_bytes"]) for item in proposed_removals)
    lines = [
        "# Proposed photo cleanup plan",
        "",
        "> **Plan only — no source file has been changed.** This report does not execute, move, rename, or delete anything.",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`  ",
        f"Scan: `{scan_id}` · Model: `{model_name}` · Similarity threshold: `{threshold:.3f}`  ",
        f"Saved choices: `{len(decisions)} / {len(groups)}` visual groups",
        "",
        "## Summary",
        "",
        f"- Keep-one groups: **{len(keep_one)}**",
        f"- Remove-all groups: **{len(remove_all)}**",
        f"- Recommended keepers: **{len(keep_one)}**",
        f"- Proposed removals: **{len(proposed_removals)} files**, **{total_bytes / 1024**2:.1f} MiB**",
        "",
        "A keeper recommendation uses the largest pixel dimensions, then the largest file size. It is a default only; a later approval screen can allow an override.",
        "",
        "## Keep one, propose removal of the rest",
        "",
    ]
    for identifier, keeper, removals, note in keep_one:
        lines.extend([
            f"### Group `{identifier}`",
            "",
            f"**Recommended keeper:** {describe(keeper)}",
            f"**Proposed removals:** {len(removals)}",
            *([f"**Your note:** {note}", ""] if note else []),
            "",
            *[f"- {describe(item)}" for item in removals],
            "",
        ])
    if not keep_one:
        lines.extend(["No groups were marked **Keep one**.", ""])

    lines.extend(["## Remove all", ""])
    for identifier, members, note in remove_all:
        lines.extend([
            f"### Group `{identifier}`",
            "",
            f"**Proposed removals:** {len(members)}",
            *([f"**Your note:** {note}", ""] if note else []),
            "",
            *[f"- {describe(item)}" for item in members],
            "",
        ])
    if not remove_all:
        lines.extend(["No groups were marked **Remove all**.", ""])

    lines.extend([
        "## Before any execution",
        "",
        "1. Review this exact list and any keeper recommendation.",
        "2. Generate an explicit, reversible execution plan.",
        "3. Approve that plan separately before any source file is moved to a recoverable holding area.",
    ])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote plan with {len(proposed_removals)} proposed removals to {report}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--report", type=Path, default=project_root / "reports" / "cleanup_plan.md")
    parser.add_argument("--threshold", type=float, default=0.995)
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be greater than 0 and at most 1")
    build_plan(args.database, args.report, args.threshold)


if __name__ == "__main__":
    main()
