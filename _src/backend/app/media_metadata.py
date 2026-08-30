from __future__ import annotations

import re
from datetime import datetime, timezone


GENERIC_BINARY_MIME_TYPES = {
    "",
    "application/binary",
    "application/force-download",
    "application/octet-stream",
    "application/x-binary",
    "binary/octet-stream",
}


_EXTENSION_MEDIA_TYPES: dict[str, tuple[str, str]] = {
    # Images commonly uploaded by phones, cameras and image editors.
    ".avif": ("image", "image/avif"),
    ".bmp": ("image", "image/bmp"),
    ".dng": ("image", "image/x-adobe-dng"),
    ".gif": ("image", "image/gif"),
    ".heic": ("image", "image/heic"),
    ".heif": ("image", "image/heif"),
    ".ico": ("image", "image/x-icon"),
    ".jfif": ("image", "image/jpeg"),
    ".jpe": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".jpg": ("image", "image/jpeg"),
    ".jxl": ("image", "image/jxl"),
    ".png": ("image", "image/png"),
    ".svg": ("image", "image/svg+xml"),
    ".tif": ("image", "image/tiff"),
    ".tiff": ("image", "image/tiff"),
    ".webp": ("image", "image/webp"),
    # Video containers commonly produced by Android/iOS cameras and exports.
    ".3g2": ("video", "video/3gpp2"),
    ".3gp": ("video", "video/3gpp"),
    ".avi": ("video", "video/x-msvideo"),
    ".flv": ("video", "video/x-flv"),
    ".m2ts": ("video", "video/mp2t"),
    ".m4v": ("video", "video/x-m4v"),
    ".mkv": ("video", "video/x-matroska"),
    ".mov": ("video", "video/quicktime"),
    ".mp4": ("video", "video/mp4"),
    ".mpe": ("video", "video/mpeg"),
    ".mpeg": ("video", "video/mpeg"),
    ".mpg": ("video", "video/mpeg"),
    ".mts": ("video", "video/mp2t"),
    ".ogv": ("video", "video/ogg"),
    ".ts": ("video", "video/mp2t"),
    ".vob": ("video", "video/dvd"),
    ".webm": ("video", "video/webm"),
    ".wmv": ("video", "video/x-ms-wmv"),
    # Audio inference keeps document uploads consistent with existing filters.
    ".aac": ("audio", "audio/aac"),
    ".flac": ("audio", "audio/flac"),
    ".m4a": ("audio", "audio/mp4"),
    ".mp3": ("audio", "audio/mpeg"),
    ".oga": ("audio", "audio/ogg"),
    ".ogg": ("audio", "audio/ogg"),
    ".opus": ("audio", "audio/opus"),
    ".wav": ("audio", "audio/wav"),
    ".wma": ("audio", "audio/x-ms-wma"),
}


# Android/iOS/camera names generally use an eight-digit date followed by a
# six-digit local wall-clock time.  The optional suffix is normally
# milliseconds, e.g. IMG_20250923_003303_054.jpg.  Boundaries prevent hashes
# or longer numeric identifiers from being interpreted as capture times.
_COMPACT_CAPTURE_TIMESTAMP = re.compile(
    r"(?<!\d)(?P<date>(?:19|20)\d{6})[_-](?P<time>\d{6})"
    r"(?:[_-]?(?P<fraction>\d{1,6}))?(?!\d)",
    re.IGNORECASE,
)

# Also recognize the common Screenshot_2025-09-23-00-33-03 form without
# weakening the validation applied to compact camera filenames.
_SEPARATED_CAPTURE_TIMESTAMP = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})[-_](?P<month>\d{2})[-_]"
    r"(?P<day>\d{2})[T _-](?P<hour>\d{2})[-_:](?P<minute>\d{2})"
    r"[-_:](?P<second>\d{2})(?:[._-](?P<fraction>\d{1,6}))?(?!\d)",
    re.IGNORECASE,
)


def _extension(filename: str | None) -> str:
    name = str(filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def media_type_from_filename(filename: str | None) -> tuple[str, str] | None:
    """Return the gallery kind and canonical MIME for a known media suffix."""

    return _EXTENSION_MEDIA_TYPES.get(_extension(filename))


def normalize_media_mime_type(mime_type: str | None, filename: str | None) -> str:
    """Fill missing/generic document MIME metadata from the original name.

    Telegram document uploads keep the original filename, but clients do not
    always supply a MIME type (HEIC/MKV are common examples).  Only generic
    binary MIME values are replaced so a specific server-provided type is not
    silently contradicted by a misleading extension.
    """

    supplied = str(mime_type or "").strip()
    bare = supplied.split(";", 1)[0].strip().lower()
    inferred = media_type_from_filename(filename)
    if bare in GENERIC_BINARY_MIME_TYPES and inferred:
        return inferred[1]
    return supplied or "application/octet-stream"


def infer_media_kind(
    kind: str | None,
    mime_type: str | None,
    filename: str | None,
) -> str:
    """Classify Telegram photos and document-backed media consistently."""

    supplied_kind = str(kind or "").strip().lower()
    bare_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    for candidate in ("image", "video", "audio"):
        if bare_mime.startswith(f"{candidate}/"):
            return candidate

    if bare_mime not in GENERIC_BINARY_MIME_TYPES:
        return "file"

    # Extension inference is intentionally limited to absent/generic MIME
    # metadata.  This recognizes an IMG_*.jpg sent as a Telegram document while
    # avoiding treating an explicitly typed PDF as an image solely by its name.
    inferred = media_type_from_filename(filename)
    if inferred:
        return inferred[0]
    if supplied_kind in {"image", "video", "audio", "file"}:
        return supplied_kind
    return "file"


def capture_datetime_from_filename(filename: str | None) -> datetime | None:
    """Parse a camera/screenshot capture timestamp from an original filename.

    Filenames do not carry an offset.  The timestamp is stored as UTC without
    shifting the wall-clock fields, preserving the literal capture day used by
    the timeline instead of moving shortly-after-midnight captures to the
    previous day.
    """

    name = str(filename or "").strip()
    compact = _COMPACT_CAPTURE_TIMESTAMP.search(name)
    try:
        if compact:
            date_token = compact.group("date")
            time_token = compact.group("time")
            fraction = compact.group("fraction") or ""
            microsecond = int(fraction.ljust(6, "0")) if fraction else 0
            return datetime(
                int(date_token[0:4]),
                int(date_token[4:6]),
                int(date_token[6:8]),
                int(time_token[0:2]),
                int(time_token[2:4]),
                int(time_token[4:6]),
                microsecond=microsecond,
                tzinfo=timezone.utc,
            )

        separated = _SEPARATED_CAPTURE_TIMESTAMP.search(name)
        if separated:
            fraction = separated.group("fraction") or ""
            microsecond = int(fraction.ljust(6, "0")) if fraction else 0
            return datetime(
                int(separated.group("year")),
                int(separated.group("month")),
                int(separated.group("day")),
                int(separated.group("hour")),
                int(separated.group("minute")),
                int(separated.group("second")),
                microsecond=microsecond,
                tzinfo=timezone.utc,
            )
    except ValueError:
        # Invalid calendar dates/times fall back to the Telegram message date.
        return None
    return None


def preferred_media_date(
    filename: str | None,
    message_date: str | datetime | None,
    kind: str | None,
) -> str | datetime | None:
    """Prefer embedded capture time for images/videos, then Telegram time."""

    if str(kind or "").lower() in {"image", "video"}:
        captured = capture_datetime_from_filename(filename)
        if captured:
            return captured.isoformat()
    return message_date
