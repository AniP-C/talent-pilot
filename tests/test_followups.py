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


def test_a_direct_line_is_kept_even_when_the_sender_is_a_robot(jobs_db):
    """The ATS relay case. There is no replyable address anywhere in the
    headers, and the recruiter's number is in the sign-off — which is the whole
    reason the phone is stored independently of the address."""
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    db.update_job_from_email(
        company_name="Acme",
        category="INTERVIEW",
        subject="Next steps",
        reasoning="Interview invitation.",
        role="Backend Engineer",
        contact_name="Priya Nair",
        contact_email="no-reply@greenhouse.io",
        contact_phone="+91 98765 43210",
        db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["contact_email"] is None
    assert job["contact_name"] == "Priya Nair"
    assert job["contact_phone"] == "+91 98765 43210"


def test_a_relays_display_name_does_not_overwrite_the_human(jobs_db):
    """The pair has to stay coherent. A no-reply relay signing itself "Acme
    Recruiting" carries a name and no address, and writing it while the human's
    address is still stored would leave a name and an address belonging to two
    different people."""
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Call",
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

    assert job["contact_name"] == "Jane Doe"
    assert job["contact_email"] == "jane@acme.com"


def test_a_bare_name_fills_a_blank_record(jobs_db):
    """The other half of the same rule: with nobody on the record, a name from
    an unattributed message is better than an empty field."""
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    db.update_job_from_email(
        company_name="Acme", category="APPLIED", subject="Received",
        reasoning="", role="Backend Engineer",
        contact_name="Acme Recruiting", contact_email="no-reply@acme.com",
        db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["contact_name"] == "Acme Recruiting"
    assert job["contact_email"] is None


def test_a_later_robot_email_does_not_wipe_a_known_phone(jobs_db):
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Call",
        reasoning="", role="Backend Engineer",
        contact_name="Priya Nair", contact_email="priya@acme.com",
        contact_phone="+91 98765 43210", db_path=jobs_db,
    )
    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Reminder",
        reasoning="", role="Backend Engineer",
        contact_email="no-reply@acme.com", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["contact_phone"] == "+91 98765 43210"
    assert job["contact_email"] == "priya@acme.com"


# =====================================================================
# WHEN IT WAS APPLIED FOR, AND WHERE TO FIND IT
# =====================================================================
def test_an_email_created_row_is_dated_by_the_email(jobs_db):
    """Not by the day the sync ran. An inbox scanned today is full of
    confirmations from weeks ago, and dating them all "today" starts the
    follow-up clock at the wrong end for every one of them."""
    db.update_job_from_email(
        company_name="Acme", category="APPLIED", subject="Received",
        reasoning="", role="Backend Engineer",
        email_date="2026-07-20", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["date_applied"] == "2026-07-20"


def test_a_missing_email_date_falls_back_to_today(jobs_db):
    db.update_job_from_email(
        company_name="Acme", category="APPLIED", subject="Received",
        reasoning="", role="Backend Engineer", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["date_applied"] == datetime.now().strftime("%Y-%m-%d")


def test_an_email_created_row_keeps_a_board_link(jobs_db):
    db.update_job_from_email(
        company_name="Acme", category="APPLIED", subject="Received",
        reasoning="", role="Backend Engineer",
        link="https://boards.greenhouse.io/acme/jobs/1", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["link"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_a_link_to_anywhere_else_is_refused(jobs_db):
    """An email body is written by whoever sent it. See posting.JOB_LINK_HOSTS."""
    db.update_job_from_email(
        company_name="Acme", category="APPLIED", subject="Received",
        reasoning="", role="Backend Engineer",
        link="https://acme-verify.example/pay-now", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["link"] == ""


def test_an_email_link_never_displaces_the_posting_url(jobs_db):
    """A job saved from its own page already has the real URL."""
    db.add_job(
        "Acme", "Backend Engineer",
        link="https://acme.com/careers/backend-engineer", db_path=jobs_db,
    )
    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Next steps",
        reasoning="", role="Backend Engineer",
        link="https://boards.greenhouse.io/acme/jobs/1", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["link"] == "https://acme.com/careers/backend-engineer"


def test_an_email_link_does_fill_a_blank(jobs_db):
    db.add_job("Acme", "Backend Engineer", db_path=jobs_db)
    db.update_job_from_email(
        company_name="Acme", category="INTERVIEW", subject="Next steps",
        reasoning="", role="Backend Engineer",
        link="https://boards.greenhouse.io/acme/jobs/1", db_path=jobs_db,
    )

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=jobs_db)

    assert job["link"] == "https://boards.greenhouse.io/acme/jobs/1"


# =====================================================================
# WHAT THE NOTE RECORDS
# =====================================================================
# Columns are for "who do I reply to"; the note is for "what did this say".
# Everything extracted lands in the note even when it is not promoted, because
# the note is the only durable record of what actually arrived.
def test_the_note_records_the_sender_and_the_extracted_details():
    note = db.compose_email_note(
        "ASSESSMENT",
        "Your coding test",
        "Assessment invitation with a deadline.",
        sender="Acme Talent <no-reply@greenhouse.io>",
        contact_name="Priya Nair",
        contact_email="priya@acme.com",
        contact_phone="+91 98765 43210",
        mentioned_emails=("careers@acme.com",),
        next_step="Complete the HackerRank test",
        deadline="Friday 21 August",
    )

    assert "Subject: Your coding test" in note
    assert "From: Acme Talent <no-reply@greenhouse.io>" in note
    assert "Contact: Priya Nair · priya@acme.com · +91 98765 43210" in note
    assert "Also mentioned: careers@acme.com" in note
    assert "Next step: Complete the HackerRank test" in note
    assert "Deadline: Friday 21 August" in note
    assert "AI Note: Assessment invitation with a deadline." in note


def test_the_note_omits_what_the_email_did_not_say():
    """An empty line reads as a field that failed rather than one that was
    never there."""
    note = db.compose_email_note("APPLIED", "Received", "Confirmation.")

    assert "From:" not in note
    assert "Contact:" not in note
    assert "Next step:" not in note
    assert "Deadline:" not in note


def test_a_flood_of_mentioned_addresses_is_capped():
    note = db.compose_email_note(
        "APPLIED", "Received", "Confirmation.",
        mentioned_emails=tuple(f"a{n}@acme.com" for n in range(12)),
    )

    assert note.count("@acme.com") == 5


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


def test_a_v3_database_gains_the_phone_column(tmp_path):
    """The same shape one version later: a workspace that already has the
    contact columns must gain the phone without losing what it holds."""
    path = tmp_path / "v3.db"

    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL, role TEXT NOT NULL, jd TEXT,
                status TEXT NOT NULL DEFAULT 'APPLIED', date_applied TEXT,
                link TEXT, notes TEXT, source TEXT NOT NULL DEFAULT 'Manual',
                resume_used TEXT, contact_name TEXT, contact_email TEXT,
                last_contact_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO jobs (company, role, status, contact_email, created_at, updated_at)
            VALUES ('Acme', 'Backend Engineer', 'APPLIED', 'jane@acme.com',
                    '2026-01-01', '2026-01-01');
            PRAGMA user_version = 3;
            """
        )

    db.create_table(path)

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=path)

    assert job["contact_phone"] is None
    assert job["contact_email"] == "jane@acme.com"
    assert set(db.JOB_COLUMNS) <= set(job)

    with db.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_a_v4_database_gains_the_posting_columns(tmp_path):
    """A workspace from before salary and location were captured must migrate
    rather than fail on a missing column, and must not lose what it holds."""
    path = tmp_path / "v4.db"

    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL, role TEXT NOT NULL, jd TEXT,
                status TEXT NOT NULL DEFAULT 'APPLIED', date_applied TEXT,
                link TEXT, notes TEXT, source TEXT NOT NULL DEFAULT 'Manual',
                resume_used TEXT, contact_name TEXT, contact_email TEXT,
                contact_phone TEXT, last_contact_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO jobs (company, role, status, contact_email, created_at, updated_at)
            VALUES ('Acme', 'Backend Engineer', 'APPLIED', 'jane@acme.com',
                    '2026-01-01', '2026-01-01');
            PRAGMA user_version = 4;
            """
        )

    db.create_table(path)

    job = db.get_job_by_identity("Acme", "Backend Engineer", db_path=path)

    assert job["location"] is None
    assert job["salary_min"] is None
    # NOT NULL DEFAULT 0, so an existing row reads as "not remote" rather than
    # as unknown — which is what every caller would have to treat it as anyway.
    assert job["remote"] == 0
    assert job["contact_email"] == "jane@acme.com"
    assert set(db.JOB_COLUMNS) <= set(job)

    with db.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# =====================================================================
# THE POSTING'S OWN FACTS
# =====================================================================
def test_a_job_stores_the_salary_it_was_given(jobs_db):
    import posting

    db.add_job(
        "Nexus Labs", "AI Engineer",
        location="Bengaluru, Karnataka",
        remote=True,
        salary=posting.normalise_salary(1800000, 2400000, "INR", "YEAR"),
        db_path=jobs_db,
    )

    job = db.get_job_by_identity("Nexus Labs", "AI Engineer", db_path=jobs_db)

    assert job["location"] == "Bengaluru, Karnataka"
    assert job["remote"] == 1
    assert (job["salary_min"], job["salary_max"]) == (1800000, 2400000)
    assert job["salary_currency"] == "INR"
    assert job["salary_period"] == "YEAR"


def test_a_job_without_a_salary_stores_nulls_not_zeroes(jobs_db):
    """"No salary recorded" and "a salary of zero" are different facts, and only
    one of them should ever be displayed."""
    db.add_job("Nexus Labs", "AI Engineer", db_path=jobs_db)

    job = db.get_job_by_identity("Nexus Labs", "AI Engineer", db_path=jobs_db)

    assert job["salary_min"] is None
    assert job["salary_max"] is None
    assert job["salary_currency"] is None
    assert job["location"] == ""
    assert job["remote"] == 0


def test_get_job_by_identity_misses_cleanly(jobs_db):
    assert db.get_job_by_identity("Nobody", "Nothing", db_path=jobs_db) is None
