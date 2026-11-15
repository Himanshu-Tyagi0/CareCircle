"""Doctor Visit Brief generation, family notes, elder approval, and consent enforcement."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from core.clock import Clock
from core.errors import (
    AuthorizationError,
    ConsentRequiredError,
    NotFoundError,
    ValidationError,
)
from core.models import ActorContext, CheckInMood, ConsentCategory, DoseState, Role
from core.models_clinical import AuditEvent, CareNote, DoctorBriefSnapshot
from core.permissions import Action, authorize
from repositories.interfaces import (
    CircleRepository,
    CollabRepository,
    DoseRepository,
    MedicationRepository,
    OutboxRepository,
)

from services.adherence_service import compute_adherence
from services.ai_service import AIService


def render_narrative_from_facts(
    factual_statements: list[dict[str, str]],
    selected_ids: list[str],
    template_id: str,
    *,
    lang: str,
) -> str:
    """Assemble short narrative strictly in code from validated factual statement IDs."""
    by_id = {f["id"]: f for f in factual_statements}
    chosen = [
        by_id[fid][lang if lang in ("en", "hi") else "en"] for fid in selected_ids if fid in by_id
    ]
    if not chosen:
        return ""
    prefix = (
        "Summary of recorded care highlights: "
        if lang == "en"
        else "दर्ज देखभाल रिकॉर्ड का संक्षिप्त विवरण: "
    )
    if template_id == "adherence_focus_v1":
        prefix = (
            "Confirmation and wellbeing overview: "
            if lang == "en"
            else "नियमितता और हाल-चाल का संक्षिप्त विवरण: "
        )
    return prefix + " ".join(chosen)


class BriefService:
    """Notes, elder questions, deterministic doctor briefs, and version-bound elder approvals."""

    def __init__(
        self,
        *,
        circle_repo: CircleRepository,
        med_repo: MedicationRepository,
        dose_repo: DoseRepository,
        collab_repo: CollabRepository,
        outbox_repo: OutboxRepository,
        ai_service: AIService | None,
        clock: Clock,
        grace_minutes: int = 30,
    ) -> None:
        self._circle = circle_repo
        self._med = med_repo
        self._dose = dose_repo
        self._collab = collab_repo
        self._outbox = outbox_repo
        self._ai = ai_service
        self._clock = clock
        self._grace = grace_minutes

    def list_notes(self, actor: ActorContext) -> list[CareNote]:
        """List circle notes and elder questions if doctor-brief consent is held."""
        authorize(actor, Action.VIEW_DOCTOR_BRIEF, target_circle_id=actor.circle_id)
        return self._collab.list_notes(actor.circle_id)

    def add_note(self, actor: ActorContext, *, note_type: str, content: str) -> CareNote:
        """Add a bounded family note or elder question under strict permission rules."""
        if note_type == "elder_question":
            authorize(actor, Action.MANAGE_ELDER_QUESTIONS, target_circle_id=actor.circle_id)
        elif note_type == "family_note":
            authorize(actor, Action.ADD_OWN_NOTE, target_circle_id=actor.circle_id)
        else:
            raise ValidationError("errors.validation")
        cleaned = content.strip()
        if not cleaned or len(cleaned) > 500:
            raise ValidationError("errors.validation")
        return self._collab.create_note(
            circle_id=actor.circle_id,
            author_account_id=actor.account_id,
            author_name=actor.display_name,
            note_type=note_type,
            content=cleaned,
            now=self._clock.now(),
        )

    def edit_note(self, actor: ActorContext, *, note_id: UUID, content: str) -> CareNote:
        """Edit an existing note (elder can edit any; non-elder only own note with grant)."""
        note = self._collab.get_note(actor.circle_id, note_id)
        if not note:
            raise NotFoundError("errors.not_found")
        if actor.role != Role.ELDER:
            authorize(
                actor,
                Action.EDIT_OWN_NOTE,
                target_circle_id=actor.circle_id,
                author_account_id=note.author_account_id,
            )
        cleaned = content.strip()
        if not cleaned or len(cleaned) > 500:
            raise ValidationError("errors.validation")
        return self._collab.update_note(
            circle_id=actor.circle_id, note_id=note_id, content=cleaned, now=self._clock.now()
        )

    def delete_note(self, actor: ActorContext, *, note_id: UUID) -> None:
        """Delete a note (elder can delete any; non-elder only own note with grant)."""
        note = self._collab.get_note(actor.circle_id, note_id)
        if not note:
            raise NotFoundError("errors.not_found")
        if actor.role != Role.ELDER:
            authorize(
                actor,
                Action.DELETE_OWN_NOTE,
                target_circle_id=actor.circle_id,
                author_account_id=note.author_account_id,
            )
        self._collab.delete_note(circle_id=actor.circle_id, note_id=note_id)

    def generate_brief(
        self,
        actor: ActorContext,
        *,
        period_days: int = 30,
        selected_note_ids: list[UUID] | None = None,
        use_ai_narrative: bool = False,
    ) -> DoctorBriefSnapshot:
        """Generate a deterministic visit brief draft; requires access to all included source categories."""
        source_cats = (
            ConsentCategory.DOCTOR_BRIEF,
            ConsentCategory.MEDICATIONS,
            ConsentCategory.ADHERENCE,
            ConsentCategory.CHECKINS,
        )
        authorize(
            actor,
            Action.PREPARE_DOCTOR_BRIEF,
            target_circle_id=actor.circle_id,
            required_categories=source_cats,
        )
        if not (1 <= period_days <= 90):
            raise ValidationError("errors.validation")

        now = self._clock.now()
        tz = ZoneInfo(actor.circle_timezone)
        end_local = now.astimezone(tz).date()
        start_local = end_local - timedelta(days=period_days - 1)

        meds = self._med.list_medications(actor.circle_id)
        doses = self._dose.list_doses_in_window(
            actor.circle_id, now - timedelta(days=period_days + 1), now
        )
        adherence = compute_adherence(
            doses,
            window_days=period_days,
            tz_name=actor.circle_timezone,
            grace_minutes=self._grace,
            now=now,
        )
        checkins = self._collab.list_checkins(actor.circle_id, start_local, end_local)
        mood_counts = Counter(c.mood.value for c in checkins if c.mood is not None)

        all_notes = self._collab.list_notes(actor.circle_id)
        selected_set = (
            set(selected_note_ids) if selected_note_ids is not None else {n.id for n in all_notes}
        )
        included_notes = [
            {"id": str(n.id), "type": n.note_type, "author": n.author_name, "content": n.content}
            for n in all_notes
            if n.id in selected_set or n.note_type == "elder_question"
        ]

        reasons: list[dict[str, str]] = []
        for d in doses:
            if d.due_at <= now and d.state in (
                DoseState.CANT_TAKE,
                DoseState.SKIPPED,
                DoseState.EXPIRED,
            ):
                reasons.append(
                    {
                        "med": d.medication_name,
                        "date": d.local_date.isoformat(),
                        "state": d.state.value,
                        "reason": (
                            (
                                getattr(d.cant_take_reason, "value", d.cant_take_reason)
                                if d.cant_take_reason
                                else d.skip_reason
                            )
                            or ""
                        ),
                    }
                )

        sections = {
            "medications": [
                {"name": m.name, "dose": m.dose_text, "instructions": m.instructions} for m in meds
            ],
            "adherence": {
                "total_past_due": adherence.total_past_due,
                "taken": adherence.taken,
                "rate_percent": adherence.rate_percent,
                "cant_take": adherence.cant_take,
                "skipped": adherence.skipped,
                "unconfirmed": adherence.unconfirmed,
                "by_medication": [
                    {
                        "name": m.medication_name,
                        "taken": m.taken,
                        "total": m.total_past_due,
                        "rate_percent": m.rate_percent,
                    }
                    for m in adherence.by_medication
                ],
            },
            "patterns": reasons[:25],
            "checkins": {
                "responses": sum(mood_counts.values()),
                "good": mood_counts.get(CheckInMood.GOOD.value, 0),
                "okay": mood_counts.get(CheckInMood.OKAY.value, 0),
                "not_well": mood_counts.get(CheckInMood.NOT_WELL.value, 0),
            },
            "notes_and_questions": included_notes,
            "generated_at_iso": now.isoformat(),
        }

        facts: list[dict[str, str]] = [
            {
                "id": "F1_ADHERENCE",
                "en": f"Over the past {period_days} days, {adherence.taken} of {adherence.total_past_due} past-due doses were confirmed taken.",
                "hi": f"पिछले {period_days} दिनों में, {adherence.total_past_due} में से {adherence.taken} तय खुराकों की पुष्टि हुई।",
            },
            {
                "id": "F2_PATTERNS",
                "en": f"Recorded non-taken events: {adherence.cant_take} can't take, {adherence.skipped} skipped, {adherence.unconfirmed} unconfirmed.",
                "hi": f"दर्ज स्थिति: {adherence.cant_take} नहीं ले सके, {adherence.skipped} छोड़ी गई, {adherence.unconfirmed} अपुष्ट।",
            },
            {
                "id": "F3_CHECKINS",
                "en": f"Daily check-ins recorded: {sum(mood_counts.values())} ({mood_counts.get('not_well', 0)} marked Not well).",
                "hi": f"दैनिक हाल-चाल जांच दर्ज: {sum(mood_counts.values())} ({mood_counts.get('not_well', 0)} बार तबीयत ठीक नहीं)।",
            },
        ]

        ai_en = ""
        ai_hi = ""
        if use_ai_narrative and self._ai is not None:
            sel = self._ai.select_brief_narrative(actor, factual_statements=facts)
            if sel is not None:
                ai_en = render_narrative_from_facts(
                    facts, sel.selected_fact_ids, sel.template_id, lang="en"
                )
                ai_hi = render_narrative_from_facts(
                    facts, sel.selected_fact_ids, sel.template_id, lang="hi"
                )

        canonical_payload = json.dumps(
            {
                "period_days": period_days,
                "start": start_local.isoformat(),
                "end": end_local.isoformat(),
                "sections": sections,
                "ai_en": ai_en,
                "ai_hi": ai_hi,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

        existing = self._collab.list_briefs(actor.circle_id)
        next_ver = (max(b.version for b in existing) + 1) if existing else 1
        snap = DoctorBriefSnapshot(
            id=uuid.uuid4(),
            circle_id=actor.circle_id,
            period_days=period_days,
            period_start=start_local,
            period_end=end_local,
            timezone=actor.circle_timezone,
            source_categories=frozenset(source_cats),
            sections=sections,
            factual_statements=facts,
            ai_narrative_en=ai_en,
            ai_narrative_hi=ai_hi,
            content_hash=content_hash,
            version=next_ver,
            created_by_account_id=actor.account_id,
            created_at=now,
        )
        saved = self._collab.save_brief(snap)
        self._outbox.append_audit(
            AuditEvent(
                id=uuid.uuid4(),
                circle_id=actor.circle_id,
                actor_account_id=actor.account_id,
                actor_membership_id=actor.membership_id,
                operation="doctor_brief_generated",
                target_type="doctor_brief",
                target_id=str(saved.id),
                metadata={"version": saved.version},
                occurred_at=now,
            )
        )
        return saved

    def approve_brief(
        self,
        actor: ActorContext,
        *,
        brief_id: UUID,
        expected_content_hash: str,
        recipient_membership_ids: list[UUID],
    ) -> DoctorBriefSnapshot:
        """Elder-only approval bound to exact content_hash and allowed recipient set."""
        authorize(actor, Action.APPROVE_DOCTOR_BRIEF, target_circle_id=actor.circle_id)
        for rid in recipient_membership_ids:
            mem = self._circle.get_membership(actor.circle_id, rid)
            if not mem or not mem.active:
                raise AuthorizationError("errors.cross_circle_denied")
        now = self._clock.now()
        approved = self._collab.approve_brief(
            circle_id=actor.circle_id,
            brief_id=brief_id,
            expected_content_hash=expected_content_hash,
            recipient_membership_ids=tuple(dict.fromkeys(recipient_membership_ids)),
            now=now,
        )
        self._outbox.append_audit(
            AuditEvent(
                id=uuid.uuid4(),
                circle_id=actor.circle_id,
                actor_account_id=actor.account_id,
                actor_membership_id=actor.membership_id,
                operation="doctor_brief_approved",
                target_type="doctor_brief",
                target_id=str(brief_id),
                metadata={"version": approved.version, "recipients": len(recipient_membership_ids)},
                occurred_at=now,
            )
        )
        return approved

    def list_accessible_briefs(self, actor: ActorContext) -> list[DoctorBriefSnapshot]:
        """Filter briefs by doctor-brief consent, source categories, and elder approval."""
        authorize(actor, Action.VIEW_DOCTOR_BRIEF, target_circle_id=actor.circle_id)
        briefs = self._collab.list_briefs(actor.circle_id)
        if actor.role == Role.ELDER:
            return briefs
        visible: list[DoctorBriefSnapshot] = []
        for b in briefs:
            if not b.source_categories.issubset(actor.consents):
                continue
            if (
                b.approved_by_elder_at is not None
                and actor.membership_id in b.approved_recipient_membership_ids
            ):
                visible.append(b)
            else:
                try:
                    authorize(
                        actor,
                        Action.PREPARE_DOCTOR_BRIEF,
                        target_circle_id=actor.circle_id,
                        required_categories=tuple(b.source_categories),
                    )
                    visible.append(b)
                except (AuthorizationError, ConsentRequiredError):
                    continue
        return visible

    def get_brief_for_export(self, actor: ActorContext, *, brief_id: UUID) -> DoctorBriefSnapshot:
        """Enforce consent, source categories, and elder approval before screen/PDF export."""
        accessible = self.list_accessible_briefs(actor)
        target = next((b for b in accessible if b.id == brief_id), None)
        if not target:
            raise AuthorizationError("errors.forbidden")
        if actor.role != Role.ELDER and (
            target.approved_by_elder_at is None
            or actor.membership_id not in target.approved_recipient_membership_ids
        ):
            raise AuthorizationError("errors.forbidden")
        return target
