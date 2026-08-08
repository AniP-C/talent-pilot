"""Follow-up detection and recruiter contact capture.

Two questions the tracker could not answer about its own data: which
applications have gone quiet, and who to reply to. Both are derived rather
than entered, so what is worth testing is the derivation — especially that
"quiet" is measured from the last real signal and not from the date applied.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import db


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _set_activity(jobs_db, job_id: int, *, applied: str, contact: str = None) -> None:
    """Backdate a job's timestamps, including the history seeded by add_job."""
    with db.connect(jobs_db) as conn:
        conn.execute(
            "UPDATE jobs SET date_applied = ?, created_at = ?, updated_at = ?, "
            "last_contact_at = ? WHERE id = ?",
            (applied, applied, applied, contact, job_id),
        )
        conn.execute(
            "UPDATE status_history SET occurred_at = ? WHERE job_id = ?",
            (applied, job_id),
        )


# =====================================================================
# WHICH APPLICATIONS ARE QUIET
# =====================================================================
def test_a_recent_application_is_not_a_followup(jobs_db):
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    _set_activity(jobs_db, job_id, applied=_days_ago(2))

    assert db.get_followups(db_path=jobs_db, quiet_after_days=10) == []


def test_a_long_silent_application_is_reported(jobs_db):
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    _set_activity(jobs_db, job_id, applied=_days_ago(40))

    followups = db.get_followups(db_path=jobs_db, quiet_after_days=10)

    assert [job["company"] for job in followups] == ["Acme"]
    assert followups[0]["days_quiet"] >= 40


def test_recent_contact_resets_the_clock(jobs_db):
    """An old application that replied yesterday is not stale.

    Measuring from date_applied alone would report every long-running process
    as neglected, which is precisely the noise that makes such a list ignored.
    """
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    _set_activity(jobs_db, job_id, applied=_days_ago(90), contact=_days_ago(1))

    assert db.get_followups(db_path=jobs_db, quiet_after_days=10) == []


def test_a_recent_stage_change_also_resets_the_clock(jobs_db):
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    _set_activity(jobs_db, job_id, applied=_days_ago(90))

    # Moving the application now writes a fresh status_history row.
    db.update_status(job_id, "INTERVIEW", db_path=jobs_db)

    assert db.get_followups(db_path=jobs_db, quiet_after_days=10) == []


def test_terminal_statuses_are_never_followups(jobs_db):
    """An offer or a rejection is not waiting on anybody."""
    for role, status in (("Offered Role", "OFFER"), ("Closed Role", "REJECTED")):
        job_id = db.add_job("Acme", role, db_path=jobs_db)
        _set_activity(jobs_db, job_id, applied=_days_ago(120))
        db.update_status(job_id, status, db_path=jobs_db)
        with db.connect(jobs_db) as conn:
            conn.execute(
                "UPDATE status_history SET occurred_at = ? WHERE job_id = ?",
                (_days_ago(120), job_id),
            )

    assert db.get_followups(db_path=jobs_db, quiet_after_days=10) == []


def test_longest_silence_comes_first(jobs_db):
    for role, days in (("Newer", 20), ("Oldest", 100), ("Middle", 50)):
        job_id = db.add_job("Acme", role, db_path=jobs_db)
        _set_activity(jobs_db, job_id, applied=_days_ago(days))

    ordered = [job["role"] for job in db.get_followups(db_path=jobs_db, quiet_after_days=10)]

    assert ordered == ["Oldest", "Middle", "Newer"]


def test_the_threshold_is_respected(jobs_db):
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    _set_activity(jobs_db, job_id, applied=_days_ago(15))

    assert db.get_followups(db_path=jobs_db, quiet_after_days=30) == []
    assert len(db.get_followups(db_path=jobs_db, quiet_after_days=10)) == 1


def test_an_undatable_row_is_surfaced_not_hidden(jobs_db):
    """A job with no usable timestamp is exactly the kind that gets forgotten."""
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    with db.connect(jobs_db) as conn:
        conn.execute(
            "UPDATE jobs SET date_applied = NULL, created_at = '', "
            "last_contact_at = NULL WHERE id = ?",
            (job_id,),
        )
        conn.execute("DELETE FROM status_history WHERE job_id = ?", (job_id,))

    followups = db.get_followups(db_path=jobs_db, quiet_after_days=10)

    assert len(followups) == 1
    assert followups[0]["days_quiet"] is None


# =====================================================================
# WHO TO REPLY TO
# =====================================================================
@pytest.mark.parametrize(
    "address",
    [
        "no-reply@acme.com",
        "noreply@acme.com",
        "donotreply@acme.com",
        "do-not-reply@greenhouse.io",
        "notifications@acme.com",
        "mailer@acme.com",
        "",
        "not-an-address",
    ],
)
def test_send_only_addresses_are_not_contacts(address):
    assert db.is_replyable(address) is False


@pytest.mark.parametrize(
    "address", ["jane@acme.com", "jane.doe@acme.co.uk", "recruiting@acme.com"]
)
def test_a_person_is_a_contact(address):
    assert db.is_replyable(address) is True


def test_the_sender_becomes_the_contact(jobs_db):
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)

    db.update_job_from_email(
        company_name="Acme",
        category="INTERVIEW",
        subject="Interview invitation",
        reasoning="Invited to interview",
        role="Backend Engineer",
        contact_name="Jane Doe",
        contact_email="jane@acme.com",
        db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)
    assert job["contact_name"] == "Jane Doe"
    assert job["contact_email"] == "jane@acme.com"
    assert job["last_contact_at"]


def test_a_noreply_sender_does_not_overwrite_a_person(jobs_db):
    """A human contact is worth more than the robot that mailed last."""
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)

    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Invitation",
        reasoning="", role="Backend Engineer",
        contact_name="Jane Doe", contact_email="jane@acme.com", db_path=jobs_db,
    )
    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Reminder",
        reasoning="", role="Backend Engineer",
        contact_name="Acme Recruiting", contact_email="no-reply@acme.com",
        db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)
    assert job["contact_email"] == "jane@acme.com"


def test_a_noreply_email_still_counts_as_contact(jobs_db):
    """An automated acknowledgement is still the employer making contact,
    which is what silence should be measured against."""
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    _set_activity(jobs_db, job_id, applied=_days_ago(90))

    db.update_job_from_email(
        company_name="Acme", category="APPLIED", subject="We received it",
        reasoning="", role="Backend Engineer",
        contact_name="", contact_email="no-reply@acme.com", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)
    assert job["contact_email"] is None
    assert job["last_contact_at"]
    assert db.get_followups(db_path=jobs_db, quiet_after_days=10) == []


def test_an_email_creating_a_job_records_the_contact(jobs_db):
    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Invitation",
        reasoning="", role="Backend Engineer",
        contact_name="Jane Doe", contact_email="jane@acme.com", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)
    assert job["contact_email"] == "jane@acme.com"


# =====================================================================
# MIGRATION
# =====================================================================
def test_a_v2_database_gains_the_contact_columns(tmp_path):
    """An existing workspace must migrate rather than fail on a missing column."""
    path = tmp_path / "old.db"

    # A v2 jobs table: everything before the contact columns existed.
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL, role TEXT NOT NULL, jd TEXT,
                status TEXT NOT NULL DEFAULT 'APPLIED', date_applied TEXT,
                link TEXT, notes TEXT, source TEXT NOT NULL DEFAULT 'Manual',
                resume_used TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO jobs (company, role, status, created_at, updated_at)
            VALUES ('Acme', 'Backend Engineer', 'APPLIED', '2026-01-01', '2026-01-01');
            PRAGMA user_version = 2;
            """
        )

    db.create_table(path)

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=path)
    assert job["contact_email"] is None
    assert set(db.JOB_COLUMNS) <= set(job)

    with db.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_get_job_by_identity_misses_cleanly(jobs_db):
    assert db.get_job_by_identity("Nobody", "Nothing", db_path=jobs_db) is None
