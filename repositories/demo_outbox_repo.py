"""Demo implementation of OutboxRepository for tokens, outbox leases, audit, and worker health."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from core.errors import NotFoundError
from core.models import ConsentCategory, OutboxStatus
from core.models_clinical import (
    ActionTokenRecord,
    AuditEvent,
    NotificationOutboxItem,
    WorkerHealth,
)

from repositories.demo_store import DemoStore


class DemoOutboxRepository:
    """Thread-safe demo OutboxRepository enforcing lease, deduplication, and append-only audit."""

    def __init__(self, store: DemoStore) -> None:
        self._s = store

    def insert_action_token(self, token: ActionTokenRecord) -> None:
        with self._s.lock:
            self._s.action_tokens[token.token_hash] = token

    def get_action_token_by_hash(self, token_hash: str) -> ActionTokenRecord | None:
        with self._s.lock:
            return self._s.action_tokens.get(token_hash)

    def consume_action_token(self, *, token_hash: str, now: datetime) -> bool:
        with self._s.lock:
            tok = self._s.action_tokens.get(token_hash)
            if (
                not tok
                or tok.consumed_at is not None
                or tok.invalidated_at is not None
                or tok.expires_at <= now
            ):
                return False
            self._s.action_tokens[token_hash] = tok.model_copy(update={"consumed_at": now})
            return True

    def invalidate_action_tokens_for_resource(self, *, resource_id: UUID, now: datetime) -> int:
        with self._s.lock:
            cnt = 0
            for h, tok in list(self._s.action_tokens.items()):
                if (
                    tok.resource_id == resource_id
                    and tok.consumed_at is None
                    and tok.invalidated_at is None
                ):
                    self._s.action_tokens[h] = tok.model_copy(update={"invalidated_at": now})
                    cnt += 1
            return cnt

    def enqueue_outbox(self, item: NotificationOutboxItem) -> tuple[NotificationOutboxItem, bool]:
        with self._s.lock:
            existing_id = self._s.outbox_logical_index.get(item.logical_key)
            if existing_id is not None:
                return self._s.outbox[existing_id], False
            self._s.outbox[item.id] = item
            self._s.outbox_logical_index[item.logical_key] = item.id
            return item, True

    def get_outbox_by_logical_key(self, logical_key: str) -> NotificationOutboxItem | None:
        with self._s.lock:
            oid = self._s.outbox_logical_index.get(logical_key)
            return self._s.outbox.get(oid) if oid else None

    def reactivate_outbox_item(
        self, *, logical_key: str, payload: dict[str, Any], now: datetime
    ) -> NotificationOutboxItem | None:
        with self._s.lock:
            oid = self._s.outbox_logical_index.get(logical_key)
            if not oid:
                return None
            item = self._s.outbox[oid]
            if item.status != OutboxStatus.CANCELLED:
                return item
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.PENDING,
                    "payload": payload,
                    "next_attempt_at": now,
                    "updated_at": now,
                }
            )
            self._s.outbox[oid] = updated
            return updated

    def list_outbox(self, circle_id: UUID, limit: int = 50) -> list[NotificationOutboxItem]:
        with self._s.lock:
            items = [o for o in self._s.outbox.values() if o.circle_id == circle_id]
            return sorted(items, key=lambda o: o.created_at, reverse=True)[:limit]

    def claim_outbox_batch(
        self, *, worker_id: str, now: datetime, lease_seconds: int, batch_size: int
    ) -> list[NotificationOutboxItem]:
        with self._s.lock:
            for oid, item in list(self._s.outbox.items()):
                if (
                    item.status == OutboxStatus.SENDING
                    and item.lease_expires_at is not None
                    and item.lease_expires_at <= now
                ):
                    self._s.outbox[oid] = item.model_copy(
                        update={
                            "status": OutboxStatus.UNCERTAIN,
                            "last_error_code": "lease_expired_during_send",
                            "updated_at": now,
                        }
                    )
                elif (
                    item.status == OutboxStatus.LEASED
                    and item.lease_expires_at is not None
                    and item.lease_expires_at <= now
                ):
                    self._s.outbox[oid] = item.model_copy(
                        update={
                            "status": OutboxStatus.RETRYABLE,
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "updated_at": now,
                        }
                    )

            eligible = [
                o
                for o in self._s.outbox.values()
                if o.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                and o.next_attempt_at <= now
            ]
            eligible.sort(key=lambda o: o.next_attempt_at)
            claimed: list[NotificationOutboxItem] = []
            for o in eligible[:batch_size]:
                upd = o.model_copy(
                    update={
                        "status": OutboxStatus.LEASED,
                        "lease_owner": worker_id,
                        "lease_expires_at": now + timedelta(seconds=lease_seconds),
                        "updated_at": now,
                    }
                )
                self._s.outbox[o.id] = upd
                claimed.append(upd)
            return claimed

    def mark_outbox_sending(self, *, item_id: UUID, lease_owner: str, now: datetime) -> bool:
        with self._s.lock:
            item = self._s.outbox.get(item_id)
            if not item or item.status != OutboxStatus.LEASED or item.lease_owner != lease_owner:
                return False
            self._s.outbox[item_id] = item.model_copy(
                update={"status": OutboxStatus.SENDING, "updated_at": now}
            )
            return True

    def update_outbox_outcome(
        self,
        *,
        item_id: UUID,
        status: OutboxStatus,
        now: datetime,
        attempts: int | None = None,
        next_attempt_at: datetime | None = None,
        error_code: str = "",
        provider_message_id: str = "",
    ) -> NotificationOutboxItem:
        with self._s.lock:
            item = self._s.outbox.get(item_id)
            if not item:
                raise NotFoundError("errors.not_found")
            updated = item.model_copy(
                update={
                    "status": status,
                    "attempts": attempts if attempts is not None else item.attempts,
                    "next_attempt_at": next_attempt_at or item.next_attempt_at,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": error_code,
                    "provider_message_id": provider_message_id or item.provider_message_id,
                    "updated_at": now,
                }
            )
            self._s.outbox[item_id] = updated
            return updated

    def cancel_outbox_for_resource(
        self,
        *,
        resource_id: UUID,
        event_prefix: str | None = None,
        logical_key: str | None = None,
        now: datetime,
    ) -> int:
        with self._s.lock:
            cnt = 0
            for oid, item in list(self._s.outbox.items()):
                if item.status not in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE):
                    continue
                if logical_key is not None and item.logical_key != logical_key:
                    continue
                if item.resource_id != resource_id:
                    continue
                if event_prefix is not None and not item.event_type.startswith(event_prefix):
                    continue
                self._s.outbox[oid] = item.model_copy(
                    update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                )
                cnt += 1
            return cnt

    def cancel_outbox_for_recipient(
        self,
        *,
        circle_id: UUID,
        recipient_membership_id: UUID,
        category: ConsentCategory | None,
        now: datetime,
    ) -> int:
        with self._s.lock:
            cnt = 0
            for oid, item in list(self._s.outbox.items()):
                if (
                    item.circle_id == circle_id
                    and item.recipient_membership_id == recipient_membership_id
                    and item.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                    and (category is None or item.required_category == category)
                ):
                    self._s.outbox[oid] = item.model_copy(
                        update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                    )
                    cnt += 1
            return cnt

    def count_outbox_metrics(self, now: datetime) -> dict[str, int]:
        with self._s.lock:
            due = [
                o
                for o in self._s.outbox.values()
                if o.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                and o.next_attempt_at <= now
            ]
            uncertain = sum(
                1 for o in self._s.outbox.values() if o.status == OutboxStatus.UNCERTAIN
            )
            max_lag = max((int((now - o.next_attempt_at).total_seconds()) for o in due), default=0)
            return {
                "due_count": len(due),
                "due_lag_seconds": max(0, max_lag),
                "uncertain_count": uncertain,
            }

    def append_audit(self, event: AuditEvent) -> None:
        with self._s.lock:
            self._s.audit_events.append(event)

    def list_audit(self, circle_id: UUID, limit: int = 100) -> list[AuditEvent]:
        with self._s.lock:
            items = [e for e in self._s.audit_events if e.circle_id == circle_id]
            return sorted(items, key=lambda e: e.occurred_at, reverse=True)[:limit]

    def get_worker_health(self, worker_name: str = "reminders") -> WorkerHealth:
        with self._s.lock:
            return self._s.worker_health.get(worker_name, WorkerHealth(worker_name=worker_name))

    def save_worker_health(self, health: WorkerHealth) -> None:
        with self._s.lock:
            self._s.worker_health[health.worker_name] = health
