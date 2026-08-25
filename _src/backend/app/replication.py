from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .auth import AuthStore
from .database import Database
from .telebox_client import TeleBoxClient, TelegramUnavailable


REPLICA_MARKER = "#savedstream-replica:v1"
HEALTH_INTERVAL_SECONDS = 10
HEALTH_FAILURE_THRESHOLD = 3
DEFAULT_MIN_INTERVAL_MS = 3000
DEFAULT_MAX_PER_MINUTE = 10
MIN_SAFE_INTERVAL_MS = 1500
MAX_SAFE_PER_MINUTE = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(item: dict[str, Any]) -> str:
    digest = str(item.get("content_sha256") or "").strip().lower()
    if digest:
        return digest
    raw = "|".join(
        str(item.get(key) or "")
        for key in ("size", "mime_type", "filename", "original_title", "date")
    )
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


class DisasterRecoveryManager:
    """Durable logical-account replication and failover coordinator.

    Telegram file copies are performed inside TeleBox.  This manager owns the
    durable queue, source metadata, progress state, and active-account route.
    """

    def __init__(self, database: Database, telegram: TeleBoxClient) -> None:
        self.database = database
        self.telegram = telegram
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._target_last_copy: dict[str, float] = {}
        self._target_copy_times: dict[str, list[float]] = {}

    async def start(self) -> None:
        if self._tasks:
            return
        await self.database.requeue_running_replication_jobs()
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._queue_loop(), name="replication-queue"),
            asyncio.create_task(self._backfill_loop(), name="replication-backfill"),
            asyncio.create_task(self._health_loop(), name="replication-health"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def ensure_groups(self) -> None:
        """Create a safe one-account group for legacy TeleBox accounts."""
        try:
            accounts = (await self.telegram.accounts()).get("items", [])
        except Exception:
            return
        existing = {str(member.get("account_id")) for group in await self.database.list_account_groups() for member in group.get("members", [])}
        for account in accounts:
            account_id = str(account.get("id") or "").strip()
            if not account_id or account_id in existing:
                continue
            group_id = f"account-{account_id}"
            try:
                await self.database.ensure_account_group(group_id, name=str(account.get("label") or group_id), primary_account_id=account_id)
            except Exception:
                continue

    async def enqueue_media(self, account_id: str, message_id: int) -> int:
        group = await self.database.account_group_for_account(account_id)
        if not group:
            return 0
        item = await self.database.get_media_index(account_id, message_id, include_deleted=True, include_provenance=True)
        if not item:
            return 0
        logical_id = str(item.get("logical_media_id") or f"{account_id}:{message_id}")
        fingerprint = _fingerprint(item)
        return await self.database.enqueue_replication_for_media(
            group_id=str(group["id"]),
            source_account_id=account_id,
            source_message_id=message_id,
            logical_media_id=logical_id,
            fingerprint=fingerprint,
            job_type="live",
        )

    async def enqueue_mutation(self, account_id: str, message_id: int, action: str, *, caption: str | None = None) -> int:
        group = await self.database.account_group_for_account(account_id)
        if not group or action not in {"delete", "caption", "hide", "private", "public"}:
            return 0
        source_item = await self.database.get_media_index(account_id, message_id, include_deleted=True, include_provenance=True)
        logical_id = str((source_item or {}).get("logical_media_id") or f"{account_id}:{message_id}")
        # A row may have originated on the primary account even after a
        # failover. Resolve mappings by logical identity rather than by the
        # currently active physical account.
        mapping_rows = await self.database.list_replication_mappings_for_logical(str(group["id"]), logical_id)
        created = 0
        for mapping in mapping_rows:
            target = str(mapping.get("target_account_id") or "")
            if target == account_id or not target:
                continue
            key = f"mutation:{group['id']}:{logical_id}:{target}:{action}:{caption or ''}"
            await self.database.create_replication_job({
                "group_id": group["id"],
                "job_type": "mutation",
                "source_account_id": account_id,
                "source_message_id": int(message_id),
                "target_account_id": target,
                "logical_media_id": logical_id,
                "idempotency_key": key,
                "mutation_action": action,
                "mutation_caption": caption,
            })
            created += 1
        return created

    async def _queue_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._process_queued_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                continue

    async def _process_queued_jobs(self) -> None:
        # Retry-wait jobs are durable queue entries too.  Older versions only
        # queried ``queued`` here, which meant a transient Telegram/FloodWait
        # error was persisted forever without ever being retried.
        jobs = await self.database.list_replication_jobs(status="queued", limit=20)
        jobs.extend(await self.database.list_replication_jobs(status="retry_wait", limit=20))
        now = datetime.now(timezone.utc)
        for job in jobs:
            retry_at = str(job.get("next_retry_at") or "")
            if retry_at:
                try:
                    parsed = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
                    if parsed > now:
                        continue
                except ValueError:
                    pass
            await self._process_job(job)

    async def _process_job(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        claimed = await self.database.update_replication_job(job_id, status="running", phase="copying", progress=1)
        if not claimed or str(claimed.get("status")) != "running":
            return
        try:
            source = str(job.get("source_account_id") or "")
            target = str(job.get("target_account_id") or "")
            message_id = int(job.get("source_message_id") or 0)
            if not source or not target or not message_id:
                raise ValueError("replication job is missing source or target")
            if str(job.get("job_type")) == "mutation":
                mapping = await self.database.get_replication_mapping(str(job["group_id"]), target, str(job.get("logical_media_id") or ""))
                target_message_id = int((mapping or {}).get("target_message_id") or 0)
                if not target_message_id:
                    await self.database.update_replication_job(job_id, status="skipped", phase="missing_mapping", progress=100, completed_at=_now(), error=None)
                    return
                action = str(job.get("mutation_action") or "")
                if action in {"delete", "caption"}:
                    await self.telegram.replication_mutation({
                        "target_account_id": target,
                        "target_message_id": target_message_id,
                        "action": action,
                        "caption": job.get("mutation_caption"),
                    })
                    if action == "delete":
                        await self.database.update_replica_metadata(str(job["group_id"]), str(job.get("logical_media_id") or ""), visibility="private", hidden=True, deleted=True)
                elif action == "hide":
                    await self.database.update_replica_metadata(str(job["group_id"]), str(job.get("logical_media_id") or ""), visibility="private", hidden=True)
                elif action == "private":
                    await self.database.update_replica_metadata(str(job["group_id"]), str(job.get("logical_media_id") or ""), visibility="private", requested_visibility="private", review_status="revoked", hidden=False)
                elif action == "public":
                    await self.database.update_replica_metadata(str(job["group_id"]), str(job.get("logical_media_id") or ""), visibility="public", requested_visibility="public", review_status="approved", hidden=False)
                else:
                    raise ValueError(f"unsupported replication mutation: {action}")
                await self.database.update_replication_job(job_id, status="completed", phase="mutated", progress=100, completed_at=_now(), error=None)
                return
            group = await self.database.get_account_group(str(job["group_id"]))
            if not group:
                raise ValueError("logical account group not found")
            item = await self.database.get_media_index(source, message_id, include_deleted=True, include_provenance=True)
            if not item:
                _, item = await self.telegram.get_media_message(source, message_id)
                item = {**item, "account_id": source, "id": message_id}
            logical_id = str(job.get("logical_media_id") or item.get("logical_media_id") or f"{source}:{message_id}")
            fingerprint = str(job.get("fingerprint") or _fingerprint(item))
            existing = await self.database.get_replication_mapping(str(job["group_id"]), target, logical_id)
            if existing:
                await self.database.update_replication_job(job_id, status="completed", phase="deduplicated", progress=100, completed_at=_now(), error=None)
                if str(job.get("job_type")) == "backfill":
                    member = await self.database.account_group_for_account(target)
                    if member:
                        await self.database.update_account_group_member(
                            str(job["group_id"]), target,
                            processed_files=int(member.get("processed_files") or 0) + 1,
                            processed_bytes=int(member.get("processed_bytes") or 0) + int(item.get("size") or 0),
                            last_error=None,
                            last_sync_at=_now(),
                        )
                return
            await self._rate_limit(target, group)
            marker = "\n".join([
                REPLICA_MARKER,
                f"group={group['id']}",
                f"logical={logical_id}",
                f"sha256={fingerprint}",
                f"source={source}:{message_id}",
            ])
            copied = await self.telegram.replication_copy(
                source_account_id=source,
                target_account_id=target,
                source_message_id=message_id,
                logical_media_id=logical_id,
                fingerprint=fingerprint,
                filename=str(item.get("filename") or f"saved-{message_id}"),
                mime_type=str(item.get("mime_type") or "application/octet-stream"),
                caption=marker,
                idempotency_key=str(job.get("idempotency_key") or job_id),
            )
            target_message_id = int(copied.get("target_message_id") or copied.get("id") or 0)
            if not target_message_id:
                raise TelegramUnavailable("TeleBox did not return target message id")
            payload = {
                "group_id": group["id"],
                "logical_media_id": logical_id,
                "source_account_id": source,
                "source_message_id": message_id,
                "target_account_id": target,
                "target_message_id": target_message_id,
                "fingerprint": fingerprint,
                "content_sha256": copied.get("content_sha256") or item.get("content_sha256"),
                "size": int(copied.get("size") or item.get("size") or 0),
                "mime_type": item.get("mime_type"),
                "filename": item.get("filename"),
                "owner_user_id": item.get("owner_user_id"),
                "submitter_telegram_user_id": item.get("submitter_telegram_user_id"),
                "visibility": item.get("visibility") or "private",
                "requested_visibility": item.get("requested_visibility") or "private",
                "review_status": item.get("review_status") or "not_required",
                "hidden": 1 if item.get("hidden") else 0,
            }
            actual_sha = str(payload.get("content_sha256") or "").strip().lower()
            if actual_sha:
                await self.database.set_media_content_hash(source, message_id, actual_sha)
            await self.database.save_replication_mapping(payload)
            replica_item = {**item, **copied, "account_id": target, "id": target_message_id, "logical_media_id": logical_id, "content_sha256": payload["content_sha256"]}
            await self.database.upsert_media_index(
                replica_item,
                visibility=str(item.get("visibility") or "private"),
                submitter_telegram_user_id=item.get("submitter_telegram_user_id"),
                owner_user_id=int(item["owner_user_id"]) if item.get("owner_user_id") is not None else None,
                requested_visibility=str(item.get("requested_visibility") or "private"),
                review_status=str(item.get("review_status") or "not_required"),
                upload_source="replica",
                hidden=bool(item.get("hidden")),
                account_group_id=str(group["id"]),
                logical_media_id=logical_id,
                content_sha256=payload["content_sha256"],
                origin_account_id=source,
                origin_message_id=message_id,
            )
            await self.database.update_replication_job(job_id, status="completed", phase="indexed", progress=100, completed_at=_now(), error=None)
            if str(job.get("job_type")) == "backfill":
                member = await self.database.account_group_for_account(target)
                if member:
                    await self.database.update_account_group_member(str(group["id"]), target, processed_files=int(member.get("processed_files") or 0) + 1, processed_bytes=int(member.get("processed_bytes") or 0) + int(item.get("size") or 0), last_error=None, last_sync_at=_now())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempts = int(job.get("attempts") or 0) + 1
            flood_match = re.search(r"(?:FLOOD_WAIT[_ ]|wait(?: of)? )(?P<seconds>\d+)", str(exc), re.IGNORECASE)
            flood_delay = int(flood_match.group("seconds")) if flood_match else 0
            delay = max(flood_delay, min(3600, 2 ** min(attempts, 10)))
            status = "retry_wait" if attempts < 12 else "failed"
            await self.database.update_replication_job(job_id, status=status, phase="retry_wait" if status == "retry_wait" else "failed", attempts=attempts, error=str(exc), next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat() if status == "retry_wait" else None)

    async def _rate_limit(self, target: str, group: dict[str, Any]) -> None:
        # Settings are intentionally conservative until a dedicated settings
        # table is added; this keeps old installations safe on first upgrade.
        interval = max(MIN_SAFE_INTERVAL_MS, int(group.get("rate_min_interval_ms") or DEFAULT_MIN_INTERVAL_MS)) / 1000
        previous = self._target_last_copy.get(target)
        if previous is not None:
            wait = interval - (asyncio.get_running_loop().time() - previous)
            if wait > 0:
                await asyncio.sleep(wait)
        now = asyncio.get_running_loop().time()
        maximum = min(MAX_SAFE_PER_MINUTE, max(1, int(group.get("rate_max_messages_per_minute") or DEFAULT_MAX_PER_MINUTE)))
        recent = [stamp for stamp in self._target_copy_times.get(target, []) if now - stamp < 60]
        if len(recent) >= maximum:
            await asyncio.sleep(max(0, 60 - (now - recent[0])))
            now = asyncio.get_running_loop().time()
            recent = [stamp for stamp in recent if now - stamp < 60]
        recent.append(now)
        self._target_copy_times[target] = recent
        self._target_last_copy[target] = now

    async def _backfill_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.ensure_groups()
                groups = await self.database.list_account_groups()
                accounts = {str(item.get("id")): item for item in (await self.telegram.accounts()).get("items", [])}
                for group in groups:
                    source = str(group.get("active_account_id") or group.get("primary_account_id") or "")
                    for member in group.get("members", []):
                        if str(member.get("role")) != "replica" or not int(member.get("enabled") or 0):
                            continue
                        target = str(member.get("account_id"))
                        if str(accounts.get(target, {}).get("state")) != "authenticated":
                            continue
                        if str(member.get("sync_status")) in {"pending", "running"}:
                            await self._discover_backfill_page(group, member, source)
                            refreshed = await self.database.account_group_for_account(target)
                            if refreshed and str(refreshed.get("sync_status")) == "running" and refreshed.get("sync_cursor") is None:
                                pending = await self.database.list_replication_jobs(group_id=str(group["id"]), limit=500)
                                pending_target = [item for item in pending if str(item.get("target_account_id")) == target and str(item.get("job_type")) == "backfill" and str(item.get("status")) in {"queued", "running", "retry_wait"}]
                                failed_target = [item for item in pending if str(item.get("target_account_id")) == target and str(item.get("job_type")) == "backfill" and str(item.get("status")) == "failed"]
                                if failed_target:
                                    await self.database.update_account_group_member(str(group["id"]), target, sync_status="failed", last_error=str(failed_target[0].get("error") or "backfill job failed"))
                                elif not pending_target:
                                    await self.database.update_account_group_member(str(group["id"]), target, sync_status="ready", last_sync_at=_now())
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue

    async def _discover_backfill_page(self, group: dict[str, Any], member: dict[str, Any], source: str) -> None:
        target = str(member["account_id"])
        if str(member.get("sync_status")) == "pending":
            await self.database.update_account_group_member(str(group["id"]), target, sync_status="running", last_error=None)
        cursor = member.get("sync_cursor")
        payload = await self.telegram.sync_saved_media(
            account_id=source,
            mode="full",
            cursor=int(cursor) if cursor is not None else None,
            limit=50,
            order="oldest",
        )
        items = list(payload.get("items") or [])
        page_bytes = sum(int(item.get("size") or 0) for item in items)
        for raw in items:
            item = {**dict(raw), "account_id": source}
            message_id = int(item.get("id") or 0)
            if not message_id:
                continue
            await self.database.upsert_media_index(item, account_group_id=str(group["id"]), logical_media_id=f"{source}:{message_id}", origin_account_id=source, origin_message_id=message_id)
            await self.database.create_replication_job({
                "group_id": group["id"], "job_type": "backfill", "source_account_id": source,
                "source_message_id": message_id, "target_account_id": target,
                "logical_media_id": str(item.get("logical_media_id") or f"{source}:{message_id}"),
                "fingerprint": _fingerprint(item),
                "idempotency_key": f"backfill:{group['id']}:{source}:{message_id}:{target}",
            })
        next_cursor = payload.get("next_cursor")
        if payload.get("has_more") and next_cursor:
            await self.database.update_account_group_member(
                str(group["id"]), target, sync_cursor=int(next_cursor),
                total_files=int(member.get("total_files") or 0) + len(items),
                total_bytes=int(member.get("total_bytes") or 0) + page_bytes,
            )
        else:
            await self.database.update_account_group_member(
                str(group["id"]), target, sync_cursor=None, sync_status="running",
                total_files=int(member.get("total_files") or 0) + len(items),
                total_bytes=int(member.get("total_bytes") or 0) + page_bytes,
                last_sync_at=_now(),
            )

    async def _health_loop(self) -> None:
        while not self._stop.is_set():
            try:
                groups = await self.database.list_account_groups()
                accounts = {str(item.get("id")): item for item in (await self.telegram.accounts()).get("items", [])}
                for group in groups:
                    if not int(group.get("auto_failover_enabled") or 0):
                        continue
                    active = str(group.get("active_account_id") or "")
                    try:
                        health = await self.telegram.account_health(active)
                        if bool(health.get("healthy")):
                            if int(group.get("health_failures") or 0):
                                await self.database.update_account_group(str(group["id"]), health_failures=0, last_health_error=None, status="failed_over" if group.get("status") == "failed_over" else "healthy")
                            continue
                        raise TelegramUnavailable(str(health.get("error") or "account health check failed"))
                    except Exception as exc:
                        failures = int(group.get("health_failures") or 0) + 1
                        if failures < HEALTH_FAILURE_THRESHOLD:
                            await self.database.update_account_group(str(group["id"]), health_failures=failures, last_health_error=str(exc), status="degraded")
                            continue
                        candidates = [m for m in group.get("members", []) if str(m.get("role")) == "replica" and int(m.get("enabled") or 0) and str(m.get("sync_status")) == "ready" and str(accounts.get(str(m.get("account_id")), {}).get("state")) == "authenticated"]
                        candidates.sort(key=lambda m: (int(m.get("priority") or 100), str(m.get("account_id"))))
                        if candidates:
                            new_active = str(candidates[0]["account_id"])
                            if new_active != active:
                                await self.database.record_failover(str(group["id"]), active, new_active, str(exc), failures)
                                await self._notify_admins(str(group["id"]), active, new_active, str(exc))
                        else:
                            await self.database.update_account_group(str(group["id"]), health_failures=failures, last_health_error=str(exc), status="degraded")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEALTH_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue

    async def _notify_admins(self, group_id: str, previous: str, active: str, reason: str) -> None:
        try:
            auth = AuthStore(self.database.path)
            for user in await auth.list_users():
                if str(user.get("role")) in {"admin", "superadmin"} and user.get("id"):
                    await self.database.create_notification(int(user["id"]), "failover", "Telegram 容灾账号已接管", f"账号组 {group_id} 已从 {previous} 切换到 {active}。原因：{reason}", "/admin")
        except Exception:
            pass
