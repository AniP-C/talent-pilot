"""Job storage: dedupe, status validation, dict rows, and email dedupe."""

import pytest

import db


def test_add_job_returns_id_and_persists(jobs_db):
    job_id = db.add_job("Acme", "Backend Engineer", db_path=jobs_db)

    stored = db.get_job(job_id, db_path=jobs_db)
    assert stored["company"] == "Acme"
    assert stored["role"] == "Backend Engineer"
    assert stored["status"] == "APPLIED"


def test_rows_are_dicts_not_tuples(jobs_db):
    """Callers must never depend on column order, which migrations change."""
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)

    job = db.get_all_jobs(db_path=jobs_db)[0]

    assert isinstance(job, dict)
    assert set(db.JOB_COLUMNS) <= set(job)


def test_duplicate_company_and_role_is_rejected(jobs_db):
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)

    with pytest.raises(db.DuplicateJobError):
        db.add_job("Acme", "Backend Engineer", db_path=jobs_db)


def test_duplicate_check_ignores_case_and_whitespace(jobs_db):
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)

    with pytest.raises(db.DuplicateJobError):
        db.add_job("  ACME  ", "backend engineer", db_path=jobs_db)


def test_duplicate_is_independent_of_date(jobs_db):
    """The old code deduped on date too, so re-saving on a new day slipped through."""
    db.add_job("Acme", "Backend Engineer", date_applied="2026-01-01", db_path=jobs_db)

    with pytest.raises(db.DuplicateJobError):
        db.add_job("Acme", "Backend Engineer", date_applied="2026-06-01", db_path=jobs_db)


@pytest.mark.parametrize("company,role", [("", "Engineer"), ("Acme", ""), ("  ", "  ")])
def test_add_job_requires_company_and_role(jobs_db, company, role):
    with pytest.raises(ValueError):
        db.add_job(company, role, db_path=jobs_db)


def test_add_job_rejects_unknown_status(jobs_db):
    with pytest.raises(ValueError, match="Unknown status"):
        db.add_job("Acme", "Engineer", status="Interviewing", db_path=jobs_db)


def test_status_is_normalized_to_uppercase(jobs_db):
    job_id = db.add_job("Acme", "Engineer", status="interview", db_path=jobs_db)

    assert db.get_job(job_id, db_path=jobs_db)["status"] == "INTERVIEW"


def test_update_status(jobs_db):
    job_id = db.add_job("Acme", "Engineer", db_path=jobs_db)

    assert db.update_status(job_id, "OFFER", db_path=jobs_db) is True
    assert db.get_job(job_id, db_path=jobs_db)["status"] == "OFFER"


def test_update_status_reports_missing_job(jobs_db):
    assert db.update_status(9999, "OFFER", db_path=jobs_db) is False


def test_delete_job(jobs_db):
    job_id = db.add_job("Acme", "Engineer", db_path=jobs_db)

    assert db.delete_job(job_id, db_path=jobs_db) is True
    assert db.get_job(job_id, db_path=jobs_db) is None


def test_check_if_applied(jobs_db):
    db.add_job("Acme", "Engineer", status="INTERVIEW", db_path=jobs_db)

    assert db.check_if_applied("acme", "ENGINEER", db_path=jobs_db) == (True, "INTERVIEW")
    assert db.check_if_applied("Other", "Engineer", db_path=jobs_db) == (False, None)


def test_check_if_applied_on_missing_database(tmp_path):
    """Called before a workspace exists, this must answer rather than raise."""
    assert db.check_if_applied("Acme", "Engineer", db_path=tmp_path / "nope.db") == (
        False,
        None,
    )


# =====================================================================
# EMAIL PIPELINE
# =====================================================================
def test_update_job_from_email_updates_existing_company(jobs_db):
    job_id = db.add_job("Acme", "Engineer", db_path=jobs_db)

    outcome = db.update_job_from_email(
        "acme", "INTERVIEW", "Interview invite", "Recruiter proposed times",
        db_path=jobs_db,
    )

    job = db.get_job(job_id, db_path=jobs_db)
    assert outcome == "updated"
    assert job["status"] == "INTERVIEW"
    assert "Interview invite" in job["notes"]


def test_update_job_from_email_creates_missing_company(jobs_db):
    outcome = db.update_job_from_email(
        "NewCo", "ASSESSMENT", "Take-home", "Sent an assessment", db_path=jobs_db
    )

    jobs = db.get_all_jobs(db_path=jobs_db)
    assert outcome == "created"
    assert jobs[0]["company"] == "NewCo"
    assert jobs[0]["source"] == "Email Sync"


def test_update_job_from_email_rejects_unknown_status(jobs_db):
    with pytest.raises(ValueError):
        db.update_job_from_email("Acme", "UNKNOWN", "s", "r", db_path=jobs_db)


def test_processed_emails_are_remembered(jobs_db):
    assert db.is_email_processed("msg-1", db_path=jobs_db) is False

    db.mark_email_processed("msg-1", db_path=jobs_db)

    assert db.is_email_processed("msg-1", db_path=jobs_db) is True
    # Marking twice must not raise, so a retried sync is safe.
    db.mark_email_processed("msg-1", db_path=jobs_db)


# =====================================================================
# STATS
# =====================================================================
def test_get_stats(jobs_db):
    db.add_job("A", "Engineer", status="APPLIED", db_path=jobs_db)
    db.add_job("B", "Engineer", status="INTERVIEW", db_path=jobs_db)
    db.add_job("C", "Engineer", status="OFFER", db_path=jobs_db)
    db.add_job("D", "Engineer", status="REJECTED", db_path=jobs_db)

    stats = db.get_stats(db_path=jobs_db)

    assert stats["total"] == 4
    assert stats["interviews"] == 1
    assert stats["offers"] == 1
    assert stats["active"] == 2  # APPLIED + INTERVIEW
    assert stats["response_rate"] == 75


def test_get_stats_on_empty_workspace(jobs_db):
    stats = db.get_stats(db_path=jobs_db)

    assert stats["total"] == 0
    assert stats["response_rate"] == 0


# =====================================================================
# SCHEMA
# =====================================================================
def test_create_table_is_idempotent(jobs_db):
    db.create_table(jobs_db)
    db.create_table(jobs_db)

    assert db.get_all_jobs(db_path=jobs_db) == []


def test_schema_version_is_stamped(jobs_db):
    with db.connect(jobs_db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version == db.SCHEMA_VERSION


def test_newer_schema_is_refused(jobs_db):
    """Opening a database from a future version must fail loudly, not silently."""
    with db.connect(jobs_db) as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer version"):
        db.create_table(jobs_db)
