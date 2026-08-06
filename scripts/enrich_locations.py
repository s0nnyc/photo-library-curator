"""Add offline GPS city labels to the current photo catalogue without changing photos."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_locations (
    scan_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    nearest_city TEXT,
    country_code TEXT,
    distance_km REAL,
    PRIMARY KEY (scan_id, path)
);
CREATE INDEX IF NOT EXISTS idx_media_locations_scan_city ON media_locations(scan_id, nearest_city);
"""


def decimal_degrees(values: object, direction: object) -> float | None:
    if not values or not direction:
        return None
    try:
        degrees, minutes, seconds = (float(value) for value in values)
        value = degrees + minutes / 60 + seconds / 3600
        return -value if str(direction) in {"S", "W"} else value
    except (TypeError, ValueError):
        return None


def gps_coordinates(path: Path) -> tuple[float, float] | None:
    try:
        with Image.open(path) as image:
            gps = image.getexif().get_ifd(34853)
            latitude = decimal_degrees(gps.get(2), gps.get(1))
            longitude = decimal_degrees(gps.get(4), gps.get(3))
            return (latitude, longitude) if latitude is not None and longitude is not None else None
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def video_gps_coordinates(path: Path) -> tuple[float, float] | None:
    command = ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)]
    try:
        tags = json.loads(subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout).get("format", {}).get("tags", {})
        location = tags.get("com.apple.quicktime.location.ISO6709") or tags.get("location")
        match = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", location or "")
        return (float(match.group(1)), float(match.group(2))) if match else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


def load_cities(path: Path) -> tuple[np.ndarray, list[tuple[str, str]]]:
    vectors: list[tuple[float, float, float]] = []
    labels: list[tuple[str, str]] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            fields = line.rstrip("\n").split("\t")
            latitude, longitude = np.radians(float(fields[4])), np.radians(float(fields[5]))
            vectors.append((np.cos(latitude) * np.cos(longitude), np.cos(latitude) * np.sin(longitude), np.sin(latitude)))
            labels.append((fields[1], fields[8]))
    return np.asarray(vectors, dtype=np.float32), labels


def nearest_cities(coordinates: list[tuple[float, float]], vectors: np.ndarray, labels: list[tuple[str, str]]) -> list[tuple[str, str, float]]:
    """Find nearest cities in batches so a large library does not become a slow loop."""
    resolved: list[tuple[str, str, float]] = []
    for start in range(0, len(coordinates), 64):
        batch = np.asarray(coordinates[start:start + 64], dtype=np.float32)
        latitude, longitude = np.radians(batch[:, 0]), np.radians(batch[:, 1])
        points = np.column_stack((np.cos(latitude) * np.cos(longitude), np.cos(latitude) * np.sin(longitude), np.sin(latitude)))
        similarities = points @ vectors.T
        indexes = np.argmax(similarities, axis=1)
        for point, index in zip(points, indexes, strict=True):
            distance = float(np.arccos(np.clip(vectors[index] @ point, -1, 1)) * 6371.0088)
            resolved.append((*labels[int(index)], distance))
    return resolved


def enrich(database: Path, cities_path: Path, max_city_distance: float) -> None:
    vectors, labels = load_cities(cities_path)
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        scan_id = connection.execute(
            "SELECT scan_id FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY scan_id DESC LIMIT 1"
        ).fetchone()[0]
        paths = [(Path(row[0]), row[1]) for row in connection.execute(
            "SELECT path, media_kind FROM media_files WHERE scan_id = ? AND media_kind IN ('image', 'video') ORDER BY relative_path", [scan_id]
        )]
        tagged: list[tuple[Path, tuple[float, float]]] = []
        for number, (path, kind) in enumerate(paths, start=1):
            coordinates = gps_coordinates(path) if kind == "image" else video_gps_coordinates(path)
            if coordinates:
                tagged.append((path, coordinates))
            if number % 500 == 0:
                print(f"Read GPS from {number:,} / {len(paths):,} media files")
        resolved = nearest_cities([coordinates for _, coordinates in tagged], vectors, labels)
        rows = [
            (scan_id, str(path), latitude, longitude, city if distance <= max_city_distance else None, country, distance)
            for (path, (latitude, longitude)), (city, country, distance) in zip(tagged, resolved, strict=True)
        ]
        connection.execute("DELETE FROM media_locations WHERE scan_id = ?", [scan_id])
        connection.executemany("INSERT INTO media_locations VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        connection.commit()
    labelled = sum(row[4] is not None for row in rows)
    print(f"Stored GPS for {len(rows):,} images; {labelled:,} received a city label within {max_city_distance:.0f} km.")


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=project / "data" / "photo_catalogue.db")
    parser.add_argument("--cities", type=Path, default=project / ".cache" / "geonames" / "cities500.txt")
    parser.add_argument("--max-city-distance", type=float, default=75)
    args = parser.parse_args()
    if args.max_city_distance <= 0:
        parser.error("--max-city-distance must be positive")
    enrich(args.database, args.cities, args.max_city_distance)


if __name__ == "__main__":
    main()
