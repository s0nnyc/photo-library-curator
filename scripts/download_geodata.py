"""Download the offline GeoNames city dataset used for local location labels."""

from __future__ import annotations

import argparse
import shutil
import urllib.request
import zipfile
from pathlib import Path

URL = "https://download.geonames.org/export/dump/cities500.zip"
ATTRIBUTION = "GeoNames cities500, CC BY 4.0 — https://www.geonames.org/"


def download(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    city_file = destination / "cities500.txt"
    if city_file.is_file():
        print(f"GeoNames dataset already available: {city_file}")
        return
    archive = destination / "cities500.zip"
    print("Downloading GeoNames cities500 for offline location matching…")
    with urllib.request.urlopen(URL, timeout=120) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    with zipfile.ZipFile(archive) as zip_file:
        zip_file.extract("cities500.txt", destination)
    archive.unlink()
    (destination / "ATTRIBUTION.txt").write_text(ATTRIBUTION + "\n", encoding="utf-8")
    print(f"Downloaded {city_file}")
    print(ATTRIBUTION)


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=project / ".cache" / "geonames")
    args = parser.parse_args()
    download(args.destination)


if __name__ == "__main__":
    main()
