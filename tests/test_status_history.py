"""Stage transitions, out-of-order email replay, and company+role matching.

Three bugs are pinned here, all found by replaying real recruiter mail:

1. Gmail lists newest first, so the *oldest* email was applied last and won.
   An offer would silently revert to "Applied".
2. update_job_from_email matched on company alone with LIMIT 1, so a
   rejection for one role flipped a different role at the same company.
3. There was no record of how an application reached its current status.
"""

import sqlite3

import pytest

import db


# =====================================================================
# ORDERING: an application must not move backwards
# =====================================================================
def test_an_offer_survives_a_later_confirmation_email(jobs_db):
    """The exact failure: Gmail returns newest first, so the "we received
    your application" confirmation is processed last and used to win."""
    db.update_job_from_email("Acme", "OFFER", "Offer letter", "", db_path=jobs_db)
    db.update_job_from_email("Acme", "INTERVIEW", "Interview", "", db_path=jobs_db)
    db.update_job_from_email("Acme", "APPLIED", "We received it", "", db_path=jobs_db)

    jobs = db.get_all_jobs(db_path=jobs_db)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "OFFER"


def test_forward_progression_still_applies(jobs_db):
    db.update_job_from_email("Acme", "APPLIED", "Received", "", db_path=jobs_db)
    db.update_job_from_email("Acme", "INTERVIEW", "Interview", "", db_path=jobs_db)

    assert db.get_all_jobs(db_path=jobs_db)[0]["status"] == "INTERVIEW"


def test_a_rejection_always_applies(jobs_db):
    """A rejection is definitive at whatever stage it arrives."""
    db.update_job_from_email("Acme", "OFFER", "Offer", "", db_path=jobs_db)
    db.update_job_from_email("Acme", "REJECTED", "Rescinded", "", db_path=jobs_db)

    assert db.get_all_jobs(db_path=jobs_db)[0]["status"] == "REJECTED"


def test_a_backwards_email_is_still_recorded_as_a_note(jobs_db):
    """Not applying the status must not mean discarding the evidence."""
    db.update_job_from_email("Acme", "INTERVIEW", "Interview", "", db_path=jobs_db)
    outcome = db.update_job_from_email(
        "Acme", "APPLIED", "Original confirmation", "auto-ack", db_path=jobs_db
    )

    assert outcome == "noted"
    assert "Original confirmation" in db.get_all_jobs(db_path=jobs_db)[0]["notes"]


@pytest.mark.parametrize(
    "current,proposed,expected",
    [
        (None, "APPLIED", True),
        ("APPLIED", "INTERVIEW", True),
        ("INTERVIEW", "APPLIED", False),
        ("OFFER", "INTERVIEW", False),
        ("OFFER", "REJECTED", True),
        ("INTERVIEW", "INTERVIEW", True),
        # ACTION_REQUIRED ranks low so it cannot pull a later stage back.
        ("INTERVIEW", "ACTION_REQUIRED", False),
        ("APPLIED", "ACTION_REQUIRED", True),
    ],
)
def test_advances(current, proposed, expected):
    assert db.advances(current, proposed) is expected


# =====================================================================
# MATCHING: the right row, not just the right company
# =====================================================================
def test_a_rejection_hits_the_role_it_names(jobs_db):
    """Two roles at one company used to collapse: the rejection for Data
    Engineer flipped the Platform Engineer row instead."""
    db.add_job("Acme", "Platform Engineer", db_path=jobs_db)
    db.add_job("Acme", "Data Engineer", db_path=jobs_db)

    db.update_job_from_email(
        "Acme", "REJECTED", "Data Engineer update", "", role="Data Engineer",
        db_path=jobs_db,
    )

    by_role = {job["role"]: job["status"] for job in db.get_all_jobs(db_path=jobs_db)}
    assert by_role["Data Engineer"] == "REJECTED"
    assert by_role["Platform Engineer"] == "APPLIED"


def test_role_matching_tolerates_a_seniority_prefix(jobs_db):
    """An email saying "Senior Platform Engineer" should find the tracked
    "Platform Engineer" rather than open a second row."""
    db.add_job("Acme", "Platform Engineer", db_path=jobs_db)

    db.update_job_from_email(
        "Acme", "INTERVIEW", "Interview", "", role="Senior Platform Engineer",
        db_path=jobs_db,
    )

    jobs = db.get_all_jobs(db_path=jobs_db)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "INTERVIEW"


def test_a_single_application_matches_without_a_role(jobs_db):
    """One application at a company is unambiguous even with no role given."""
    db.add_job("Acme", "Platform Engineer", db_path=jobs_db)

    db.update_job_from_email("Acme", "INTERVIEW", "Interview", "", db_path=jobs_db)

    jobs = db.get_all_jobs(db_path=jobs_db)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "INTERVIEW"


def test_an_ambiguous_company_does_not_guess(jobs_db):
    """Two roles and no role in the email: creating a new row is wrong, but
    corrupting an existing one is worse."""
    db.add_job("Acme", "Platform Engineer", db_path=jobs_db)
    db.add_job("Acme", "Data Engineer", db_path=jobs_db)

    db.update_job_from_email("Acme", "REJECTED", "Some update", "", db_path=jobs_db)

    statuses = {job["role"]: job["status"] for job in db.get_all_jobs(db_path=jobs_db)}
    assert statuses["Platform Engineer"] == "APPLIED"
    assert statuses["Data Engineer"] == "APPLIED"


def test_a_colliding_insert_attaches_the_note_instead_of_raising(jobs_db):
    """Ambiguous company + a row already at (company, 'Unknown Role')."""
    db.add_job("Acme", "Unknown Role", db_path=jobs_db)
    db.add_job("Acme", "Data Engineer", db_path=jobs_db)

    outcome = db.update_job_from_email(
        "Acme", "INTERVIEW", "Some update", "", db_path=jobs_db
    )

    assert outcome == "noted"
    unknown = next(
        j for j in db.get_all_jobs(db_path=jobs_db) if j["role"] == "Unknown Role"
    )
    assert "Some update" in unknown["notes"]


# =====================================================================
# HISTORY
# =====================================================================
def test_adding_a_job_records_its_first_stage(jobs_db):
    job_id = db.add_job("Acme", "Platform Engineer", db_path=jobs_db)

    history = db.get_status_history(job_id, db_path=jobs_db)
    assert len(history) == 1
    assert history[0]["from_status"] is None
    assert history[0]["to_status"] == "APPLIED"
    assert history[0]["applied"] == 1


def test_each_transition_is_recorded_in_order(jobs_db):
    job_id = db.add_job("Acme", "Platform Engineer", db_path=jobs_db)
    db.update_status(job_id, "ASSESSMENT", db_path=jobs_db)
    db.update_status(job_id, "INTERVIEW", db_path=jobs_db)

    steps = [
        (entry["from_status"], entry["to_status"])
        for entry in db.get_status_history(job_id, db_path=jobs_db)
    ]
    assert steps == [
        (None, "APPLIED"),
        ("APPLIED", "ASSESSMENT"),
        ("ASSESSMENT", "INTERVIEW"),
    ]


def test_a_rejected_backwards_move_is_recorded_but_marked_unapplied(jobs_db):
    """The timeline has to explain a surprising status, not hide the reason."""
    db.update_job_from_email("Acme", "INTERVIEW", "Interview", "", db_path=jobs_db)
    db.update_job_from_email("Acme", "APPLIED", "Confirmation", "", db_path=jobs_db)

    job_id = db.get_all_jobs(db_path=jobs_db)[0]["id"]
    history = db.get_status_history(job_id, db_path=jobs_db)

    unapplied = [entry for entry in history if not entry["applied"]]
    assert len(unapplied) == 1
    assert unapplied[0]["to_status"] == "APPLIED"
    assert unapplied[0]["source"] == "Email Sync"


def test_a_no_op_status_update_is_not_recorded(jobs_db):
    """Re-saving the same status is not a transition and would only add noise."""
    job_id = db.add_job("Acme", "Platform Engineer", db_path=jobs_db)
    db.update_status(job_id, "APPLIED", db_path=jobs_db)

    assert len(db.get_status_history(job_id, db_path=jobs_db)) == 1


def test_manual_updates_may_move_backwards(jobs_db):
    """The rank guard restrains the sync, not the person who owns the data."""
    job_id = db.add_job("Acme", "Platform Engineer", db_path=jobs_db)
    db.update_status(job_id, "OFFER", db_path=jobs_db)
    db.update_status(job_id, "APPLIED", db_path=jobs_db)

    assert db.get_job(job_id, db_path=jobs_db)["status"] == "APPLIED"


def test_history_is_deleted_with_the_job(jobs_db):
    """ON DELETE CASCADE, so a removed application leaves no orphan rows."""
    job_id = db.add_job("Acme", "Platform Engineer", db_path=jobs_db)
    db.update_status(job_id, "INTERVIEW", db_path=jobs_db)
    db.delete_job(job_id, db_path=jobs_db)

    assert db.get_status_history(job_id, db_path=jobs_db) == []


def test_recent_changes_span_every_application(jobs_db):
    db.add_job("Acme", "Platform Engineer", db_path=jobs_db)
    db.add_job("Nexus", "AI Engineer", db_path=jobs_db)

    recent = db.get_recent_status_changes(db_path=jobs_db, limit=10)

    assert {entry["company"] for entry in recent} == {"Acme", "Nexus"}
    # Joined onto jobs, so the dashboard can label a row without another query.
    assert all("role" in entry for entry in recent)


# =====================================================================
# MIGRATION
# =====================================================================
def test_a_v1_database_is_upgraded_and_backfilled(tmp_path):
    """An existing tracker must gain a timeline rather than an empty panel."""
    path = tmp_path / "old.db"

    # Build a v1 database by hand: jobs, no status_history.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL, role TEXT NOT NULL, jd TEXT,
            status TEXT NOT NULL DEFAULT 'APPLIED', date_applied TEXT,
            link TEXT, notes TEXT, source TEXT NOT NULL DEFAULT 'Manual',
            resume_used TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO jobs (company, role, status, source, created_at, updated_at)
        VALUES ('Acme', 'Platform Engineer', 'INTERVIEW', 'Manual',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()

    db.create_table(path)

    job_id = db.get_all_jobs(db_path=path)[0]["id"]
    history = db.get_status_history(job_id, db_path=path)

    assert len(history) == 1
    assert history[0]["to_status"] == "INTERVIEW"
    # Marked so a backfilled row is never mistaken for a real observation.
    assert history[0]["source"] == "Migration"


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "jobs.db"
    db.create_table(path)
    job_id = db.add_job("Acme", "Platform Engineer", db_path=path)

    db.create_table(path)
    db.create_table(path)

    assert len(db.get_status_history(job_id, db_path=path)) == 1


def test_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    db.create_table(path)

    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="newer version"):
        db.create_table(path)
