from pathlib import Path

from sort_library import safe_name, unique_destination


def test_safe_name_removes_path_separators() -> None:
    assert safe_name("City/Name\\Country") == "City_Name_Country"


def test_unique_destination_adds_suffix_for_collisions(tmp_path: Path) -> None:
    occupied = set()
    first = unique_destination(tmp_path, "IMG_0001.JPG", occupied, tmp_path / "original.JPG")
    second = unique_destination(tmp_path, "IMG_0001.JPG", occupied, tmp_path / "original.JPG")
    assert first.name == "IMG_0001.JPG"
    assert second.name == "IMG_0001__2.JPG"


def test_unique_destination_keeps_file_already_at_its_destination(tmp_path: Path) -> None:
    current = tmp_path / "IMG_0001.JPG"
    current.touch()
    assert unique_destination(tmp_path, "IMG_0001.JPG", set(), current) == current
