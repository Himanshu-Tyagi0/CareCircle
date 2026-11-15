-- Ordered Migration 002: Additional partial indexes for worker active windows & briefs
CREATE INDEX IF NOT EXISTS idx_checkins_unresponded_window
    ON carecircle.checkin_instances (window_ends_at, expires_at)
    WHERE responded_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_appointments_active_upcoming
    ON carecircle.appointments (starts_at)
    WHERE cancelled_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_outbox_uncertain
    ON carecircle.notification_log (circle_id, updated_at DESC)
    WHERE status = 'uncertain';
