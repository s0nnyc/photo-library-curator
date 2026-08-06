"""Create local GPU image embeddings for a completed photo-library scan."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import open_clip
import torch
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

MODEL_NAME = "ViT-B-32"
PRETRAINED_TAG = "laion2b_s34b_b79k"

EMBEDDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS image_embeddings (
    scan_id INTEGER NOT NULL REFERENCES scan_runs(scan_id),
    path TEXT NOT NULL,
    model_name TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (scan_id, path, model_name)
);
CREATE INDEX IF NOT EXISTS idx_image_embeddings_scan_model
    ON image_embeddings(scan_id, model_name);

CREATE TABLE IF NOT EXISTS image_embedding_failures (
    scan_id INTEGER NOT NULL REFERENCES scan_runs(scan_id),
    path TEXT NOT NULL,
    model_name TEXT NOT NULL,
    error_message TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (scan_id, path, model_name)
);
"""


def latest_scan_id(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT scan_id FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("No completed photo scan is available. Run scan_media.py first.")
    return row[0]


def pending_image_paths(connection: sqlite3.Connection, scan_id: int, model_id: str, limit: int | None) -> list[str]:
    query = """
        SELECT media.path
        FROM media_files AS media
        LEFT JOIN image_embeddings AS embedding
          ON embedding.scan_id = media.scan_id
         AND embedding.path = media.path
         AND embedding.model_name = ?
        LEFT JOIN image_embedding_failures AS failure
          ON failure.scan_id = media.scan_id
         AND failure.path = media.path
         AND failure.model_name = ?
        WHERE media.scan_id = ?
          AND media.media_kind = 'image'
          AND embedding.path IS NULL
          AND failure.path IS NULL
        ORDER BY media.relative_path
    """
    if limit:
        query += " LIMIT ?"
        return [row[0] for row in connection.execute(query, [model_id, model_id, scan_id, limit])]
    return [row[0] for row in connection.execute(query, [model_id, model_id, scan_id])]


def load_image(path: str, preprocess: object) -> torch.Tensor:
    with Image.open(path) as image:
        return preprocess(image.convert("RGB"))


def save_batch(
    connection: sqlite3.Connection,
    scan_id: int,
    model_id: str,
    paths: list[str],
    tensors: list[torch.Tensor],
    model: torch.nn.Module,
    device: torch.device,
) -> int:
    if not paths:
        return 0
    image_batch = torch.stack(tensors).to(device)
    with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.float16):
        vectors = model.encode_image(image_batch)
        vectors = vectors / vectors.norm(dim=-1, keepdim=True)
    vectors = vectors.float().cpu().numpy()
    created_at = datetime.now(timezone.utc).isoformat()
    connection.executemany(
        "INSERT INTO image_embeddings VALUES (?, ?, ?, ?, ?, ?)",
        [
            (scan_id, path, model_id, vector.shape[0], vector.astype(np.float32).tobytes(), created_at)
            for path, vector in zip(paths, vectors, strict=True)
        ],
    )
    connection.commit()
    return len(paths)


def record_failure(connection: sqlite3.Connection, scan_id: int, path: str, model_id: str, error: Exception) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO image_embedding_failures VALUES (?, ?, ?, ?, ?)",
        (scan_id, path, model_id, f"{type(error).__name__}: {error}", datetime.now(timezone.utc).isoformat()),
    )
    connection.commit()


def embed(database: Path, batch_size: int, limit: int | None) -> None:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available. The local GPU runtime was not detected.")

    project_root = Path(__file__).resolve().parents[1]
    os.environ.setdefault("HF_HOME", str(project_root / ".cache" / "huggingface"))
    model_id = f"{MODEL_NAME}:{PRETRAINED_TAG}"
    device = torch.device("cuda")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(EMBEDDING_SCHEMA)
        scan_id = latest_scan_id(connection)
        paths = pending_image_paths(connection, scan_id, model_id, limit)
        if not paths:
            print("No unembedded images found for the latest scan.")
            return

        print(f"Loading {MODEL_NAME} on {torch.cuda.get_device_name(0)}.", flush=True)
        print(f"Embedding {len(paths):,} images from scan {scan_id}; source images remain read-only.", flush=True)
        model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=PRETRAINED_TAG)
        model = model.to(device).eval()

        completed = 0
        batch_paths: list[str] = []
        batch_tensors: list[torch.Tensor] = []
        skipped = 0
        for path in paths:
            try:
                image_tensor = load_image(path, preprocess)
            except (OSError, UnidentifiedImageError, ValueError) as error:
                print(f"Skipped unreadable image: {path} ({type(error).__name__}: {error})")
                record_failure(connection, scan_id, path, model_id, error)
                skipped += 1
                continue
            batch_paths.append(path)
            batch_tensors.append(image_tensor)

            if len(batch_paths) == batch_size:
                completed += save_batch(connection, scan_id, model_id, batch_paths, batch_tensors, model, device)
                print(f"Embedded {completed:,} / {len(paths):,} images", flush=True)
                batch_paths, batch_tensors = [], []

        completed += save_batch(connection, scan_id, model_id, batch_paths, batch_tensors, model, device)
        print(f"Embedded {completed:,} images; skipped {skipped:,} unreadable images.")


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project_root / "data" / "photo_catalogue.db")
    parser.add_argument("--batch-size", type=int, default=64, help="Images per GPU inference batch")
    parser.add_argument("--limit", type=int, help="Embed only this many images, for a small test run")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    embed(args.database, args.batch_size, args.limit)


if __name__ == "__main__":
    main()
