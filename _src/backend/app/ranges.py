from __future__ import annotations

from dataclasses import dataclass


class InvalidRange(ValueError):
    pass


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"


def parse_range_header(header: str | None, total: int) -> ByteRange:
    if total <= 0:
        raise InvalidRange("The media file is empty")
    if not header:
        return ByteRange(0, total - 1, total)
    unit, separator, value = header.partition("=")
    if separator != "=" or unit.strip().lower() != "bytes" or "," in value:
        raise InvalidRange("Only one byte range is supported")

    value = value.strip()
    if "-" not in value:
        raise InvalidRange("Malformed Range header")
    start_text, end_text = value.split("-", 1)

    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise InvalidRange("Invalid suffix range")
            suffix_length = min(suffix_length, total)
            return ByteRange(total - suffix_length, total - 1, total)

        start = int(start_text.strip())
        end = int(end_text.strip()) if end_text.strip() else total - 1
    except ValueError as exc:
        raise InvalidRange("Malformed Range header") from exc

    if start < 0 or start >= total or end < start:
        raise InvalidRange("Range is outside the media file")
    return ByteRange(start, min(end, total - 1), total)
