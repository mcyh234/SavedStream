from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.telegram_service import MESSAGE_SCAN_LIMIT, TelegramService


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def iter_messages(self, entity, **kwargs):
        self.calls.append((entity, kwargs))

        async def iterator():
            cursor = kwargs.get("min_id") or kwargs.get("offset_id")
            reverse = kwargs["reverse"]
            messages = sorted(self.messages, key=lambda message: message.id, reverse=not reverse)
            for message in messages:
                if cursor and ((reverse and message.id <= cursor) or (not reverse and message.id >= cursor)):
                    continue
                yield message

        return iterator()


class FakeTelegramService(TelegramService):
    async def authorized_client(self):
        return self.client

    def _serialize_message(self, message):
        return message.item


def make_service(tmp_path, messages):
    settings = Settings(1, "hash", "admin", tmp_path, False, 30)
    service = FakeTelegramService(settings)
    service.client = FakeClient(messages)
    return service


@pytest.mark.asyncio
async def test_filtered_pagination_does_not_skip_extra_match(tmp_path):
    messages = [
        SimpleNamespace(id=10, item=None),
        SimpleNamespace(id=9, item={"id": 9, "kind": "video"}),
        SimpleNamespace(id=8, item={"id": 8, "kind": "image"}),
        SimpleNamespace(id=7, item={"id": 7, "kind": "video"}),
        SimpleNamespace(id=6, item={"id": 6, "kind": "video"}),
    ]
    service = make_service(tmp_path, messages)

    first, cursor, has_more = await service.list_saved_media(
        limit=2, cursor=None, order="newest", kind="video", query=""
    )
    second, next_cursor, second_has_more = await service.list_saved_media(
        limit=2, cursor=cursor, order="newest", kind="video", query=""
    )

    assert [item["id"] for item in first] == [9, 7]
    assert cursor == 7
    assert has_more is True
    assert [item["id"] for item in second] == [6]
    assert next_cursor is None
    assert second_has_more is False


@pytest.mark.asyncio
async def test_scan_window_returns_resume_cursor(tmp_path):
    messages = [SimpleNamespace(id=value, item=None) for value in range(1000, 499, -1)]
    service = make_service(tmp_path, messages)

    items, cursor, has_more = await service.list_saved_media(
        limit=20, cursor=None, order="newest", kind="all", query=""
    )

    assert items == []
    assert cursor == 501
    assert has_more is True
    assert service.client.calls[0][1]["limit"] == MESSAGE_SCAN_LIMIT + 1


@pytest.mark.asyncio
async def test_oldest_cursor_uses_min_id(tmp_path):
    service = make_service(tmp_path, [])
    await service.list_saved_media(
        limit=20, cursor=12, order="oldest", kind="all", query=""
    )
    assert service.client.calls[0][1]["min_id"] == 12


def test_document_camera_file_is_serialized_as_timeline_image(tmp_path):
    settings = Settings(1, "hash", "admin", tmp_path, False, 30)
    service = TelegramService(settings)
    message = SimpleNamespace(
        id=91,
        media=SimpleNamespace(document=SimpleNamespace(thumbs=[]), sizes=None),
        file=SimpleNamespace(
            size=11 * 1024 * 1024,
            mime_type="application/octet-stream",
            name="IMG_20250923_003303_054.jpg",
            ext=".jpg",
            duration=None,
            width=4032,
            height=3024,
        ),
        video=None,
        photo=None,
        audio=None,
        raw_text="",
        date=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
    )

    item = service._serialize_message(message)

    assert item is not None
    assert item["kind"] == "image"
    assert item["mime_type"] == "image/jpeg"
    assert item["filename"] == "IMG_20250923_003303_054.jpg"
    assert item["date"] == "2025-09-23T00:33:03.054000+00:00"
