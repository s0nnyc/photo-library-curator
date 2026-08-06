"""Create a review report for visually similar, non-identical image pairs."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import numpy as np


def latest_scan_id(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT scan_id FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("No completed scan is available.")
    return row[0]


def select_model(connection: sqlite3.Connection, scan_id: int) -> str:
    row = connection.execute(
        "SELECT model_name FROM image_embeddings WHERE scan_id = ? ORDER BY computed_at DESC LIMIT 1",
        [scan_id],
    ).fetchone()
    if not row:
        raise ValueError("No image embeddings are available. Run embed_images.py first.")
    return row[0]


def create_report(database: Path, report: Path, threshold: float) -> None:
    with sqlite3.connect(database) as connection:
        scan_id = latest_scan_id(connection)
        model_name = select_model(connection, scan_id)
        rows = connection.execute("""
            SELECT media.relative_path, media.sha256, embedding.dimensions, embedding.embedding
            FROM image_embeddings AS embedding
            JOIN media_files AS media
              ON media.scan_id = embedding.scan_id AND media.path = embedding.path
            WHERE embedding.scan_id = ? AND embedding.model_name = ?
            ORDER BY media.relative_path
        """, [scan_id, model_name]).fetchall()
        failures = connection.execute("""
            SELECT media.relative_path, failure.error_message
            FROM image_embedding_failures AS failure
            JOIN media_files AS media
              ON media.scan_id = failure.scan_id AND media.path = failure.path
            WHERE failure.scan_id = ? AND failure.model_name = ?
            ORDER BY media.relative_path
        """, [scan_id, model_name]).fetchall()

    paths = [row[0] for row in rows]
    hashes = [row[1] for row in rows]
    vectors = np.vstack([np.frombuffer(row[3], dtype=np.float32, count=row[2]) for row in rows])
    similarity = vectors @ vectors.T
    upper_triangle = np.triu(np.ones(similarity.shape, dtype=bool), 1)
    indexes_a, indexes_b = np.where((similarity >= threshold) & upper_triangle)
    candidates = sorted(
        (
            (float(similarity[index_a, index_b]), paths[index_a], paths[index_b])
            for index_a, index_b in zip(indexes_a, indexes_b, strict=True)
            if hashes[index_a] != hashes[index_b]
        ),
        reverse=True,
    )

    lines = [
        "# Visual similarity review candidates",
        "",
        f"Scan ID: `{scan_id}`  ",
        f"Model: `{model_name}`  ",
        f"Similarity threshold: `{threshold:.3f}`",
        "",
        "These are visually similar but non-identical files. They are review candidates only; no source file has been changed.",
        "",
        f"Candidates: **{len(candidates):,}**",
        "",
        "| Similarity | First file | Second file |",
        "| ---: | --- | --- |",
        *[
            f"| {score:.4f} | `{first_path.replace('|', '\\|')}` | `{second_path.replace('|', '\\|')}` |"
            for score, first_path, second_path in candidates
        ],
        "",
        "## Files unreadable by the pixel decoder",
        "",
        f"Files: **{len(failures):,}**",
        "",
        "| File | Error |",
        "| --- | --- |",
        *[
            f"| `{path.replace('|', '\\|')}` | {error.replace('|', '\\|')} |"
            for path, error in failures
        ],
    ]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(candidates):,} candidate pairs to {report}")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--report", type=Path, default=project_root / "reports" / "visual_similarity.md")
    parser.add_argument("--threshold", type=float, default=0.995, help="Cosine similarity threshold from 0 to 1")
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be greater than 0 and at most 1")
    create_report(args.database, args.report, args.threshold)


if __name__ == "__main__":
    main()

