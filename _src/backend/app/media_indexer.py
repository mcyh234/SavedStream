from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .database import Database
from .telebox_client import TeleBoxClient, TelegramUnavailable


class MediaIndexer:
    """Background Telegram-to-SQLite indexer.

    The gallery never calls Telegram for a list.  This worker performs the
    initial backfill and then periodically catches up from the per-account
    high-water mark.  All checkpoints are persisted so a restart resumes
    instead of starting over.
    """

    def __init__(self, database: Database, telegram: TeleBoxClient, replication: Any | None = None) -> None:
        self.database = database
        self.telegram = telegram
        self.replication = replication
        self._task: asyncio.Task[None] | None = None
        self._ingest_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._manual: dict[str, asyncio.Task[None]] = {}
        self._account_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="savedstream-media-indexer")
        self._ingest_task = asyncio.create_task(
            self._run_ingest_reconciler(),
            name="savedstream-ingest-reconciler",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._ingest_task:
            self._ingest_task.cancel()
            await asyncio.gather(self._ingest_task, return_exceptions=True)
            self._ingest_task = None
        tasks = list(self._manual.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._manual.clear()

    async def _run(self) -> None:
        # A short delay lets the API become healthy before a potentially large
        # first backfill starts.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=2)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                accounts = (await self.telegram.accounts()).get("items", [])
                for account in accounts:
                    if str(account.get("state")) != "authenticated":
                        continue
                    await self.sync_account(str(account["id"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Per-account errors are persisted by sync_account; a failed
                # account must not stop indexing other accounts.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue

    async def _run_ingest_reconciler(self) -> None:
        # Helper Bot imports should become visible without waiting for the
        # normal 60-second Telegram index pass.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=1)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self.reconcile_completed_ingest_jobs()
                await self.sync_review_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.database.update_ingest_reconcile_state(
                    last_run_at=datetime.now(timezone.utc).isoformat(),
                    error=str(exc),
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                continue

    async def reconcile_completed_ingest_jobs(self) -> dict[str, Any]:
        """Index completed Helper Bot jobs without bypassing media review.

        New jobs carry an explicit requested/review state.  A public request is
        indexed as private/pending until an administrator approves it.  Jobs
        produced by an older bridge without those fields retain a small
        backwards-compatible path so an upgrade can reconcile already-created
        rows; the database migration still demotes legacy public rows.
        """

        state = await self.database.get_ingest_reconcile_state()
        updated_after = int(state.get("last_updated_at") or 0)
        after_job_id = int(state.get("last_job_id") or 0)
        processed = 0
        affected_accounts: set[str] = set()
        last_error: str | None = None
        try:
            while True:
                payload = await self.telegram.jobs(
                    status="completed",
                    updated_after=updated_after,
                    after_job_id=after_job_id,
                    limit=100,
                )
                jobs = list(payload.get("items") or [])
                if not jobs:
                    break
                for raw_job in jobs:
                    job = dict(raw_job)
                    job_id = int(job.get("id") or 0)
                    job_updated_at = int(job.get("updated_at") or 0)
                    account_id = str(job.get("account_id") or "").strip()
                    saved_message_id = int(job.get("saved_message_id") or 0)
                    submitter_id = str(
                        job.get("submitter_telegram_user_id")
                        or job.get("source_chat_id")
                        or ""
                    ).strip()
                    try:
                        if not job_id or not job_updated_at or not account_id or not saved_message_id:
                            # A malformed completed row cannot be repaired by
                            # retrying forever.  Advance past it and retain an
                            # operator-visible error in the reconciliation state.
                            raise ValueError(f"Malformed completed ingest job #{job_id or 'unknown'}")
                        _, item = await self.telegram.get_media_message(account_id, saved_message_id)
                        item = dict(item)
                        item["account_id"] = account_id
                        requested_visibility = str(job.get("requested_visibility") or "").strip()
                        job_review_status = str(job.get("review_status") or "").strip()
                        if requested_visibility in {"private", "public"}:
                            # The bridge is the source of the user's choice;
                            # SavedStream is the source of administrator review.
                            review_status = (
                                job_review_status
                                if job_review_status in {"not_required", "pending", "approved", "rejected", "revoked"}
                                else ("pending" if requested_visibility == "public" else "not_required")
                            )
                            visibility = "public" if review_status == "approved" else "private"
                        else:
                            # Legacy bridge compatibility.  New Helper Bot
                            # jobs never enter this branch.
                            user = await self.database.get_media_user(submitter_id) if submitter_id else None
                            visibility = (
                                "public"
                                if user
                                and str(user.get("status")) == "approved"
                                and str(user.get("account_id")) == account_id
                                else "private"
                            )
                            requested_visibility = "public" if visibility == "public" else "private"
                            review_status = "approved" if visibility == "public" else "not_required"
                        await self.database.upsert_media_index(
                            item,
                            visibility=visibility,
                            source_ingest_job_id=job_id,
                            submitter_telegram_user_id=submitter_id or None,
                            requested_visibility=requested_visibility,
                            review_status=review_status,
                            review_batch_id=str(job.get("review_batch_id") or "") or None,
                            account_group_id=str((await self.database.account_group_for_account(account_id) or {}).get("id") or "") or None,
                        )
                        if self.replication:
                            try:
                                await self.replication.enqueue_media(account_id, saved_message_id)
                            except Exception:
                                pass
                        affected_accounts.add(account_id)
                        processed += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_error = f"Helper ingest job #{job_id or 'unknown'}: {exc}"
                        # Keep transiently unavailable public requests in the
                        # administrator queue using the durable job metadata.
                        # Permanent Telegram deletions are intentionally not
                        # materialized as media rows.
                        if not self._is_permanent_ingest_error(exc):
                            try:
                                placeholder = await self.database.upsert_ingest_review_placeholder(
                                    job,
                                    error=str(exc),
                                )
                                if placeholder:
                                    affected_accounts.add(account_id)
                            except Exception as placeholder_exc:
                                last_error = (
                                    f"{last_error}; "
                                    f"placeholder failed: {placeholder_exc}"
                                )
                        # One stale/deleted Telegram message must not block
                        # newer completed imports from entering the review
                        # queue.  The cursor is still advanced and the error
                        # remains visible in the sync state for operators.
                    updated_after = job_updated_at
                    after_job_id = job_id
                    state = await self.database.update_ingest_reconcile_state(
                        last_updated_at=updated_after,
                        last_job_id=after_job_id,
                        last_run_at=datetime.now(timezone.utc).isoformat(),
                        error=last_error,
                    )
                if not payload.get("has_more"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)
            state = await self.database.update_ingest_reconcile_state(
                last_updated_at=updated_after,
                last_job_id=after_job_id,
                last_run_at=datetime.now(timezone.utc).isoformat(),
                error=last_error,
            )
        finally:
            for account_id in affected_accounts:
                await self.database.rebuild_timeline(account_id)
        if last_error is None:
            state = await self.database.update_ingest_reconcile_state(
                last_updated_at=updated_after,
                last_job_id=after_job_id,
                last_run_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
        return {**state, "processed": processed}

    @staticmethod
    def _is_permanent_ingest_error(error: Exception) -> bool:
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "not found",
                "message was deleted",
                "message is no longer available",
                "message_id_invalid",
                "message id is invalid",
                "stale telegram message",
            )
        )

    async def sync_review_outbox(self) -> int:
        """Push administrator decisions to TeleBox and retry durable failures."""
        synced = 0
        for row in await self.database.list_review_sync_outbox(limit=100):
            try:
                updater = getattr(self.telegram, "update_ingest_job_review")
                await updater(
                    int(row["job_id"]),
                    decision=str(row["decision"]),
                    reason=row.get("reason"),
                    reviewed_by=str(row.get("reviewed_by") or "admin"),
                )
                await self.database.mark_review_sync_success(int(row["job_id"]))
                synced += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts = int(row.get("attempts") or 0)
                await self.database.mark_review_sync_failure(
                    int(row["job_id"]),
                    str(exc),
                    delay_seconds=min(900, 15 * (2 ** min(attempts, 6))),
                )
        return synced

    async def request_sync(self, account_id: str, *, full: bool = False) -> dict[str, Any]:
        existing = self._manual.get(account_id)
        if existing and not existing.done():
            return await self.database.get_sync_state(account_id)

        task = asyncio.create_task(self.sync_account(account_id, full=full), name=f"media-sync-{account_id}")
        self._manual[account_id] = task
        try:
            return await task
        finally:
            self._manual.pop(account_id, None)

    def schedule_sync(self, account_id: str, *, full: bool = False) -> bool:
        existing = self._manual.get(account_id)
        if existing and not existing.done():
            return False

        async def runner() -> None:
            try:
                await self.sync_account(account_id, full=full)
            finally:
                self._manual.pop(account_id, None)

        self._manual[account_id] = asyncio.create_task(runner(), name=f"media-sync-{account_id}")
        return True

    async def sync_account(self, account_id: str, *, full: bool = False) -> dict[str, Any]:
        lock = self._account_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            return await self._sync_account(account_id, full=full)

    async def _sync_account(self, account_id: str, *, full: bool = False) -> dict[str, Any]:
        state = await self.database.get_sync_state(account_id)
        interrupted_full = (
            state.get("mode") == "full"
            and state.get("status") in {"running", "paused", "error"}
            and not state.get("last_sync_at")
        )
        mode = "full" if full or state.get("status") == "never" or not state.get("last_sync_at") else "incremental"
        cursor = state.get("cursor") if mode == "full" and (interrupted_full or not full) else None
        after_id = None if mode == "full" else state.get("high_watermark_id")
        indexed_count = 0 if full else int(state.get("indexed_count") or 0)
        seen_message_ids: set[int] = set()
        await self.database.update_sync_state(
            account_id,
            status="running",
            mode=mode,
            cursor=cursor,
            error=None,
        )
        max_seen = int(after_id or state.get("high_watermark_id") or 0)
        try:
            while True:
                payload = await self.telegram.sync_saved_media(
                    account_id=account_id,
                    mode=mode,
                    cursor=cursor,
                    after_id=after_id,
                    limit=200,
                )
                items = payload.get("items", [])
                if mode == "full":
                    seen_message_ids.update(
                        int(message_id)
                        for message_id in payload.get("message_ids", [])
                        if int(message_id) > 0
                    )
                for raw in items:
                    item = dict(raw)
                    item["account_id"] = account_id
                    await self.database.upsert_media_index(item)
                    if mode == "full":
                        seen_message_ids.add(int(item.get("id") or 0))
                    max_seen = max(max_seen, int(item.get("id") or 0))
                    indexed_count += 1
                await self.database.update_sync_state(
                    account_id,
                    status="running",
                    mode=mode,
                    cursor=payload.get("next_cursor") if mode == "full" else None,
                    high_watermark_id=max_seen or None,
                    indexed_count=indexed_count,
                )
                next_cursor = payload.get("next_cursor")
                if not payload.get("has_more") or not next_cursor:
                    break
                if mode == "full":
                    cursor = int(next_cursor)
                else:
                    after_id = int(next_cursor)
            if mode == "full":
                await self.database.mark_media_missing(account_id, seen_message_ids)
            await self.database.rebuild_timeline(account_id)
            return await self.database.update_sync_state(
                account_id,
                status="ready",
                mode="incremental",
                cursor=None,
                high_watermark_id=max_seen or None,
                indexed_count=indexed_count,
                last_sync_at=datetime.now(timezone.utc).isoformat(),
                error=None,
            )
        except asyncio.CancelledError:
            await self.database.update_sync_state(account_id, status="paused", error="sync cancelled")
            raise
        except Exception as exc:
            await self.database.update_sync_state(account_id, status="error", error=str(exc))
            return await self.database.get_sync_state(account_id)
