from __future__ import annotations

from datetime import datetime, timezone

from app.media_metadata import (
    capture_datetime_from_filename,
    infer_media_kind,
    normalize_media_mime_type,
    preferred_media_date,
)


def test_camera_filename_timestamp_includes_milliseconds() -> None:
    captured = capture_datetime_from_filename("IMG_20250923_003303_054.jpg")

    assert captured == datetime(2025, 9, 23, 0, 33, 3, 54_000, tzinfo=timezone.utc)


def test_camera_filename_timestamp_supports_video_and_compact_pixel_names() -> None:
    video = capture_datetime_from_filename("VID_20250923_235959.mp4")
    pixel = capture_datetime_from_filename("PXL_20250923_003303054.jpg")
    screenshot = capture_datetime_from_filename("Screenshot_2025-09-23-00-33-03.png")

    assert video == datetime(2025, 9, 23, 23, 59, 59, tzinfo=timezone.utc)
    assert pixel == datetime(2025, 9, 23, 0, 33, 3, 54_000, tzinfo=timezone.utc)
    assert screenshot == datetime(2025, 9, 23, 0, 33, 3, tzinfo=timezone.utc)


def test_invalid_filename_timestamp_falls_back_to_telegram_date() -> None:
    telegram_date = "2026-08-30T10:20:30+00:00"

    assert capture_datetime_from_filename("IMG_20250230_256199_054.jpg") is None
    assert preferred_media_date(
        "IMG_20250230_256199_054.jpg",
        telegram_date,
        "image",
    ) == telegram_date


def test_document_media_kind_and_mime_are_inferred_from_original_extension() -> None:
    assert infer_media_kind("file", "application/octet-stream", "IMG_20250923_003303_054.jpg") == "image"
    assert normalize_media_mime_type("application/octet-stream", "IMG_20250923_003303_054.jpg") == "image/jpeg"
    assert infer_media_kind("file", "application/octet-stream", "VID_20250923_003303_054.MOV") == "video"
    assert normalize_media_mime_type("application/octet-stream", "VID_20250923_003303_054.MOV") == "video/quicktime"
    assert infer_media_kind("file", "application/octet-stream", "camera.HEIC") == "image"


def test_specific_non_media_mime_is_not_overridden_by_extension() -> None:
    assert infer_media_kind("file", "application/pdf", "misleading.jpg") == "file"
    assert infer_media_kind("image", "application/pdf", "misleading.jpg") == "file"
    assert normalize_media_mime_type("application/pdf", "misleading.jpg") == "application/pdf"
