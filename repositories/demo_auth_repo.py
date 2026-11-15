"""Demo implementations of AuthRepository and CircleRepository."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from core.errors import ConflictError, NotFoundError
from core.models import (
    Account,
    AccountStatus,
    ActorContext,
    CareCircle,
    ConsentCategory,
    FamilyGrant,
    InvitationRecord,
    MemberConsent,
    Membership,
    PendingVerification,
    Role,
    SessionRecord,
)
from core.models_clinical import AuditEvent, OutboxStatus

from repositories.demo_store import DemoStore


def _build_actor(
    store: DemoStore,
    sess: SessionRecord,
    acc: Account,
    mem: Membership,
    circle: CareCircle,
) -> ActorContext:
    if mem.role == Role.ELDER:
        consents = frozenset(ConsentCategory)
    else:
        consents = frozenset(
            cat
            for cat in ConsentCategory
            if (
                (circle.id, mem.id, cat) in store.consents
                and store.consents[(circle.id, mem.id, cat)].granted
            )
        )
    return ActorContext(
        session_id=sess.id,
        account_id=acc.id,
        membership_id=mem.id,
        circle_id=circle.id,
        role=mem.role,
        display_name=acc.display_name,
        language=acc.language,
        font_px=acc.font_px,
        high_contrast=acc.high_contrast,
        is_care_contact=mem.is_care_contact,
        grants=mem.grants,
        consents=consents,
        auth_revision=mem.auth_revision,
        circle_timezone=circle.timezone,
        ai_consent_granted=circle.ai_consent_granted,
    )


class DemoAuthRepository:
    """Thread-safe demo AuthRepository enforcing lockout, single-use links, and sessions."""

    def __init__(self, store: DemoStore) -> None:
        self._s = store

    def get_account_by_email(self, email_normalized: str) -> Account | None:
        with self._s.lock:
            acc_id = self._s.email_index.get(email_normalized)
            return self._s.accounts.get(acc_id) if acc_id else None

    def get_account_by_id(self, account_id: UUID) -> Account | None:
        with self._s.lock:
            return self._s.accounts.get(account_id)

    def list_memberships_for_account(self, account_id: UUID) -> list[tuple[Membership, CareCircle]]:
        with self._s.lock:
            out: list[tuple[Membership, CareCircle]] = []
            for mem in self._s.memberships.values():
                if mem.account_id == account_id and mem.active:
                    circle = self._s.circles.get(mem.circle_id)
                    if circle and circle.id not in self._s.deleted_circle_ids:
                        out.append((mem, circle))
            return out

    def record_login_attempt(
        self,
        *,
        email_normalized: str,
        success: bool,
        now: datetime,
        max_failures: int,
        lockout_minutes: int,
        global_limit_per_min: int,
    ) -> dict[str, Any]:
        with self._s.lock:
            bucket = now.replace(second=0, microsecond=0)
            cnt = self._s.login_buckets.get(bucket, 0) + 1
            self._s.login_buckets[bucket] = cnt
            if cnt > global_limit_per_min:
                return {"status": "rate_limited"}

            acc_id = self._s.email_index.get(email_normalized)
            if not acc_id:
                return {"status": "not_found"}
            acc = self._s.accounts[acc_id]
            if acc.locked_until is not None and acc.locked_until > now:
                return {"status": "locked", "locked_until": acc.locked_until}
            if success:
                self._s.accounts[acc_id] = acc.model_copy(
                    update={"failed_login_attempts": 0, "locked_until": None}
                )
                return {"status": "ok", "account_id": acc_id}

            failures = acc.failed_login_attempts + 1
            if failures >= max_failures:
                locked_until = now + timedelta(minutes=lockout_minutes)
                self._s.accounts[acc_id] = acc.model_copy(
                    update={"failed_login_attempts": failures, "locked_until": locked_until}
                )
                return {"status": "locked", "locked_until": locked_until}
            self._s.accounts[acc_id] = acc.model_copy(update={"failed_login_attempts": failures})
            return {"status": "failed", "attempts": failures}

    def create_session(
        self,
        *,
        token_hash: str,
        account_id: UUID,
        membership_id: UUID,
        now: datetime,
        idle_minutes: int,
        absolute_hours: int,
    ) -> ActorContext:
        with self._s.lock:
            acc = self._s.accounts.get(account_id)
            mem = self._s.memberships.get(membership_id)
            if not acc or acc.status != AccountStatus.ACTIVE or not mem or not mem.active:
                raise NotFoundError("errors.not_found")
            if mem.account_id != account_id:
                raise NotFoundError("errors.not_found")
            circle = self._s.circles.get(mem.circle_id)
            if not circle or circle.id in self._s.deleted_circle_ids:
                raise NotFoundError("errors.not_found")

            abs_exp = now + timedelta(hours=absolute_hours)
            idle_exp = min(abs_exp, now + timedelta(minutes=idle_minutes))
            sess = SessionRecord(
                id=uuid.uuid4(),
                token_hash=token_hash,
                account_id=account_id,
                membership_id=membership_id,
                circle_id=circle.id,
                auth_revision=mem.auth_revision,
                created_at=now,
                last_seen_at=now,
                idle_expires_at=idle_exp,
                absolute_expires_at=abs_exp,
            )
            self._s.sessions[token_hash] = sess
            return _build_actor(self._s, sess, acc, mem, circle)

    def resolve_session(
        self, *, token_hash: str, now: datetime, idle_minutes: int
    ) -> ActorContext | None:
        with self._s.lock:
            sess = self._s.sessions.get(token_hash)
            if not sess or sess.revoked_at is not None:
                return None
            if sess.idle_expires_at <= now or sess.absolute_expires_at <= now:
                return None
            acc = self._s.accounts.get(sess.account_id)
            mem = self._s.memberships.get(sess.membership_id)
            circle = self._s.circles.get(sess.circle_id)
            if (
                not acc
                or acc.status != AccountStatus.ACTIVE
                or not mem
                or not mem.active
                or mem.circle_id != sess.circle_id
                or mem.auth_revision != sess.auth_revision
                or not circle
                or circle.id in self._s.deleted_circle_ids
            ):
                return None

            new_idle = min(sess.absolute_expires_at, now + timedelta(minutes=idle_minutes))
            updated = sess.model_copy(update={"last_seen_at": now, "idle_expires_at": new_idle})
            self._s.sessions[token_hash] = updated
            return _build_actor(self._s, updated, acc, mem, circle)

    def revoke_session(self, *, token_hash: str, now: datetime) -> None:
        with self._s.lock:
            sess = self._s.sessions.get(token_hash)
            if sess and sess.revoked_at is None:
                self._s.sessions[token_hash] = sess.model_copy(update={"revoked_at": now})

    def revoke_sessions_for_account(
        self, *, account_id: UUID, now: datetime, except_token_hash: str | None = None
    ) -> int:
        with self._s.lock:
            count = 0
            for h, sess in list(self._s.sessions.items()):
                if (
                    sess.account_id == account_id
                    and sess.revoked_at is None
                    and h != except_token_hash
                ):
                    self._s.sessions[h] = sess.model_copy(update={"revoked_at": now})
                    count += 1
            return count

    def update_preferences(
        self,
        *,
        account_id: UUID,
        language: str,
        font_px: int,
        high_contrast: bool,
    ) -> Account:
        with self._s.lock:
            acc = self._s.accounts.get(account_id)
            if not acc:
                raise NotFoundError("errors.not_found")
            updated = acc.model_copy(
                update={"language": language, "font_px": font_px, "high_contrast": high_contrast}
            )
            self._s.accounts[account_id] = updated
            return updated

    def update_telegram_binding(
        self, *, account_id: UUID, opt_in: bool, chat_id: str | None
    ) -> Account:
        with self._s.lock:
            acc = self._s.accounts.get(account_id)
            if not acc:
                raise NotFoundError("errors.not_found")
            updated = acc.model_copy(
                update={"telegram_opt_in": opt_in, "telegram_chat_id": chat_id}
            )
            self._s.accounts[account_id] = updated
            return updated

    def update_passcode_and_revoke(
        self,
        *,
        account_id: UUID,
        new_passcode_hash: str,
        now: datetime,
        keep_session_hash: str | None = None,
    ) -> None:
        with self._s.lock:
            acc = self._s.accounts.get(account_id)
            if not acc:
                raise NotFoundError("errors.not_found")
            self._s.accounts[account_id] = acc.model_copy(
                update={"passcode_hash": new_passcode_hash}
            )
            self.revoke_sessions_for_account(
                account_id=account_id, now=now, except_token_hash=keep_session_hash
            )

    def create_pending_verification(
        self,
        *,
        token_hash: str,
        email_normalized: str,
        display_name: str,
        passcode_hash: str,
        timezone: str,
        language: str,
        expires_at: datetime,
        now: datetime,
    ) -> PendingVerification:
        with self._s.lock:
            pv = PendingVerification(
                id=uuid.uuid4(),
                token_hash=token_hash,
                email_normalized=email_normalized,
                display_name=display_name,
                passcode_hash=passcode_hash,
                timezone=timezone,
                language=language,
                expires_at=expires_at,
                created_at=now,
            )
            self._s.pending_verifications[token_hash] = pv
            return pv

    def get_pending_verification_by_hash(self, token_hash: str) -> PendingVerification | None:
        with self._s.lock:
            return self._s.pending_verifications.get(token_hash)

    def redeem_verification(self, *, token_hash: str, now: datetime) -> dict[str, Any]:
        with self._s.lock:
            pv = self._s.pending_verifications.get(token_hash)
            if not pv or pv.consumed_at is not None or pv.expires_at <= now:
                return {"status": "invalid"}
            if pv.email_normalized in self._s.email_index:
                return {"status": "conflict"}

            self._s.pending_verifications[token_hash] = pv.model_copy(update={"consumed_at": now})
            acc_id = uuid.uuid4()
            acc = Account(
                id=acc_id,
                email_normalized=pv.email_normalized,
                display_name=pv.display_name,
                passcode_hash=pv.passcode_hash,
                status=AccountStatus.ACTIVE,
                language=pv.language,
                font_px=24,
                created_at=now,
            )
            self._s.accounts[acc_id] = acc
            self._s.email_index[pv.email_normalized] = acc_id

            circle_id = uuid.uuid4()
            circle = CareCircle(
                id=circle_id,
                elder_account_id=acc_id,
                display_name=pv.display_name,
                timezone=pv.timezone,
                checkin_enabled_from=now,
                created_at=now,
            )
            self._s.circles[circle_id] = circle

            mem_id = uuid.uuid4()
            mem = Membership(
                id=mem_id,
                circle_id=circle_id,
                account_id=acc_id,
                role=Role.ELDER,
                active=True,
                created_at=now,
            )
            self._s.memberships[mem_id] = mem
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=acc_id,
                    actor_membership_id=mem_id,
                    operation="elder_onboarding_verified",
                    target_type="circle",
                    target_id=str(circle_id),
                    occurred_at=now,
                )
            )
            return {
                "status": "ok",
                "account_id": acc_id,
                "circle_id": circle_id,
                "membership_id": mem_id,
            }

    def create_own_circle(
        self,
        *,
        account_id: UUID,
        display_name: str,
        timezone: str,
        now: datetime,
    ) -> tuple[CareCircle, Membership]:
        with self._s.lock:
            for c in self._s.circles.values():
                if c.elder_account_id == account_id and c.id not in self._s.deleted_circle_ids:
                    raise ConflictError("errors.conflict")
            circle_id = uuid.uuid4()
            circle = CareCircle(
                id=circle_id,
                elder_account_id=account_id,
                display_name=display_name,
                timezone=timezone,
                checkin_enabled_from=now,
                created_at=now,
            )
            self._s.circles[circle_id] = circle
            mem = Membership(
                id=uuid.uuid4(),
                circle_id=circle_id,
                account_id=account_id,
                role=Role.ELDER,
                active=True,
                created_at=now,
            )
            self._s.memberships[mem.id] = mem
            return circle, mem

    def create_invitation(
        self,
        *,
        circle_id: UUID,
        token_hash: str,
        invited_email_normalized: str,
        role: Role,
        grants: frozenset[FamilyGrant],
        initial_consents: frozenset[ConsentCategory],
        is_care_contact: bool,
        invited_by_account_id: UUID,
        expires_at: datetime,
        now: datetime,
    ) -> InvitationRecord:
        with self._s.lock:
            inv = InvitationRecord(
                id=uuid.uuid4(),
                circle_id=circle_id,
                token_hash=token_hash,
                invited_email_normalized=invited_email_normalized,
                role=role,
                grants=grants,
                initial_consents=initial_consents,
                is_care_contact=is_care_contact,
                invited_by_account_id=invited_by_account_id,
                expires_at=expires_at,
                created_at=now,
            )
            self._s.invitations[token_hash] = inv
            return inv

    def get_invitation_by_hash(self, token_hash: str) -> tuple[InvitationRecord, CareCircle] | None:
        with self._s.lock:
            inv = self._s.invitations.get(token_hash)
            if not inv:
                return None
            circle = self._s.circles.get(inv.circle_id)
            if not circle or circle.id in self._s.deleted_circle_ids:
                return None
            return inv, circle

    def redeem_invitation(
        self,
        *,
        token_hash: str,
        now: datetime,
        passcode_hash: str | None,
        display_name: str | None,
        existing_account_id: UUID | None,
    ) -> dict[str, Any]:
        with self._s.lock:
            inv = self._s.invitations.get(token_hash)
            if not inv or inv.consumed_at or inv.revoked_at or inv.expires_at <= now:
                return {"status": "invalid"}
            if inv.circle_id in self._s.deleted_circle_ids:
                return {"status": "invalid"}

            acc_id = self._s.email_index.get(inv.invited_email_normalized)
            if acc_id is not None:
                if existing_account_id != acc_id:
                    return {"status": "auth_required"}
            else:
                if not passcode_hash or not display_name:
                    return {"status": "passcode_required"}
                acc_id = uuid.uuid4()
                self._s.accounts[acc_id] = Account(
                    id=acc_id,
                    email_normalized=inv.invited_email_normalized,
                    display_name=display_name,
                    passcode_hash=passcode_hash,
                    status=AccountStatus.ACTIVE,
                    font_px=18,
                    created_at=now,
                )
                self._s.email_index[inv.invited_email_normalized] = acc_id

            for existing_mem in self._s.memberships.values():
                if existing_mem.circle_id == inv.circle_id and existing_mem.account_id == acc_id:
                    return {"status": "conflict"}

            self._s.invitations[token_hash] = inv.model_copy(update={"consumed_at": now})
            mem_id = uuid.uuid4()
            mem = Membership(
                id=mem_id,
                circle_id=inv.circle_id,
                account_id=acc_id,
                role=inv.role,
                active=True,
                is_care_contact=inv.is_care_contact,
                grants=inv.grants,
                created_at=now,
            )
            self._s.memberships[mem_id] = mem
            for cat in ConsentCategory:
                self._s.consents[(inv.circle_id, mem_id, cat)] = MemberConsent(
                    circle_id=inv.circle_id,
                    membership_id=mem_id,
                    category=cat,
                    granted=cat in inv.initial_consents,
                    updated_at=now,
                    updated_by_account_id=inv.invited_by_account_id,
                )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=inv.circle_id,
                    actor_account_id=acc_id,
                    actor_membership_id=mem_id,
                    operation="invitation_accepted",
                    target_type="membership",
                    target_id=str(mem_id),
                    metadata={"role": inv.role.value},
                    occurred_at=now,
                )
            )
            return {"status": "ok", "account_id": acc_id, "membership_id": mem_id}


class DemoCircleRepository:
    """Thread-safe demo CircleRepository enforcing cross-circle and consent boundaries."""

    def __init__(self, store: DemoStore) -> None:
        self._s = store

    def get_circle(self, circle_id: UUID) -> CareCircle | None:
        with self._s.lock:
            if circle_id in self._s.deleted_circle_ids:
                return None
            return self._s.circles.get(circle_id)

    def list_active_circles(self) -> list[CareCircle]:
        with self._s.lock:
            return [
                c for cid, c in self._s.circles.items() if cid not in self._s.deleted_circle_ids
            ]

    def list_members(
        self, circle_id: UUID
    ) -> list[tuple[Membership, Account, list[MemberConsent]]]:
        with self._s.lock:
            out: list[tuple[Membership, Account, list[MemberConsent]]] = []
            for mem in self._s.memberships.values():
                if mem.circle_id == circle_id:
                    acc = self._s.accounts[mem.account_id]
                    consents = [
                        self._s.consents[(circle_id, mem.id, cat)]
                        for cat in ConsentCategory
                        if (circle_id, mem.id, cat) in self._s.consents
                    ]
                    out.append((mem, acc, consents))
            return out

    def get_membership(self, circle_id: UUID, membership_id: UUID) -> Membership | None:
        with self._s.lock:
            mem = self._s.memberships.get(membership_id)
            if not mem or mem.circle_id != circle_id:
                return None
            return mem

    def get_member_consents(
        self, circle_id: UUID, membership_id: UUID
    ) -> frozenset[ConsentCategory]:
        with self._s.lock:
            mem = self._s.memberships.get(membership_id)
            if not mem or mem.circle_id != circle_id or not mem.active:
                return frozenset()
            if mem.role == Role.ELDER:
                return frozenset(ConsentCategory)
            return frozenset(
                cat
                for cat in ConsentCategory
                if (circle_id, membership_id, cat) in self._s.consents
                and self._s.consents[(circle_id, membership_id, cat)].granted
            )

    def update_member_consent(
        self,
        *,
        circle_id: UUID,
        membership_id: UUID,
        category: ConsentCategory,
        granted: bool,
        actor_account_id: UUID,
        now: datetime,
    ) -> None:
        with self._s.lock:
            mem = self.get_membership(circle_id, membership_id)
            if not mem or mem.role == Role.ELDER:
                raise NotFoundError("errors.not_found")
            self._s.consents[(circle_id, membership_id, category)] = MemberConsent(
                circle_id=circle_id,
                membership_id=membership_id,
                category=category,
                granted=granted,
                updated_at=now,
                updated_by_account_id=actor_account_id,
            )
            self._s.memberships[membership_id] = mem.model_copy(
                update={"auth_revision": mem.auth_revision + 1}
            )
            if not granted:
                for oid, item in list(self._s.outbox.items()):
                    if (
                        item.circle_id == circle_id
                        and item.recipient_membership_id == membership_id
                        and item.required_category == category
                        and item.status in (OutboxStatus.PENDING, OutboxStatus.RETRYABLE)
                    ):
                        self._s.outbox[oid] = item.model_copy(
                            update={"status": OutboxStatus.CANCELLED, "updated_at": now}
                        )
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="consent_updated",
                    target_type="membership",
                    target_id=str(membership_id),
                    metadata={"category": category.value, "granted": granted},
                    occurred_at=now,
                )
            )

    def update_member_grants(
        self,
        *,
        circle_id: UUID,
        membership_id: UUID,
        grants: frozenset[FamilyGrant],
        is_care_contact: bool,
        actor_account_id: UUID,
        now: datetime,
    ) -> Membership:
        with self._s.lock:
            mem = self.get_membership(circle_id, membership_id)
            if not mem or mem.role == Role.ELDER:
                raise NotFoundError("errors.not_found")
            updated = mem.model_copy(
                update={
                    "grants": grants,
                    "is_care_contact": is_care_contact,
                    "auth_revision": mem.auth_revision + 1,
                }
            )
            self._s.memberships[membership_id] = updated
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="member_grants_updated",
                    target_type="membership",
                    target_id=str(membership_id),
                    metadata={
                        "grants": sorted(g.value for g in grants),
                        "is_care_contact": is_care_contact,
                    },
                    occurred_at=now,
                )
            )
            return updated

    def deactivate_member(
        self,
        *,
        circle_id: UUID,
        membership_id: UUID,
        actor_account_id: UUID,
        now: datetime,
    ) -> None:
        with self._s.lock:
            mem = self.get_membership(circle_id, membership_id)
            if not mem or mem.role == Role.ELDER:
                raise NotFoundError("errors.not_found")
            self._s.memberships[membership_id] = mem.model_copy(
                update={"active": False, "auth_revision": mem.auth_revision + 1}
            )
            for h, sess in list(self._s.sessions.items()):
                if sess.membership_id == membership_id and sess.revoked_at is None:
                    self._s.sessions[h] = sess.model_copy(update={"revoked_at": now})
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="member_removed",
                    target_type="membership",
                    target_id=str(membership_id),
                    occurred_at=now,
                )
            )

    def update_circle_policies(
        self, *, circle_id: UUID, updates: dict[str, Any], actor_account_id: UUID, now: datetime
    ) -> CareCircle:
        with self._s.lock:
            circle = self.get_circle(circle_id)
            if not circle:
                raise NotFoundError("errors.not_found")
            for contact_field in (
                "primary_contact_membership_id",
                "secondary_contact_membership_id",
                "checkin_fallback_membership_id",
            ):
                if contact_field in updates and updates[contact_field] is not None:
                    mem = self.get_membership(circle_id, updates[contact_field])
                    if not mem or not mem.active:
                        raise NotFoundError("errors.cross_circle_denied")
            updated = circle.model_copy(update=updates)
            self._s.circles[circle_id] = updated
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="circle_policy_updated",
                    target_type="circle",
                    target_id=str(circle_id),
                    metadata={"fields": sorted(updates.keys())},
                    occurred_at=now,
                )
            )
            return updated

    def delete_circle_purge(
        self, *, circle_id: UUID, actor_account_id: UUID, now: datetime
    ) -> None:
        with self._s.lock:
            if circle_id not in self._s.circles:
                raise NotFoundError("errors.not_found")
            self._s.deleted_circle_ids.add(circle_id)
            for h, sess in list(self._s.sessions.items()):
                if sess.circle_id == circle_id and sess.revoked_at is None:
                    self._s.sessions[h] = sess.model_copy(update={"revoked_at": now})
            self._s.audit_events.append(
                AuditEvent(
                    id=uuid.uuid4(),
                    circle_id=circle_id,
                    actor_account_id=actor_account_id,
                    operation="circle_deleted_purged",
                    target_type="circle",
                    target_id=str(circle_id),
                    occurred_at=now,
                )
            )
