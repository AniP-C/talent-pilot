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


def test_email_finds_the_application_under_a_suffixed_company_name(jobs_db):
    """"GN" applied to, "GN Group" assessing: one application, not two.

    Straight from a real sync log — the confirmation named the employer one
    way and the assessment invitation the other, so the second email opened a
    second row and the first stayed at APPLIED.
    """
    job_id = db.add_job("GN", "AI Developer", status="APPLIED", db_path=jobs_db)

    outcome = db.update_job_from_email(
        "GN Group", "ASSESSMENT", "Your assessment", "Online test link",
        db_path=jobs_db,
    )

    assert outcome == "updated"
    assert len(db.get_all_jobs(db_path=jobs_db)) == 1
    assert db.get_job(job_id, db_path=jobs_db)["status"] == "ASSESSMENT"


def test_suffix_match_does_not_override_an_exact_company(jobs_db):
    """An exact spelling keeps its own mail, near-miss or not."""
    loose = db.add_job("Orion", "Engineer", status="APPLIED", db_path=jobs_db)
    exact = db.add_job("Orion Labs", "Engineer", status="APPLIED", db_path=jobs_db)

    db.update_job_from_email(
        "Orion Labs", "INTERVIEW", "Interview", "", db_path=jobs_db
    )

    assert db.get_job(exact, db_path=jobs_db)["status"] == "INTERVIEW"
    assert db.get_job(loose, db_path=jobs_db)["status"] == "APPLIED"


def test_unrelated_companies_are_not_merged_by_the_suffix_match(jobs_db):
    db.add_job("Onix", "AI Engineer", status="APPLIED", db_path=jobs_db)

    outcome = db.update_job_from_email(
        "Onyx Systems", "REJECTED", "No thanks", "", db_path=jobs_db
    )

    assert outcome == "created"
    assert len(db.get_all_jobs(db_path=jobs_db)) == 2


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


# =====================================================================
# SOURCE EMAIL LINK (v7)
# =====================================================================
def test_email_id_is_recorded_on_a_created_row(jobs_db):
    db.update_job_from_email(
        "NewCo", "ASSESSMENT", "Take-home", "Sent an assessment",
        message_id="18f2c0abc", db_path=jobs_db,
    )

    assert db.get_all_jobs(db_path=jobs_db)[0]["last_email_id"] == "18f2c0abc"


def test_email_id_is_recorded_on_an_updated_row(jobs_db):
    job_id = db.add_job("Acme", "Engineer", db_path=jobs_db)

    db.update_job_from_email(
        "Acme", "INTERVIEW", "Interview invite", "Recruiter proposed times",
        message_id="18f2c0def", db_path=jobs_db,
    )

    assert db.get_job(job_id, db_path=jobs_db)["last_email_id"] == "18f2c0def"


def test_newest_email_id_replaces_the_previous_one(jobs_db):
    """The point of the field is "what happened most recently".

    A stale id would open the confirmation receipt instead of the assessment
    request that is the reason to look, so newest must win — the opposite of
    the rule `link` follows.
    """
    job_id = db.add_job("Acme", "Engineer", db_path=jobs_db)

    db.update_job_from_email(
        "Acme", "ASSESSMENT", "Take-home", "Assessment sent",
        message_id="older", db_path=jobs_db,
    )
    db.update_job_from_email(
        "Acme", "INTERVIEW", "Interview invite", "Times proposed",
        message_id="newer", db_path=jobs_db,
    )

    assert db.get_job(job_id, db_path=jobs_db)["last_email_id"] == "newer"


def test_an_email_without_an_id_keeps_the_stored_one(jobs_db):
    """A manual call must not blank a link the sync had already established."""
    job_id = db.add_job("Acme", "Engineer", db_path=jobs_db)

    db.update_job_from_email(
        "Acme", "ASSESSMENT", "Take-home", "Assessment sent",
        message_id="kept", db_path=jobs_db,
    )
    db.update_job_from_email(
        "Acme", "INTERVIEW", "Interview invite", "Times proposed",
        db_path=jobs_db,
    )

    assert db.get_job(job_id, db_path=jobs_db)["last_email_id"] == "kept"


def test_history_rows_cite_the_email_they_came_from(jobs_db):
    db.add_job("Acme", "Engineer", db_path=jobs_db)

    db.update_job_from_email(
        "Acme", "INTERVIEW", "Interview invite", "Times proposed",
        message_id="18f2c0aaa", db_path=jobs_db,
    )

    with db.connect(jobs_db) as conn:
        cited = conn.execute(
            "SELECT message_id FROM status_history WHERE source = 'Email Sync'"
        ).fetchone()

    assert cited["message_id"] == "18f2c0aaa"


def test_a_manual_history_row_cites_no_email(jobs_db):
    """NULL, not "", so "no email" stays distinct from "id not captured"."""
    db.add_job("Acme", "Engineer", db_path=jobs_db)

    with db.connect(jobs_db) as conn:
        seeded = conn.execute(
            "SELECT message_id FROM status_history"
        ).fetchone()

    assert seeded["message_id"] is None


def test_v6_database_gains_the_new_columns(jobs_db):
    """A workspace created before v7 must migrate rather than break.

    Mirrors what is on the live instance: the tables exist with data in them,
    and only the version stamp says the new columns are missing.
    """
    db.add_job("Acme", "Engineer", db_path=jobs_db)

    with db.connect(jobs_db) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN last_email_id")
        conn.execute("ALTER TABLE status_history DROP COLUMN message_id")
        conn.execute("PRAGMA user_version = 6")

    db.create_table(jobs_db)

    with db.connect(jobs_db) as conn:
        jobs_columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        history_columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(status_history)")
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert "last_email_id" in jobs_columns
    assert "message_id" in history_columns
    assert version == db.SCHEMA_VERSION
    # The existing row survived the migration.
    assert db.get_all_jobs(db_path=jobs_db)[0]["company"] == "Acme"
