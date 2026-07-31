import pytest

from app.ranges import InvalidRange, parse_range_header


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, (0, 99)),
        ("bytes=10-19", (10, 19)),
        ("Bytes=10-", (10, 99)),
        ("bytes=-10", (90, 99)),
        ("bytes=90-200", (90, 99)),
    ],
)
def test_parse_range(header, expected):
    result = parse_range_header(header, 100)
    assert (result.start, result.end) == expected


@pytest.mark.parametrize(
    "header",
    [
        "items=0-1",
        "bytes=",
        "bytes=10-9",
        "bytes=100-",
        "bytes=-0",
        "bytes=0-1,3-4",
    ],
)
def test_rejects_invalid_range(header):
    with pytest.raises(InvalidRange):
        parse_range_header(header, 100)
