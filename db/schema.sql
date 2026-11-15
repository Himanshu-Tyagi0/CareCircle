-- CareCircle Canonical PostgreSQL Schema (Supabase-compatible)
-- All application tables live in the private `carecircle` schema.
-- `anon` and `authenticated` have ZERO direct table or RPC access.
-- Only `service_role` calls narrowly scoped SECURITY DEFINER RPCs.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS carecircle;

REVOKE ALL ON SCHEMA carecircle FROM PUBLIC;
REVOKE ALL ON SCHEMA carecircle FROM anon;
REVOKE ALL ON SCHEMA carecircle FROM authenticated;
GRANT USAGE ON SCHEMA carecircle TO service_role;

-- 1. Accounts
CREATE TABLE IF NOT EXISTS carecircle.accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_normalized TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 120),
    passcode_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending_verification', 'active', 'disabled')),
    failed_login_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_login_attempts >= 0),
    locked_until TIMESTAMPTZ,
    language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'hi')),
    font_px INTEGER NOT NULL DEFAULT 24 CHECK (font_px BETWEEN 18 AND 32),
    high_contrast BOOLEAN NOT NULL DEFAULT FALSE,
    telegram_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    telegram_chat_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Care Circles (elder profiles)
CREATE TABLE IF NOT EXISTS carecircle.care_circles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    elder_account_id UUID NOT NULL REFERENCES carecircle.accounts(id),
    display_name TEXT NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 120),
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    quiet_hours_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    quiet_hours_start TIME NOT NULL DEFAULT '22:00:00',
    quiet_hours_end TIME NOT NULL DEFAULT '07:00:00',
    elder_quiet_reminders_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    checkin_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    checkin_time TIME NOT NULL DEFAULT '09:00:00',
    checkin_window_minutes INTEGER NOT NULL DEFAULT 120 CHECK (checkin_window_minutes BETWEEN 15 AND 720),
    checkin_enabled_from TIMESTAMPTZ,
    primary_contact_membership_id UUID,
    secondary_contact_membership_id UUID,
    checkin_fallback_membership_id UUID,
    emergency_country_code TEXT,
    emergency_contact_name TEXT,
    emergency_contact_phone TEXT,
    ai_consent_granted BOOLEAN NOT NULL DEFAULT FALSE,
    ai_consent_updated_at TIMESTAMPTZ,
    meal_breakfast_time TIME NOT NULL DEFAULT '08:00:00',
    meal_lunch_time TIME NOT NULL DEFAULT '13:00:00',
    meal_dinner_time TIME NOT NULL DEFAULT '20:00:00',
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_owned_circle_per_elder
    ON carecircle.care_circles (elder_account_id)
    WHERE deleted_at IS NULL;

-- 3. Memberships (composite unique (circle_id, id) enforces cross-circle ownership)
CREATE TABLE IF NOT EXISTS carecircle.memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    account_id UUID NOT NULL REFERENCES carecircle.accounts(id),
    role TEXT NOT NULL CHECK (role IN ('elder', 'caretaker', 'family')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    is_care_contact BOOLEAN NOT NULL DEFAULT FALSE,
    grants TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    auth_revision INTEGER NOT NULL DEFAULT 1 CHECK (auth_revision >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (circle_id, id),
    UNIQUE (circle_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_account_active
    ON carecircle.memberships (account_id, active);

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_elder_per_circle
    ON carecircle.memberships (circle_id)
    WHERE role = 'elder' AND active = TRUE;

-- Cross-circle composite FKs preventing assigning contacts from another circle
ALTER TABLE carecircle.care_circles
    DROP CONSTRAINT IF EXISTS fk_circle_primary_contact;
ALTER TABLE carecircle.care_circles
    ADD CONSTRAINT fk_circle_primary_contact
    FOREIGN KEY (id, primary_contact_membership_id)
    REFERENCES carecircle.memberships (circle_id, id);

ALTER TABLE carecircle.care_circles
    DROP CONSTRAINT IF EXISTS fk_circle_secondary_contact;
ALTER TABLE carecircle.care_circles
    ADD CONSTRAINT fk_circle_secondary_contact
    FOREIGN KEY (id, secondary_contact_membership_id)
    REFERENCES carecircle.memberships (circle_id, id);

ALTER TABLE carecircle.care_circles
    DROP CONSTRAINT IF EXISTS fk_circle_checkin_fallback;
ALTER TABLE carecircle.care_circles
    ADD CONSTRAINT fk_circle_checkin_fallback
    FOREIGN KEY (id, checkin_fallback_membership_id)
    REFERENCES carecircle.memberships (circle_id, id);

-- 4. Per-member category consents
CREATE TABLE IF NOT EXISTS carecircle.member_consents (
    circle_id UUID NOT NULL,
    membership_id UUID NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('medications', 'adherence', 'checkins', 'appointments', 'doctor_brief')),
    granted BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by_account_id UUID NOT NULL REFERENCES carecircle.accounts(id),
    PRIMARY KEY (circle_id, membership_id, category),
    FOREIGN KEY (circle_id, membership_id) REFERENCES carecircle.memberships (circle_id, id) ON DELETE CASCADE
);

-- 5. Pending email verification for onboarding & invitations
CREATE TABLE IF NOT EXISTS carecircle.pending_verifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash TEXT NOT NULL UNIQUE,
    email_normalized TEXT NOT NULL,
    display_name TEXT NOT NULL,
    passcode_hash TEXT NOT NULL,
    timezone TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('en', 'hi')),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS carecircle.invitations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    invited_email_normalized TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('caretaker', 'family')),
    grants TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    initial_consents TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    is_care_contact BOOLEAN NOT NULL DEFAULT FALSE,
    invited_by_account_id UUID NOT NULL REFERENCES carecircle.accounts(id),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_invitations_circle ON carecircle.invitations (circle_id);

-- 6. Sessions & login rate throttle
CREATE TABLE IF NOT EXISTS carecircle.sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash TEXT NOT NULL UNIQUE,
    account_id UUID NOT NULL REFERENCES carecircle.accounts(id),
    membership_id UUID NOT NULL,
    circle_id UUID NOT NULL,
    auth_revision INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idle_expires_at TIMESTAMPTZ NOT NULL,
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    FOREIGN KEY (circle_id, membership_id) REFERENCES carecircle.memberships (circle_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON carecircle.sessions (idle_expires_at, absolute_expires_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_account ON carecircle.sessions (account_id);

CREATE TABLE IF NOT EXISTS carecircle.login_throttle_buckets (
    bucket_minute TIMESTAMPTZ PRIMARY KEY,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)
);

-- 7. Medications & Versioned Schedules & Dose Instances
CREATE TABLE IF NOT EXISTS carecircle.medications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    dose_text TEXT NOT NULL CHECK (char_length(dose_text) BETWEEN 1 AND 120),
    instructions TEXT NOT NULL DEFAULT '',
    start_date DATE NOT NULL,
    end_date DATE,
    criticality TEXT NOT NULL DEFAULT 'normal' CHECK (criticality IN ('critical', 'normal', 'optional')),
    stock_count DOUBLE PRECISION CHECK (stock_count IS NULL OR stock_count >= 0),
    stock_unit TEXT NOT NULL DEFAULT '',
    refill_threshold DOUBLE PRECISION CHECK (refill_threshold IS NULL OR refill_threshold >= 0),
    stock_units_per_dose DOUBLE PRECISION CHECK (stock_units_per_dose IS NULL OR stock_units_per_dose > 0),
    low_stock_alert_active BOOLEAN NOT NULL DEFAULT FALSE,
    archived_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (circle_id, id),
    CHECK (end_date IS NULL OR end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS idx_medications_circle_active ON carecircle.medications (circle_id, archived_at);

CREATE TABLE IF NOT EXISTS carecircle.medication_schedules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL,
    medication_id UUID NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    times_local TIME[] NOT NULL CHECK (cardinality(times_local) >= 1),
    weekdays INTEGER[] NOT NULL CHECK (cardinality(weekdays) >= 1),
    start_date DATE NOT NULL,
    end_date DATE,
    timezone TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (circle_id, id),
    FOREIGN KEY (circle_id, medication_id) REFERENCES carecircle.medications (circle_id, id)
);

CREATE INDEX IF NOT EXISTS idx_schedules_med_active ON carecircle.medication_schedules (medication_id, active);

CREATE TABLE IF NOT EXISTS carecircle.dose_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL,
    medication_id UUID NOT NULL,
    schedule_id UUID NOT NULL,
    schedule_version INTEGER NOT NULL,
    medication_name TEXT NOT NULL,
    dose_text TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    criticality TEXT NOT NULL CHECK (criticality IN ('critical', 'normal', 'optional')),
    due_at TIMESTAMPTZ NOT NULL,
    local_date DATE NOT NULL,
    local_time_intended TIME NOT NULL,
    dst_adjusted BOOLEAN NOT NULL DEFAULT FALSE,
    dst_adjustment_note TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'snoozed', 'taken', 'can''t_take', 'skipped', 'cancelled', 'expired')),
    snoozed_until TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    confirmed_by_account_id UUID REFERENCES carecircle.accounts(id),
    cant_take_report_id UUID,
    cant_take_reason TEXT CHECK (cant_take_reason IS NULL OR cant_take_reason IN ('unwell', 'nausea_or_side_effect', 'out_of_stock', 'doctor_advised_skip', 'other')),
    cant_take_note TEXT NOT NULL DEFAULT '',
    skip_reason TEXT NOT NULL DEFAULT '',
    inventory_decremented BOOLEAN NOT NULL DEFAULT FALSE,
    inventory_discrepancy BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_high_water INTEGER NOT NULL DEFAULT 0,
    escalation_state JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (circle_id, id),
    UNIQUE (medication_id, due_at),
    FOREIGN KEY (circle_id, medication_id) REFERENCES carecircle.medications (circle_id, id),
    FOREIGN KEY (circle_id, schedule_id) REFERENCES carecircle.medication_schedules (circle_id, id)
);

CREATE INDEX IF NOT EXISTS idx_dose_instances_circle_due ON carecircle.dose_instances (circle_id, due_at);
CREATE INDEX IF NOT EXISTS idx_dose_instances_active_window ON carecircle.dose_instances (due_at, expires_at) WHERE state IN ('pending', 'snoozed', 'can''t_take');

CREATE TABLE IF NOT EXISTS carecircle.dose_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL,
    dose_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    actor_account_id UUID REFERENCES carecircle.accounts(id),
    previous_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    stock_delta DOUBLE PRECISION NOT NULL DEFAULT 0,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (circle_id, dose_id) REFERENCES carecircle.dose_instances (circle_id, id)
);

CREATE INDEX IF NOT EXISTS idx_dose_events_dose ON carecircle.dose_events (dose_id, occurred_at);

-- 8. Appointments, Check-ins, Acknowledgements, Notes, Briefs
CREATE TABLE IF NOT EXISTS carecircle.appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    doctor_name TEXT NOT NULL CHECK (char_length(doctor_name) BETWEEN 1 AND 120),
    specialty TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    starts_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_appointments_circle_starts ON carecircle.appointments (circle_id, starts_at);

CREATE TABLE IF NOT EXISTS carecircle.checkin_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    local_date DATE NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    window_ends_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    mood TEXT CHECK (mood IS NULL OR mood IN ('good', 'okay', 'not_well')),
    note TEXT NOT NULL DEFAULT '',
    responded_at TIMESTAMPTZ,
    responded_by_account_id UUID REFERENCES carecircle.accounts(id),
    escalation_high_water INTEGER NOT NULL DEFAULT 0,
    escalation_state JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (circle_id, local_date)
);

CREATE INDEX IF NOT EXISTS idx_checkins_active ON carecircle.checkin_instances (circle_id, expires_at);

CREATE TABLE IF NOT EXISTS carecircle.care_acknowledgements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    episode_type TEXT NOT NULL CHECK (episode_type IN ('dose', 'checkin', 'urgent_help')),
    resource_id UUID NOT NULL,
    acknowledged_by_membership_id UUID NOT NULL,
    acknowledged_by_name TEXT NOT NULL,
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (episode_type, resource_id),
    FOREIGN KEY (circle_id, acknowledged_by_membership_id) REFERENCES carecircle.memberships (circle_id, id)
);

CREATE TABLE IF NOT EXISTS carecircle.care_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    author_account_id UUID NOT NULL REFERENCES carecircle.accounts(id),
    author_name TEXT NOT NULL,
    note_type TEXT NOT NULL CHECK (note_type IN ('family_note', 'elder_question')),
    content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_care_notes_circle ON carecircle.care_notes (circle_id, created_at);

CREATE TABLE IF NOT EXISTS carecircle.doctor_briefs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    period_days INTEGER NOT NULL CHECK (period_days BETWEEN 1 AND 90),
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    timezone TEXT NOT NULL,
    source_categories TEXT[] NOT NULL,
    sections JSONB NOT NULL,
    factual_statements JSONB NOT NULL DEFAULT '[]'::JSONB,
    ai_narrative_en TEXT NOT NULL DEFAULT '',
    ai_narrative_hi TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    approved_by_elder_at TIMESTAMPTZ,
    approved_recipient_membership_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    created_by_account_id UUID NOT NULL REFERENCES carecircle.accounts(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doctor_briefs_circle ON carecircle.doctor_briefs (circle_id, created_at DESC);

-- 9. Signed Action Tokens, Outbox, Audit, Worker Health
CREATE TABLE IF NOT EXISTS carecircle.action_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    token_hash TEXT NOT NULL UNIQUE,
    key_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID NOT NULL,
    recipient_membership_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    invalidated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (circle_id, recipient_membership_id) REFERENCES carecircle.memberships (circle_id, id)
);

CREATE INDEX IF NOT EXISTS idx_action_tokens_expiry ON carecircle.action_tokens (expires_at) WHERE consumed_at IS NULL AND invalidated_at IS NULL;

CREATE TABLE IF NOT EXISTS carecircle.notification_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL REFERENCES carecircle.care_circles(id),
    logical_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID NOT NULL,
    escalation_level INTEGER NOT NULL DEFAULT 0,
    recipient_membership_id UUID NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'telegram')),
    required_category TEXT,
    resource_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'leased', 'sending', 'sent', 'retryable', 'uncertain', 'failed', 'cancelled', 'superseded')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    lease_owner TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    last_error_code TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (circle_id, recipient_membership_id) REFERENCES carecircle.memberships (circle_id, id)
);

CREATE INDEX IF NOT EXISTS idx_outbox_claimable ON carecircle.notification_log (status, next_attempt_at) WHERE status IN ('pending', 'retryable');
CREATE INDEX IF NOT EXISTS idx_outbox_circle ON carecircle.notification_log (circle_id, created_at DESC);

CREATE TABLE IF NOT EXISTS carecircle.audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    circle_id UUID NOT NULL,
    actor_account_id UUID,
    actor_membership_id UUID,
    operation TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_circle_time ON carecircle.audit_events (circle_id, occurred_at DESC);

CREATE OR REPLACE FUNCTION carecircle.prevent_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('carecircle.allow_circle_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_immutable ON carecircle.audit_events;
CREATE TRIGGER trg_audit_immutable
    BEFORE UPDATE OR DELETE ON carecircle.audit_events
    FOR EACH ROW EXECUTE FUNCTION carecircle.prevent_audit_mutation();

CREATE TABLE IF NOT EXISTS carecircle.worker_health (
    worker_name TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error_category TEXT NOT NULL DEFAULT '',
    due_event_lag_seconds INTEGER NOT NULL DEFAULT 0,
    uncertain_send_count INTEGER NOT NULL DEFAULT 0,
    last_summary JSONB NOT NULL DEFAULT '{}'::JSONB
);

-- Enable RLS as defense-in-depth on every table; default-deny for anon/authenticated.
-- Note: service_role bypasses RLS by design; isolation is enforced via restricted privileges and RPC validation.
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'accounts', 'care_circles', 'memberships', 'member_consents',
        'pending_verifications', 'invitations', 'sessions', 'login_throttle_buckets',
        'medications', 'medication_schedules', 'dose_instances', 'dose_events',
        'appointments', 'checkin_instances', 'care_acknowledgements', 'care_notes',
        'doctor_briefs', 'action_tokens', 'notification_log', 'audit_events', 'worker_health'
    ]
    LOOP
        EXECUTE format('ALTER TABLE carecircle.%I ENABLE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('ALTER TABLE carecircle.%I FORCE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('REVOKE ALL ON TABLE carecircle.%I FROM PUBLIC, anon, authenticated;', tbl);
        EXECUTE format('GRANT ALL ON TABLE carecircle.%I TO service_role;', tbl);
    END LOOP;
END
$$;
