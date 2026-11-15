-- CareCircle Server-Only SECURITY DEFINER RPCs
-- All RPCs enforce explicit schema qualification, safe search_path, zero dynamic SQL,
-- and execution granted ONLY to service_role.

CREATE OR REPLACE FUNCTION public.cc_resolve_session(
    p_token_hash TEXT,
    p_now TIMESTAMPTZ,
    p_idle_minutes INTEGER
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = carecircle, pg_temp
AS $$
DECLARE
    v_sess carecircle.sessions%ROWTYPE;
    v_acc carecircle.accounts%ROWTYPE;
    v_mem carecircle.memberships%ROWTYPE;
    v_circle carecircle.care_circles%ROWTYPE;
    v_consents TEXT[];
BEGIN
    SELECT * INTO v_sess
    FROM carecircle.sessions s
    WHERE s.token_hash = p_token_hash
      AND s.revoked_at IS NULL
      AND s.idle_expires_at > p_now
      AND s.absolute_expires_at > p_now
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_acc FROM carecircle.accounts WHERE id = v_sess.account_id AND status = 'active';
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_mem
    FROM carecircle.memberships
    WHERE id = v_sess.membership_id
      AND circle_id = v_sess.circle_id
      AND account_id = v_sess.account_id
      AND active = TRUE
      AND auth_revision = v_sess.auth_revision;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_circle FROM carecircle.care_circles WHERE id = v_sess.circle_id AND deleted_at IS NULL;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    UPDATE carecircle.sessions
    SET last_seen_at = p_now,
        idle_expires_at = LEAST(absolute_expires_at, p_now + make_interval(mins => p_idle_minutes))
    WHERE id = v_sess.id;

    IF v_mem.role = 'elder' THEN
        v_consents := ARRAY['medications', 'adherence', 'checkins', 'appointments', 'doctor_brief'];
    ELSE
        SELECT COALESCE(array_agg(mc.category), ARRAY[]::TEXT[]) INTO v_consents
        FROM carecircle.member_consents mc
        WHERE mc.circle_id = v_mem.circle_id
          AND mc.membership_id = v_mem.id
          AND mc.granted = TRUE;
    END IF;

    RETURN jsonb_build_object(
        'session_id', v_sess.id,
        'account_id', v_acc.id,
        'membership_id', v_mem.id,
        'circle_id', v_circle.id,
        'role', v_mem.role,
        'display_name', v_acc.display_name,
        'language', v_acc.language,
        'font_px', v_acc.font_px,
        'high_contrast', v_acc.high_contrast,
        'is_care_contact', v_mem.is_care_contact,
        'grants', to_jsonb(v_mem.grants),
        'consents', to_jsonb(v_consents),
        'auth_revision', v_mem.auth_revision,
        'circle_timezone', v_circle.timezone,
        'ai_consent_granted', v_circle.ai_consent_granted
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.cc_record_login_attempt(
    p_email TEXT,
    p_success BOOLEAN,
    p_now TIMESTAMPTZ,
    p_max_failures INTEGER,
    p_lockout_minutes INTEGER,
    p_global_limit_per_min INTEGER
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = carecircle, pg_temp
AS $$
DECLARE
    v_bucket TIMESTAMPTZ := date_trunc('minute', p_now);
    v_global_count INTEGER;
    v_acc carecircle.accounts%ROWTYPE;
    v_new_failures INTEGER;
    v_locked_until TIMESTAMPTZ;
BEGIN
    INSERT INTO carecircle.login_throttle_buckets (bucket_minute, attempt_count)
    VALUES (v_bucket, 1)
    ON CONFLICT (bucket_minute)
    DO UPDATE SET attempt_count = carecircle.login_throttle_buckets.attempt_count + 1
    RETURNING attempt_count INTO v_global_count;

    IF v_global_count > p_global_limit_per_min THEN
        RETURN jsonb_build_object('status', 'rate_limited');
    END IF;

    SELECT * INTO v_acc
    FROM carecircle.accounts
    WHERE email_normalized = p_email
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'not_found');
    END IF;

    IF v_acc.locked_until IS NOT NULL AND v_acc.locked_until > p_now THEN
        RETURN jsonb_build_object('status', 'locked', 'locked_until', v_acc.locked_until);
    END IF;

    IF p_success THEN
        UPDATE carecircle.accounts
        SET failed_login_attempts = 0, locked_until = NULL
        WHERE id = v_acc.id;
        RETURN jsonb_build_object('status', 'ok', 'account_id', v_acc.id);
    END IF;

    v_new_failures := v_acc.failed_login_attempts + 1;
    IF v_new_failures >= p_max_failures THEN
        v_locked_until := p_now + make_interval(mins => p_lockout_minutes);
        UPDATE carecircle.accounts
        SET failed_login_attempts = v_new_failures, locked_until = v_locked_until
        WHERE id = v_acc.id;
        RETURN jsonb_build_object('status', 'locked', 'locked_until', v_locked_until);
    ELSE
        UPDATE carecircle.accounts
        SET failed_login_attempts = v_new_failures
        WHERE id = v_acc.id;
        RETURN jsonb_build_object('status', 'failed', 'attempts', v_new_failures);
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.cc_redeem_verification(
    p_token_hash TEXT,
    p_now TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = carecircle, pg_temp
AS $$
DECLARE
    v_pv carecircle.pending_verifications%ROWTYPE;
    v_account_id UUID;
    v_circle_id UUID;
    v_membership_id UUID;
BEGIN
    SELECT * INTO v_pv
    FROM carecircle.pending_verifications
    WHERE token_hash = p_token_hash
      AND consumed_at IS NULL
      AND expires_at > p_now
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'invalid');
    END IF;

    IF EXISTS (SELECT 1 FROM carecircle.accounts WHERE email_normalized = v_pv.email_normalized) THEN
        RETURN jsonb_build_object('status', 'conflict');
    END IF;

    UPDATE carecircle.pending_verifications SET consumed_at = p_now WHERE id = v_pv.id;

    INSERT INTO carecircle.accounts (email_normalized, display_name, passcode_hash, status, language, font_px)
    VALUES (v_pv.email_normalized, v_pv.display_name, v_pv.passcode_hash, 'active', v_pv.language, 24)
    RETURNING id INTO v_account_id;

    INSERT INTO carecircle.care_circles (elder_account_id, display_name, timezone, checkin_enabled_from)
    VALUES (v_account_id, v_pv.display_name, v_pv.timezone, p_now)
    RETURNING id INTO v_circle_id;

    INSERT INTO carecircle.memberships (circle_id, account_id, role, active, is_care_contact)
    VALUES (v_circle_id, v_account_id, 'elder', TRUE, FALSE)
    RETURNING id INTO v_membership_id;

    INSERT INTO carecircle.audit_events (circle_id, actor_account_id, actor_membership_id, operation, target_type, target_id, metadata, occurred_at)
    VALUES (v_circle_id, v_account_id, v_membership_id, 'elder_onboarding_verified', 'circle', v_circle_id::TEXT, '{}'::JSONB, p_now);

    RETURN jsonb_build_object('status', 'ok', 'account_id', v_account_id, 'circle_id', v_circle_id, 'membership_id', v_membership_id);
END;
$$;

CREATE OR REPLACE FUNCTION public.cc_confirm_dose_taken(
    p_token_hash TEXT,
    p_dose_id UUID,
    p_now TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = carecircle, pg_temp
AS $$
DECLARE
    v_actor JSONB;
    v_dose carecircle.dose_instances%ROWTYPE;
    v_med carecircle.medications%ROWTYPE;
    v_role TEXT;
    v_grants TEXT[];
    v_consents TEXT[];
    v_discrepancy BOOLEAN := FALSE;
    v_decremented BOOLEAN := FALSE;
    v_stock_delta DOUBLE PRECISION := 0;
BEGIN
    v_actor := public.cc_resolve_session(p_token_hash, p_now, 30);
    IF v_actor IS NULL THEN
        RETURN jsonb_build_object('status', 'unauthorized');
    END IF;

    v_role := v_actor->>'role';
    v_grants := ARRAY(SELECT jsonb_array_elements_text(v_actor->'grants'));
    v_consents := ARRAY(SELECT jsonb_array_elements_text(v_actor->'consents'));

    IF v_role = 'family' AND NOT ('log_doses' = ANY(v_grants)) THEN
        RETURN jsonb_build_object('status', 'forbidden');
    END IF;
    IF v_role <> 'elder' AND NOT ('medications' = ANY(v_consents)) THEN
        RETURN jsonb_build_object('status', 'consent_required');
    END IF;

    SELECT * INTO v_dose
    FROM carecircle.dose_instances
    WHERE id = p_dose_id AND circle_id = (v_actor->>'circle_id')::UUID
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'not_found');
    END IF;

    IF v_dose.state = 'taken' THEN
        RETURN jsonb_build_object('status', 'already_taken', 'confirmed_at', v_dose.confirmed_at);
    END IF;

    SELECT * INTO v_med FROM carecircle.medications WHERE id = v_dose.medication_id FOR UPDATE;

    IF v_med.stock_count IS NOT NULL AND v_med.stock_units_per_dose IS NOT NULL AND NOT v_dose.inventory_decremented THEN
        IF v_med.stock_count <= 0 THEN
            v_discrepancy := TRUE;
        ELSE
            v_stock_delta := -LEAST(v_med.stock_count, v_med.stock_units_per_dose);
            UPDATE carecircle.medications
            SET stock_count = GREATEST(0, stock_count - v_med.stock_units_per_dose),
                updated_at = p_now
            WHERE id = v_med.id;
            v_decremented := TRUE;
        END IF;
    END IF;

    UPDATE carecircle.dose_instances
    SET state = 'taken',
        confirmed_at = p_now,
        confirmed_by_account_id = (v_actor->>'account_id')::UUID,
        inventory_decremented = v_dose.inventory_decremented OR v_decremented,
        inventory_discrepancy = v_dose.inventory_discrepancy OR v_discrepancy,
        updated_at = p_now
    WHERE id = v_dose.id;

    UPDATE carecircle.notification_log
    SET status = 'cancelled', updated_at = p_now
    WHERE resource_id = v_dose.id
      AND resource_type = 'dose'
      AND event_type LIKE 'dose_escalation_%'
      AND status IN ('pending', 'retryable');

    INSERT INTO carecircle.dose_events (circle_id, dose_id, event_type, actor_account_id, previous_state, new_state, stock_delta, occurred_at)
    VALUES (v_dose.circle_id, v_dose.id, 'confirmed_taken', (v_actor->>'account_id')::UUID, v_dose.state, 'taken', v_stock_delta, p_now);

    INSERT INTO carecircle.audit_events (circle_id, actor_account_id, actor_membership_id, operation, target_type, target_id, metadata, occurred_at)
    VALUES (v_dose.circle_id, (v_actor->>'account_id')::UUID, (v_actor->>'membership_id')::UUID, 'dose_taken', 'dose', v_dose.id::TEXT, jsonb_build_object('discrepancy', v_discrepancy), p_now);

    RETURN jsonb_build_object('status', 'ok', 'confirmed_at', p_now, 'discrepancy', v_discrepancy);
END;
$$;

CREATE OR REPLACE FUNCTION public.cc_claim_outbox(
    p_worker_id TEXT,
    p_now TIMESTAMPTZ,
    p_lease_seconds INTEGER,
    p_batch_size INTEGER
)
RETURNS SETOF carecircle.notification_log
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = carecircle, pg_temp
AS $$
BEGIN
    -- Mark expired sending leases as uncertain (never blind-retry after potential send)
    UPDATE carecircle.notification_log
    SET status = 'uncertain',
        last_error_code = 'lease_expired_during_send',
        updated_at = p_now
    WHERE status = 'sending'
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at <= p_now;

    -- Re-queue expired pre-send leases as retryable
    UPDATE carecircle.notification_log
    SET status = 'retryable',
        lease_owner = NULL,
        lease_expires_at = NULL,
        updated_at = p_now
    WHERE status = 'leased'
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at <= p_now;

    RETURN QUERY
    WITH claimable AS (
        SELECT nl.id
        FROM carecircle.notification_log nl
        WHERE nl.status IN ('pending', 'retryable')
          AND nl.next_attempt_at <= p_now
        ORDER BY nl.next_attempt_at ASC
        LIMIT p_batch_size
        FOR UPDATE SKIP LOCKED
    )
    UPDATE carecircle.notification_log nl
    SET status = 'leased',
        lease_owner = p_worker_id,
        lease_expires_at = p_now + make_interval(secs => p_lease_seconds),
        updated_at = p_now
    FROM claimable c
    WHERE nl.id = c.id
    RETURNING nl.*;
END;
$$;

CREATE OR REPLACE FUNCTION public.cc_record_acknowledgement(
    p_token_hash TEXT,
    p_episode_type TEXT,
    p_resource_id UUID,
    p_now TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = carecircle, pg_temp
AS $$
DECLARE
    v_actor JSONB;
    v_circle_id UUID;
    v_mem_id UUID;
    v_inserted UUID;
BEGIN
    v_actor := public.cc_resolve_session(p_token_hash, p_now, 30);
    IF v_actor IS NULL THEN
        RETURN jsonb_build_object('status', 'unauthorized');
    END IF;

    IF NOT ((v_actor->>'role') = 'elder' OR (v_actor->>'role') = 'caretaker' OR (v_actor->>'is_care_contact')::BOOLEAN = TRUE) THEN
        RETURN jsonb_build_object('status', 'forbidden');
    END IF;

    v_circle_id := (v_actor->>'circle_id')::UUID;
    v_mem_id := (v_actor->>'membership_id')::UUID;

    INSERT INTO carecircle.care_acknowledgements (circle_id, episode_type, resource_id, acknowledged_by_membership_id, acknowledged_by_name, acknowledged_at)
    VALUES (v_circle_id, p_episode_type, p_resource_id, v_mem_id, v_actor->>'display_name', p_now)
    ON CONFLICT (episode_type, resource_id) DO NOTHING
    RETURNING id INTO v_inserted;

    IF v_inserted IS NULL THEN
        RETURN jsonb_build_object('status', 'already_acknowledged');
    END IF;

    UPDATE carecircle.notification_log
    SET status = 'cancelled', updated_at = p_now
    WHERE circle_id = v_circle_id
      AND resource_id = p_resource_id
      AND event_type LIKE '%_escalation_%'
      AND status IN ('pending', 'retryable');

    INSERT INTO carecircle.audit_events (circle_id, actor_account_id, actor_membership_id, operation, target_type, target_id, metadata, occurred_at)
    VALUES (v_circle_id, (v_actor->>'account_id')::UUID, v_mem_id, 'care_acknowledged', p_episode_type, p_resource_id::TEXT, '{}'::JSONB, p_now);

    RETURN jsonb_build_object('status', 'ok', 'ack_id', v_inserted);
END;
$$;

CREATE OR REPLACE FUNCTION public.cc_delete_circle(
    p_token_hash TEXT,
    p_now TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = carecircle, pg_temp
AS $$
DECLARE
    v_actor JSONB;
    v_circle_id UUID;
BEGIN
    v_actor := public.cc_resolve_session(p_token_hash, p_now, 30);
    IF v_actor IS NULL THEN
        RETURN jsonb_build_object('status', 'unauthorized');
    END IF;
    IF (v_actor->>'role') <> 'elder' THEN
        RETURN jsonb_build_object('status', 'forbidden');
    END IF;

    v_circle_id := (v_actor->>'circle_id')::UUID;

    PERFORM set_config('carecircle.allow_circle_purge', 'on', true);

    UPDATE carecircle.care_circles
    SET primary_contact_membership_id = NULL,
        secondary_contact_membership_id = NULL,
        checkin_fallback_membership_id = NULL
    WHERE id = v_circle_id;

    DELETE FROM carecircle.sessions WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.action_tokens WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.notification_log WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.care_acknowledgements WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.care_notes WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.doctor_briefs WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.checkin_instances WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.appointments WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.dose_events WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.dose_instances WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.medication_schedules WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.medications WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.invitations WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.member_consents WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.memberships WHERE circle_id = v_circle_id;
    DELETE FROM carecircle.audit_events WHERE circle_id = v_circle_id;

    UPDATE carecircle.care_circles SET deleted_at = p_now WHERE id = v_circle_id;

    INSERT INTO carecircle.audit_events (circle_id, actor_account_id, actor_membership_id, operation, target_type, target_id, metadata, occurred_at)
    VALUES (v_circle_id, (v_actor->>'account_id')::UUID, (v_actor->>'membership_id')::UUID, 'circle_deleted_purged', 'circle', v_circle_id::TEXT, '{}'::JSONB, p_now);

    RETURN jsonb_build_object('status', 'ok');
END;
$$;

-- Strictly revoke public/anon/authenticated RPC execution; allow only service_role
REVOKE ALL ON FUNCTION public.cc_resolve_session(TEXT, TIMESTAMPTZ, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cc_resolve_session(TEXT, TIMESTAMPTZ, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.cc_record_login_attempt(TEXT, BOOLEAN, TIMESTAMPTZ, INTEGER, INTEGER, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cc_record_login_attempt(TEXT, BOOLEAN, TIMESTAMPTZ, INTEGER, INTEGER, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.cc_redeem_verification(TEXT, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cc_redeem_verification(TEXT, TIMESTAMPTZ) TO service_role;

REVOKE ALL ON FUNCTION public.cc_confirm_dose_taken(TEXT, UUID, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cc_confirm_dose_taken(TEXT, UUID, TIMESTAMPTZ) TO service_role;

REVOKE ALL ON FUNCTION public.cc_claim_outbox(TEXT, TIMESTAMPTZ, INTEGER, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cc_claim_outbox(TEXT, TIMESTAMPTZ, INTEGER, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.cc_record_acknowledgement(TEXT, TEXT, UUID, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cc_record_acknowledgement(TEXT, TEXT, UUID, TIMESTAMPTZ) TO service_role;

REVOKE ALL ON FUNCTION public.cc_delete_circle(TEXT, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cc_delete_circle(TEXT, TIMESTAMPTZ) TO service_role;
