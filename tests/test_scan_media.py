from scan_media import filename_group, media_kind


def test_media_kind_recognises_supported_types() -> None:
    assert media_kind(".HEIC".lower()) == "image"
    assert media_kind(".mov") == "video"
    assert media_kind(".aae") == "sidecar"


def test_filename_group_detects_common_sources() -> None:
    assert filename_group("IMG_1234.JPG", "image") == "camera_generated"
    assert filename_group("signal-2025-11-30.jpg", "image") == "messaging_export"
    assert filename_group("IMG_1234.AAE", "sidecar") == "iphone_edit_sidecar"
