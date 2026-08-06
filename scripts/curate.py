"""Run the simple Photo Library Curator workflow."""

from __future__ import annotations

import argparse
from pathlib import Path

from build_ai_cleanup import DEFAULT_THRESHOLD, build_recommendation
from build_cleanup_page import build_page
from cleanup_server import serve
from embed_images import embed
from scan_media import scan


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Photo library to analyze (read-only during analysis)")
    parser.add_argument("--serve", action="store_true", help="Open the current recommendation without rescanning")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--manifest", type=Path, default=project_root / "data" / "ai_cleanup_manifest.json")
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be greater than 0 and at most 1")
    page = project_root / "reports" / "cleanup_approval"
    if args.source:
        scan(args.source, args.database, project_root / "reports" / "inventory.md", hash_contents=True)
        embed(args.database, args.batch_size, limit=None)
        build_recommendation(args.database, args.manifest, project_root / "reports" / "cleanup_recommendation.md", args.threshold)
    elif not args.serve:
        parser.error("--source is required when analyzing a library. Use --serve to reopen an existing recommendation.")
    build_page(args.manifest, page)
    serve(args.manifest, page)


if __name__ == "__main__":
    main()
