"""Isolated synthetic demo seed for English and Hindi Care Circles (never runs in production)."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.clock import SystemClock
from core.config import load_config
from core.models import (
    Account,
    AccountStatus,
    CantTakeReason,
    CareCircle,
    CheckInMood,
    ConsentCategory,
    Criticality,
    DoseState,
    FamilyGrant,
    MemberConsent,
    Membership,
    Role,
)
from core.models_clinical import (
    Appointment,
    AuditEvent,
    CareNote,
    CheckInInstance,
    Medication,
    MedicationSchedule,
)
from core.security import hash_passcode
from repositories.demo_store import DemoStore
from services.schedule_service import expand_schedule

# Public synthetic demo credentials (RFC 2606 .invalid domain — never real accounts)
DEMO_PASSCODE_ELDER = "demo-elder-2026"
DEMO_PASSCODE_CARETAKER = "demo-care-2026"
DEMO_PASSCODE_FAMILY = "demo-family-2026"

DEMO_ACCOUNTS = (
    ("elder-en@demo.carecircle.invalid", "Asha Sharma", "en", Role.ELDER, DEMO_PASSCODE_ELDER),
    (
        "caretaker-en@demo.carecircle.invalid",
        "Rohan Verma",
        "en",
        Role.CARETAKER,
        DEMO_PASSCODE_CARETAKER,
    ),
    ("family-en@demo.carecircle.invalid", "Meera Kapoor", "en", Role.FAMILY, DEMO_PASSCODE_FAMILY),
    ("elder-hi@demo.carecircle.invalid", "कमला देवी", "hi", Role.ELDER, DEMO_PASSCODE_ELDER),
    (
        "caretaker-hi@demo.carecircle.invalid",
        "सुरेश तिवारी",
        "hi",
        Role.CARETAKER,
        DEMO_PASSCODE_CARETAKER,
    ),
    ("family-hi@demo.carecircle.invalid", "अनीता शर्मा", "hi", Role.FAMILY, DEMO_PASSCODE_FAMILY),
)


def _seed_circle(
    store: DemoStore,
    *,
    now: datetime,
    lang: str,
    tz_name: str,
    elder_email: str,
    elder_name: str,
    care_email: str,
    care_name: str,
    fam_email: str,
    fam_name: str,
    pass_hashes: dict[str, str],
) -> None:
    if elder_email in store.email_index:
        return

    elder_id = uuid.uuid4()
    care_id = uuid.uuid4()
    fam_id = uuid.uuid4()
    circle_id = uuid.uuid4()
    m_elder = uuid.uuid4()
    m_care = uuid.uuid4()
    m_fam = uuid.uuid4()

    for uid, email, name, _role, font_px, p_hash in (
        (elder_id, elder_email, elder_name, Role.ELDER, 24, pass_hashes["elder"]),
        (care_id, care_email, care_name, Role.CARETAKER, 18, pass_hashes["caretaker"]),
        (fam_id, fam_email, fam_name, Role.FAMILY, 18, pass_hashes["family"]),
    ):
        store.accounts[uid] = Account(
            id=uid,
            email_normalized=email,
            display_name=name,
            passcode_hash=p_hash,
            status=AccountStatus.ACTIVE,
            language=lang,
            font_px=font_px,
            created_at=now,
        )
        store.email_index[email] = uid

    store.circles[circle_id] = CareCircle(
        id=circle_id,
        elder_account_id=elder_id,
        display_name=elder_name,
        timezone=tz_name,
        quiet_hours_enabled=True,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
        elder_quiet_reminders_enabled=True,
        checkin_enabled=True,
        checkin_time=time(9, 0),
        checkin_window_minutes=120,
        checkin_enabled_from=now - timedelta(days=5),
        primary_contact_membership_id=m_care,
        secondary_contact_membership_id=m_fam,
        checkin_fallback_membership_id=m_fam,
        emergency_country_code="IN",
        emergency_contact_name=care_name,
        emergency_contact_phone="+91-00000-00001",
        ai_consent_granted=False,
        created_at=now,
    )

    store.memberships[m_elder] = Membership(
        id=m_elder, circle_id=circle_id, account_id=elder_id, role=Role.ELDER, created_at=now
    )
    store.memberships[m_care] = Membership(
        id=m_care,
        circle_id=circle_id,
        account_id=care_id,
        role=Role.CARETAKER,
        is_care_contact=True,
        created_at=now,
    )
    fam_grants = frozenset({FamilyGrant.ADD_OWN_NOTE}) if lang == "en" else frozenset()
    store.memberships[m_fam] = Membership(
        id=m_fam,
        circle_id=circle_id,
        account_id=fam_id,
        role=Role.FAMILY,
        is_care_contact=True,
        grants=fam_grants,
        created_at=now,
    )

    for cat in ConsentCategory:
        store.consents[(circle_id, m_care, cat)] = MemberConsent(
            circle_id=circle_id,
            membership_id=m_care,
            category=cat,
            granted=True,
            updated_at=now,
            updated_by_account_id=elder_id,
        )
        fam_granted = (
            cat
            in (
                ConsentCategory.MEDICATIONS,
                ConsentCategory.ADHERENCE,
                ConsentCategory.DOCTOR_BRIEF,
            )
            if lang == "en"
            else cat == ConsentCategory.MEDICATIONS
        )
        store.consents[(circle_id, m_fam, cat)] = MemberConsent(
            circle_id=circle_id,
            membership_id=m_fam,
            category=cat,
            granted=fam_granted,
            updated_at=now,
            updated_by_account_id=elder_id,
        )

    tz = ZoneInfo(tz_name)
    today = now.astimezone(tz).date()
    med_specs = (
        (
            "Metformin" if lang == "en" else "मेटफॉर्मिन (Metformin)",
            "500 mg (1 tablet)" if lang == "en" else "500 mg (1 गोली)",
            "After breakfast with water" if lang == "en" else "नाश्ते के बाद पानी के साथ",
            Criticality.CRITICAL,
            24.0,
            "tablets" if lang == "en" else "गोलियाँ",
            6.0,
            1.0,
            (time(8, 0), time(20, 0)),
        ),
        (
            "Amlodipine" if lang == "en" else "एम्लोडिपाइन (Amlodipine)",
            "5 mg (1 tablet)" if lang == "en" else "5 mg (1 गोली)",
            "Morning before walk" if lang == "en" else "सुबह टहलने से पहले",
            Criticality.NORMAL,
            14.0,
            "tablets" if lang == "en" else "गोलियाँ",
            5.0,
            1.0,
            (time(9, 30),),
        ),
        (
            "Vitamin D3" if lang == "en" else "विटामिन D3",
            "1000 IU (1 capsule)" if lang == "en" else "1000 IU (1 कैप्सूल)",
            "After lunch" if lang == "en" else "दोपहर के खाने के बाद",
            Criticality.OPTIONAL,
            30.0,
            "capsules" if lang == "en" else "कैप्सूल",
            5.0,
            1.0,
            (time(13, 30),),
        ),
    )

    for idx, (m_name, dose_txt, instr, crit, stock, unit, thresh, upd, times) in enumerate(
        med_specs
    ):
        med_id = uuid.uuid4()
        med = Medication(
            id=med_id,
            circle_id=circle_id,
            name=m_name,
            dose_text=dose_txt,
            instructions=instr,
            start_date=today - timedelta(days=10),
            criticality=crit,
            stock_count=stock,
            stock_unit=unit,
            refill_threshold=thresh,
            stock_units_per_dose=upd,
            created_at=now - timedelta(days=10),
            updated_at=now,
        )
        sched = MedicationSchedule(
            id=uuid.uuid4(),
            circle_id=circle_id,
            medication_id=med_id,
            version=1,
            times_local=times,
            weekdays=(0, 1, 2, 3, 4, 5, 6),
            start_date=today - timedelta(days=10),
            timezone=tz_name,
            active=True,
            created_at=now - timedelta(days=10),
        )
        store.medications[med_id] = med
        store.schedules[sched.id] = sched

        generated = expand_schedule(
            schedule=sched,
            medication=med,
            window_start_utc=now - timedelta(days=4),
            window_end_utc=now + timedelta(hours=48),
            now=now,
        )
        for d_idx, d in enumerate(generated):
            if d.due_at < now - timedelta(hours=2):
                if idx == 1 and d_idx == 1:
                    d = d.model_copy(
                        update={
                            "state": DoseState.CANT_TAKE,
                            "cant_take_reason": CantTakeReason.NAUSEA_OR_SIDE_EFFECT,
                            "cant_take_report_id": uuid.uuid4(),
                        }
                    )
                else:
                    d = d.model_copy(
                        update={
                            "state": DoseState.TAKEN,
                            "confirmed_at": d.due_at + timedelta(minutes=8),
                            "confirmed_by_account_id": elder_id,
                        }
                    )
            elif d.due_at <= now:
                d = d.model_copy(update={"state": DoseState.PENDING})
            store.doses[d.id] = d
            store.dose_med_due_index[(d.medication_id, d.due_at)] = d.id

    appt = Appointment(
        id=uuid.uuid4(),
        circle_id=circle_id,
        doctor_name="Dr. Kavita Rao" if lang == "en" else "डॉ. कविता राव",
        specialty="Internal Medicine" if lang == "en" else "सामान्य चिकित्सा",
        location="City Health Clinic" if lang == "en" else "सिटी हेल्थ क्लिनिक",
        starts_at=now + timedelta(days=3),
        timezone=tz_name,
        notes="Bring 30-day brief" if lang == "en" else "30 दिन का सारांश साथ लाएं",
        created_at=now,
        updated_at=now,
    )
    store.appointments[appt.id] = appt

    for offset, mood in ((3, CheckInMood.GOOD), (2, CheckInMood.GOOD), (1, CheckInMood.OKAY)):
        c_date = today - timedelta(days=offset)
        due_dt = datetime.combine(c_date, time(9, 0), tzinfo=tz).astimezone(ZoneInfo("UTC"))
        ci = CheckInInstance(
            id=uuid.uuid4(),
            circle_id=circle_id,
            local_date=c_date,
            due_at=due_dt,
            window_ends_at=due_dt + timedelta(hours=2),
            expires_at=due_dt + timedelta(days=1),
            mood=mood,
            responded_at=due_dt + timedelta(minutes=15),
            responded_by_account_id=elder_id,
            created_at=due_dt,
            updated_at=due_dt,
        )
        store.checkins[ci.id] = ci
        store.checkin_date_index[(circle_id, c_date)] = ci.id

    q_note = CareNote(
        id=uuid.uuid4(),
        circle_id=circle_id,
        author_account_id=elder_id,
        author_name=elder_name,
        note_type="elder_question",
        content=(
            "Should morning Amlodipine be taken before or after breakfast?"
            if lang == "en"
            else "क्या सुबह की एम्लोडिपाइन नाश्ते से पहले लेनी चाहिए या बाद में?"
        ),
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(days=1),
    )
    f_note = CareNote(
        id=uuid.uuid4(),
        circle_id=circle_id,
        author_account_id=fam_id,
        author_name=fam_name,
        note_type="family_note",
        content=(
            "Noticed mild dizziness on Tuesday afternoon; rested well afterward."
            if lang == "en"
            else "मंगलवार दोपहर हल्का चक्कर आया था; आराम करने के बाद ठीक लगा।"
        ),
        created_at=now - timedelta(hours=12),
        updated_at=now - timedelta(hours=12),
    )
    store.notes[q_note.id] = q_note
    store.notes[f_note.id] = f_note

    store.audit_events.append(
        AuditEvent(
            id=uuid.uuid4(),
            circle_id=circle_id,
            actor_account_id=elder_id,
            actor_membership_id=m_elder,
            operation="demo_circle_seeded",
            target_type="circle",
            target_id=str(circle_id),
            occurred_at=now,
        )
    )


def seed_demo_store(store: DemoStore, now: datetime, *, rounds: int = 8) -> None:
    """Populate isolated English and Hindi demo circles safely (idempotent)."""
    with store.lock:
        if store.accounts:
            return
        hashes = {
            "elder": hash_passcode(DEMO_PASSCODE_ELDER, rounds=rounds),
            "caretaker": hash_passcode(DEMO_PASSCODE_CARETAKER, rounds=rounds),
            "family": hash_passcode(DEMO_PASSCODE_FAMILY, rounds=rounds),
        }
        _seed_circle(
            store,
            now=now,
            lang="en",
            tz_name="Asia/Kolkata",
            elder_email="elder-en@demo.carecircle.invalid",
            elder_name="Asha Sharma",
            care_email="caretaker-en@demo.carecircle.invalid",
            care_name="Rohan Verma",
            fam_email="family-en@demo.carecircle.invalid",
            fam_name="Meera Kapoor",
            pass_hashes=hashes,
        )
        _seed_circle(
            store,
            now=now,
            lang="hi",
            tz_name="Asia/Kolkata",
            elder_email="elder-hi@demo.carecircle.invalid",
            elder_name="कमला देवी",
            care_email="caretaker-hi@demo.carecircle.invalid",
            care_name="सुरेश तिवारी",
            fam_email="family-hi@demo.carecircle.invalid",
            fam_name="अनीता शर्मा",
            pass_hashes=hashes,
        )


def main(argv: list[str] | None = None) -> int:
    """Explicit opt-in CLI for demo seeding verification."""
    parser = argparse.ArgumentParser(description="CareCircle isolated demo seed tool")
    parser.add_argument(
        "--confirm-demo-seed", action="store_true", help="Explicitly authorize demo seed"
    )
    args = parser.parse_args(argv)
    if not args.confirm_demo_seed:
        print("Refusing to seed without --confirm-demo-seed", file=sys.stderr)
        return 2
    cfg = load_config()
    if not cfg.demo_mode:
        print("Refusing to seed while CARECIRCLE_DEMO_MODE is false", file=sys.stderr)
        return 2
    store = DemoStore()
    seed_demo_store(store, SystemClock().now(), rounds=cfg.bcrypt_rounds)
    print(
        f"Seeded isolated demo store: {len(store.circles)} circles, {len(store.accounts)} accounts."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
