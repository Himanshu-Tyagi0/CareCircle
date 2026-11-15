"""Real PostgreSQL 15 SQL integration tests verifying roles, RPCs, constraints, concurrency, and plans."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time as pytime
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

PG_BIN = Path("/opt/postgres/usr/lib/postgresql/15/bin")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    """Use TEST_DATABASE_URL if set, or boot an isolated ephemeral PostgreSQL 15 instance."""
    env_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if env_url:
        dsn = env_url
        _apply_sql_files(dsn)
        yield dsn
        return

    if not (PG_BIN / "initdb").exists():
        pytest.skip("PostgreSQL 15 binaries not found at /opt/postgres and TEST_DATABASE_URL unset")

    data_dir = Path(tempfile.mkdtemp(prefix="cc_pgdata_", dir="/tmp"))
    sock_dir = Path(tempfile.mkdtemp(prefix="cc_pgsock_", dir="/tmp"))
    os.chmod(data_dir, 0o700)
    os.chmod(sock_dir, 0o700)
    shutil.chown(data_dir, user="nobody", group="nogroup")
    shutil.chown(sock_dir, user="nobody", group="nogroup")
    port = _free_port()
    log_file = data_dir / "postgres.log"
    pg_env = os.environ.copy()
    pg_env["LD_LIBRARY_PATH"] = (
        "/opt/postgres/usr/lib/x86_64-linux-gnu:/opt/postgres/lib/x86_64-linux-gnu"
    )

    try:
        subprocess.run(
            [
                str(PG_BIN / "initdb"),
                "-D",
                str(data_dir),
                "-U",
                "postgres",
                "-A",
                "trust",
                "--encoding=UTF8",
            ],
            check=True,
            capture_output=True,
            env=pg_env,
            user="nobody",
        )
        subprocess.run(
            [
                str(PG_BIN / "pg_ctl"),
                "-D",
                str(data_dir),
                "-l",
                str(log_file),
                "-o",
                f"-p {port} -k {sock_dir} -h 127.0.0.1 -c jit=off",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            env=pg_env,
            user="nobody",
        )
        dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        for _ in range(20):
            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                pytime.sleep(0.2)
        _apply_sql_files(dsn)
        yield dsn
    finally:
        subprocess.run(
            [str(PG_BIN / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=False,
            capture_output=True,
            env=pg_env,
            user="nobody",
        )
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(sock_dir, ignore_errors=True)


def _apply_sql_files(dsn: str) -> None:
    root = Path(__file__).resolve().parent.parent
    with psycopg.connect(dsn, autocommit=True) as conn:
        for rel in (
            "db/dev_roles.sql",
            "db/schema.sql",
            "db/rpcs.sql",
            "db/migrations/002_worker_and_brief_indexes.sql",
        ):
            sql = (root / rel).read_text(encoding="utf-8")
            conn.execute(sql)


@pytest.mark.postgres
def test_postgres_role_isolation_constraints_concurrency_and_plans(pg_dsn: str) -> None:
    """Verify anon/authenticated denied, composite FKs, atomic RPCs, SKIP LOCKED, and audit immutability."""
    now = datetime(2026, 5, 10, 8, 0, tzinfo=UTC)

    with psycopg.connect(pg_dsn) as conn:
        # 1. anon and authenticated cannot access private tables or SECURITY DEFINER RPCs
        for role_name in ("anon", "authenticated"):
            with conn.transaction():
                conn.execute(f"SET LOCAL ROLE {role_name}")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("SELECT * FROM carecircle.accounts")
            with conn.transaction():
                conn.execute(f"SET LOCAL ROLE {role_name}")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        "SELECT public.cc_resolve_session(%s, %s, %s)",
                        ("deadbeef", now, 30),
                    )

        # 2. Seed two circles via verification RPC as service_role
        with conn.transaction():
            conn.execute("SET LOCAL ROLE service_role")
            t1 = "tok-hash-1"
            t2 = "tok-hash-2"
            conn.execute(
                """
                INSERT INTO carecircle.pending_verifications
                  (email_normalized, display_name, passcode_hash, timezone, language, token_hash, expires_at)
                VALUES
                  ('e1@pg.test', 'Elder 1', 'hash1', 'UTC', 'en', %s, %s),
                  ('e2@pg.test', 'Elder 2', 'hash2', 'UTC', 'en', %s, %s)
                """,
                (t1, now + timedelta(hours=1), t2, now + timedelta(hours=1)),
            )
            r1 = conn.execute("SELECT public.cc_redeem_verification(%s, %s)", (t1, now)).fetchone()[
                0
            ]
            r2 = conn.execute("SELECT public.cc_redeem_verification(%s, %s)", (t2, now)).fetchone()[
                0
            ]
            assert r1["status"] == "ok"
            assert r2["status"] == "ok"
            c1_id = uuid.UUID(r1["circle_id"])
            a1_id = uuid.UUID(r1["account_id"])
            m1_id = uuid.UUID(r1["membership_id"])
            m2_id = uuid.UUID(r2["membership_id"])

            # 3. Composite FK prevents assigning circle 2's membership as circle 1's primary contact
            with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                conn.execute(
                    "UPDATE carecircle.care_circles SET primary_contact_membership_id = %s WHERE id = %s",
                    (m2_id, c1_id),
                )

            # Create session for elder 1, med, and dose with stock=1
            sess_hash = "sess-hash-pg-1"
            conn.execute(
                """
                INSERT INTO carecircle.sessions
                  (account_id, membership_id, circle_id, auth_revision, token_hash, last_seen_at, idle_expires_at, absolute_expires_at)
                VALUES (%s, %s, %s, 1, %s, %s, %s, %s)
                """,
                (
                    a1_id,
                    m1_id,
                    c1_id,
                    sess_hash,
                    now,
                    now + timedelta(minutes=30),
                    now + timedelta(hours=12),
                ),
            )
            med_id = conn.execute(
                """
                INSERT INTO carecircle.medications
                  (circle_id, name, dose_text, start_date, stock_count, stock_units_per_dose)
                VALUES (%s, 'MedPG', '5mg', %s, 1, 1)
                RETURNING id
                """,
                (c1_id, now.date()),
            ).fetchone()[0]
            sched_id = conn.execute(
                """
                INSERT INTO carecircle.medication_schedules
                  (circle_id, medication_id, version, times_local, weekdays, start_date, timezone)
                VALUES (%s, %s, 1, ARRAY['08:00'::TIME], ARRAY[0,1,2,3,4,5,6], %s, 'UTC')
                RETURNING id
                """,
                (c1_id, med_id, now.date()),
            ).fetchone()[0]
            dose_id = conn.execute(
                """
                INSERT INTO carecircle.dose_instances
                  (circle_id, medication_id, schedule_id, schedule_version, medication_name, dose_text,
                   criticality, due_at, local_date, local_time_intended, expires_at)
                VALUES (%s, %s, %s, 1, 'MedPG', '5mg', 'normal', %s, %s, '08:00'::TIME, %s)
                RETURNING id
                """,
                (c1_id, med_id, sched_id, now, now.date(), now + timedelta(hours=12)),
            ).fetchone()[0]

            # Queue 4 outbox items (for another resource) for SKIP LOCKED concurrency test
            other_res_id = uuid.uuid4()
            for i in range(4):
                conn.execute(
                    """
                    INSERT INTO carecircle.notification_log
                      (circle_id, logical_key, event_type, resource_type, resource_id, escalation_level,
                       recipient_membership_id, channel, status, next_attempt_at)
                    VALUES (%s, %s, 'dose_escalation_lvl1', 'dose', %s, 1, %s, 'email', 'pending', %s)
                    """,
                    (c1_id, f"pg-out-{i}", other_res_id, m1_id, now),
                )

    # 4. Concurrent atomic login lockout via cc_record_login_attempt
    def attempt_login(_: int) -> str:
        with psycopg.connect(pg_dsn) as c, c.transaction():
            c.execute("SET LOCAL ROLE service_role")
            res = c.execute(
                "SELECT public.cc_record_login_attempt(%s, %s, %s, %s, %s, %s)",
                ("e1@pg.test", False, now, 5, 15, 100),
            ).fetchone()[0]
            return str(res["status"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        login_res = list(pool.map(attempt_login, range(7)))
    assert login_res.count("failed") == 4
    assert login_res.count("locked") == 3

    # 5. Concurrent dose confirmation via cc_confirm_dose_taken (only 1 decrements stock)
    def confirm_dose(_: int) -> str:
        with psycopg.connect(pg_dsn) as c, c.transaction():
            c.execute("SET LOCAL ROLE service_role")
            res = c.execute(
                "SELECT public.cc_confirm_dose_taken(%s, %s, %s)",
                ("sess-hash-pg-1", dose_id, now),
            ).fetchone()[0]
            return str(res["status"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        dose_res = list(pool.map(confirm_dose, range(4)))
    assert dose_res.count("ok") == 1
    assert dose_res.count("already_taken") == 3

    # 6. Concurrent SKIP LOCKED claims return disjoint outbox rows
    def claim_worker(wid: str) -> list[str]:
        with psycopg.connect(pg_dsn) as c, c.transaction():
            c.execute("SET LOCAL ROLE service_role")
            rows = c.execute(
                "SELECT id FROM public.cc_claim_outbox(%s, %s, %s, %s)",
                (wid, now, 60, 2),
            ).fetchall()
            return [str(r[0]) for r in rows]

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim_worker, ["w1", "w2"]))
    assert len(claims[0]) == 2
    assert len(claims[1]) == 2
    assert set(claims[0]).isdisjoint(set(claims[1]))

    # 7. Audit immutability + cc_delete_circle + EXPLAIN index check
    with psycopg.connect(pg_dsn) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE service_role")
        stock = conn.execute(
            "SELECT stock_count FROM carecircle.medications WHERE id = %s", (med_id,)
        ).fetchone()[0]
        assert stock == 0

        # Audit log cannot be mutated even by service_role
        with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
            conn.execute("UPDATE carecircle.audit_events SET operation = 'tamper'")

        # EXPLAIN query plan for indexed active window
        conn.execute("SET LOCAL enable_seqscan = off")
        plan_rows = conn.execute(
            """
            EXPLAIN (FORMAT TEXT)
            SELECT id FROM carecircle.dose_instances
            WHERE state IN ('pending', 'snoozed', 'can''t_take') AND due_at <= %s AND expires_at > %s
            """,
            (now, now),
        ).fetchall()
        plan_txt = "\n".join(r[0] for r in plan_rows)
        assert "Index" in plan_txt

        # Delete/purge circle works and writes final audit event
        del_res = conn.execute(
            "SELECT public.cc_delete_circle(%s, %s)", ("sess-hash-pg-1", now)
        ).fetchone()[0]
        assert del_res["status"] == "ok"
