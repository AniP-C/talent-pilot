"""The one-shot repair for rows whose company holds a job title.

Fixing the extractor stops new bad rows; it does nothing for the ones already
stored, which stay invisible until a recruiter email opens a duplicate. This
script repairs them from the saved posting URL.

A repair script gets run once, against real data, usually in a hurry — so the
behaviour worth pinning is that it never damages a healthy row, never loses
history when merging, and is safe to run twice.
"""

import importlib.util
from pathlib import Path

import pytest

import db

_SPEC = importlib.util.spec_from_file_location(
    "fix_bad_companies",
    Path(__file__).resolve().parent.parent / "deploy" / "fix_bad_companies.py",
)
fix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fix)


# =====================================================================
# DIAGNOSIS
# =====================================================================
@pytest.mark.parametrize(
    "company,role,fragment",
    [
        ("AI Engineer", "AI Engineer", "both"),
        ("AI Engineer", "Senior AI Engineer", "job title"),
        ("Unknown Company", "AI Engineer", "placeholder"),
        ("", "AI Engineer", "empty"),
    ],
)
def test_broken_rows_are_diagnosed(company, role, fragment):
    reason = fix.diagnose({"company": company, "role": role})
    assert reason is not None and fragment in reason


@pytest.mark.parametrize(
    "company,role",
    [
        ("Stripe", "Platform Engineer"),
        ("Nexus Labs", "AI Engineer"),
        ("Engineering Solutions Pvt Ltd", "Backend Developer"),
    ],
)
def test_healthy_rows_are_left_alone(company, role):
    assert fix.diagnose({"company": company, "role": role}) is None


# =====================================================================
# SUGGESTION
# =====================================================================
def test_a_company_is_suggested_from_the_posting_url():
    company, source = fix.suggest(
        {
            "id": 1,
            "company": "AI Engineer",
            "role": "AI Engineer",
            "link": "https://nexuslabs.workable.com/j/ABC",
        },
        {},
    )
    assert company == "Nexuslabs"
    assert source == "posting URL"


def test_an_override_wins_over_the_url():
    company, source = fix.suggest(
        {"id": 7, "company": "AI Engineer", "role": "AI Engineer", "link": "https://x.com/j"},
        {7: "Zerodha"},
    )
    assert company == "Zerodha"
    assert source == "--set"


def test_an_opaque_url_yields_no_suggestion():
    company, reason = fix.suggest(
        {
            "id": 1,
            "company": "Backend Developer",
            "role": "Backend Developer",
            "link": "https://www.linkedin.com/jobs/view/401",
        },
        {},
    )
    assert company == ""
    assert "does not identify" in reason


def test_a_missing_url_is_reported_distinctly():
    """"No link saved" and "link is useless" need different fixes."""
    company, reason = fix.suggest(
        {"id": 1, "company": "AI Engineer", "role": "AI Engineer", "link": ""}, {}
    )
    assert company == ""
    assert "no posting URL" in reason


# =====================================================================
# REPAIR
# =====================================================================
def test_dry_run_changes_nothing(jobs_db):
    db.add_job(
        "AI Engineer", "AI Engineer",
        link="https://nexuslabs.workable.com/j/A", db_path=jobs_db,
    )

    counts = fix.repair_workspace(jobs_db, {}, apply=False)

    assert counts["broken"] == 1
    assert db.get_all_jobs(db_path=jobs_db)[0]["company"] == "AI Engineer"


def test_apply_repairs_the_company(jobs_db):
    db.add_job(
        "AI Engineer", "AI Engineer",
        link="https://nexuslabs.workable.com/j/A", db_path=jobs_db,
    )

    fix.repair_workspace(jobs_db, {}, apply=True)

    assert db.get_all_jobs(db_path=jobs_db)[0]["company"] == "Nexuslabs"


def test_a_healthy_row_is_untouched(jobs_db):
    job_id = db.add_job(
        "Stripe", "Platform Engineer",
        link="https://boards.greenhouse.io/stripe/jobs/1", db_path=jobs_db,
    )
    before = db.get_job(job_id, db_path=jobs_db)

    fix.repair_workspace(jobs_db, {}, apply=True)

    assert db.get_job(job_id, db_path=jobs_db) == before


def test_the_repair_is_recorded_in_the_timeline(jobs_db):
    """A value silently differing from what the user last saw is worse than
    a wrong one they can see was changed."""
    job_id = db.add_job(
        "AI Engineer", "AI Engineer",
        link="https://nexuslabs.workable.com/j/A", db_path=jobs_db,
    )

    fix.repair_workspace(jobs_db, {}, apply=True)

    sources = [e["source"] for e in db.get_status_history(job_id, db_path=jobs_db)]
    assert "Cleanup" in sources


# =====================================================================
# MERGING THE DUPLICATE THE BUG CAUSED
# =====================================================================
def test_a_duplicate_is_merged_and_progress_is_kept(jobs_db):
    """The scraped row sat under a job title while an email created the
    correct entry. Repairing the company collides them; the merge must keep
    the further-along status rather than the row being repaired."""
    db.add_job(
        "AI Engineer", "AI Engineer",
        link="https://nexuslabs.workable.com/j/A", db_path=jobs_db,
    )
    db.update_job_from_email(
        "Nexuslabs", "INTERVIEW", "Interview invitation", "",
        role="AI Engineer", db_path=jobs_db,
    )
    assert len(db.get_all_jobs(db_path=jobs_db)) == 2

    fix.repair_workspace(jobs_db, {}, apply=True)

    jobs = db.get_all_jobs(db_path=jobs_db)
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Nexuslabs"
    assert jobs[0]["status"] == "INTERVIEW"


def test_merging_preserves_history_from_both_rows(jobs_db):
    """status_history cascades on delete, so it has to be repointed first."""
    db.add_job(
        "AI Engineer", "AI Engineer", source="Web Extension",
        link="https://nexuslabs.workable.com/j/A", db_path=jobs_db,
    )
    db.update_job_from_email(
        "Nexuslabs", "INTERVIEW", "Interview invitation", "",
        role="AI Engineer", db_path=jobs_db,
    )

    fix.repair_workspace(jobs_db, {}, apply=True)

    job_id = db.get_all_jobs(db_path=jobs_db)[0]["id"]
    history = db.get_status_history(job_id, db_path=jobs_db)

    # Both originals, plus the cleanup entry.
    assert len(history) >= 3
    assert {"Web Extension", "Email Sync", "Cleanup"} <= {e["source"] for e in history}


def test_merging_keeps_both_sets_of_notes(jobs_db):
    db.add_job(
        "AI Engineer", "AI Engineer", notes="scraped note",
        link="https://nexuslabs.workable.com/j/A", db_path=jobs_db,
    )
    db.update_job_from_email(
        "Nexuslabs", "INTERVIEW", "Interview invitation", "from the recruiter",
        role="AI Engineer", db_path=jobs_db,
    )

    fix.repair_workspace(jobs_db, {}, apply=True)

    notes = db.get_all_jobs(db_path=jobs_db)[0]["notes"]
    assert "scraped note" in notes
    assert "Interview invitation" in notes


# =====================================================================
# SAFETY
# =====================================================================
def test_running_twice_is_a_no_op(jobs_db):
    db.add_job(
        "AI Engineer", "AI Engineer",
        link="https://nexuslabs.workable.com/j/A", db_path=jobs_db,
    )

    fix.repair_workspace(jobs_db, {}, apply=True)
    after_first = db.get_all_jobs(db_path=jobs_db)

    second = fix.repair_workspace(jobs_db, {}, apply=True)

    assert second["broken"] == 0
    assert db.get_all_jobs(db_path=jobs_db) == after_first


def test_a_row_needing_input_is_counted_not_guessed(jobs_db):
    db.add_job(
        "Backend Developer", "Backend Developer",
        link="https://www.linkedin.com/jobs/view/401", db_path=jobs_db,
    )

    counts = fix.repair_workspace(jobs_db, {}, apply=True)

    assert counts["unresolved"] == 1
    assert db.get_all_jobs(db_path=jobs_db)[0]["company"] == "Backend Developer"


def test_a_suggestion_that_is_itself_a_role_is_refused(jobs_db):
    """The repair must not swap one bad company for another."""
    db.add_job(
        "AI Engineer", "AI Engineer",
        link="https://engineer.com/jobs/1", db_path=jobs_db,
    )

    counts = fix.repair_workspace(jobs_db, {}, apply=True)

    assert counts["fixed"] == 0
    assert counts["unresolved"] == 1


def test_better_status_picks_the_further_stage():
    assert fix.better_status("APPLIED", "INTERVIEW") == "INTERVIEW"
    assert fix.better_status("OFFER", "APPLIED") == "OFFER"
    assert fix.better_status("INTERVIEW", "REJECTED") == "REJECTED"


def test_overrides_are_parsed():
    assert fix.parse_overrides(['12=Nexus Labs', '15=Acme']) == {
        12: "Nexus Labs", 15: "Acme"
    }


def test_a_malformed_override_is_rejected():
    with pytest.raises(SystemExit):
        fix.parse_overrides(["nonsense"])
    with pytest.raises(SystemExit):
        fix.parse_overrides(["abc=Company"])
