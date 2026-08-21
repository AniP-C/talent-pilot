"""Keeping an analysis with the job it belongs to.

Two things are being bought here: a score the dashboard can show without
reopening the analyzer, and not paying for the same requirement-by-requirement
analysis twice. The second only holds if a stored result is refused once the
description it describes has changed, which is most of what these check.
"""

import sqlite3

import pytest

import db
import ui


@pytest.fixture
def analysis():
    """The shape analyze_jd returns, trimmed to what gets stored."""
    return {
        "match_percentage": 78,
        "matched_skills": ["Python", "LangChain"],
        "missing_skills": ["Kubernetes"],
        "summary": "Strong on the GenAI stack, thin on infrastructure.",
        "requirements": [
            {
                "skill": "Python",
                "importance": "required",
                "kind": "skill",
                "status": "demonstrated",
                "evidence": "Three years across two roles",
            }
        ],
        "coverage": {"score": 78, "scored": True},
        "keyword_coverage": {
            "score": 61,
            "scored": True,
            "matched": ["Python"],
            "missing": ["Kubernetes"],
            "total": 2,
        },
    }


@pytest.fixture
def job(jobs_db):
    return db.add_job(
        company="Acme Robotics",
        role="AI Engineer",
        jd="We need Python, LangChain and Kubernetes.",
        db_path=jobs_db,
    )


# =====================================================================
# MIGRATION
# =====================================================================
def test_an_existing_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "jobs.db"
    db.create_table(path)

    job_id = db.add_job(company="Globex", role="Engineer", db_path=path)

    # Wind it back to the previous schema, as a workspace in the wild is.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    db.create_table(path)

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()

    assert version == db.SCHEMA_VERSION
    assert {"match_score", "keyword_score", "analysis_json", "analyzed_at"} <= columns
    # The row that was already there is still there.
    assert rows == 1
    assert db.get_job(job_id, db_path=path)["company"] == "Globex"


# =====================================================================
# STORING
# =====================================================================
def test_both_scores_land_on_the_row(job, jobs_db, analysis):
    assert db.save_analysis(job, analysis, jd_text="anything", db_path=jobs_db)

    row = db.get_job(job, db_path=jobs_db)

    assert row["match_score"] == 78
    assert row["keyword_score"] == 61
    assert row["analyzed_at"]


def test_a_job_that_was_never_analysed_has_no_scores(job, jobs_db):
    row = db.get_job(job, db_path=jobs_db)

    # None, not zero. "Never analysed" and "analysed and scored nothing" are
    # different facts and the dashboard renders them differently.
    assert row["match_score"] is None
    assert row["keyword_score"] is None


def test_an_unscoreable_keyword_pass_stores_no_keyword_score(job, jobs_db, analysis):
    """A posting naming no known terms has no keyword score to report."""
    analysis["keyword_coverage"] = {"score": 0, "scored": False, "total": 0}

    db.save_analysis(job, analysis, jd_text="anything", db_path=jobs_db)

    assert db.get_job(job, db_path=jobs_db)["keyword_score"] is None


def test_a_failed_analysis_is_not_stored(job, jobs_db):
    assert not db.save_analysis(
        job, {"error": "RATE_LIMIT", "message": "Quota exceeded"}, db_path=jobs_db
    )
    assert db.get_job(job, db_path=jobs_db)["match_score"] is None


def test_the_whole_analysis_comes_back(job, jobs_db, analysis):
    db.save_analysis(job, analysis, jd_text="anything", db_path=jobs_db)

    restored = db.get_analysis(job, db_path=jobs_db)

    assert restored["match_percentage"] == 78
    assert restored["summary"].startswith("Strong on the GenAI stack")
    assert restored["requirements"][0]["skill"] == "Python"
    assert restored["keyword_coverage"]["missing"] == ["Kubernetes"]


def test_a_corrupt_stored_analysis_reads_as_none(job, jobs_db, analysis):
    """A bad blob means "analyse it again", not a crash on the analyzer tab."""
    db.save_analysis(job, analysis, jd_text="anything", db_path=jobs_db)

    with db.connect(jobs_db) as conn:
        conn.execute("UPDATE jobs SET analysis_json = ? WHERE id = ?", ("{not json", job))

    assert db.get_analysis(job, db_path=jobs_db) is None


# =====================================================================
# REUSE, AND REFUSING TO
# =====================================================================
def test_the_same_description_reuses_the_analysis(job, jobs_db, analysis):
    description = "We need Python, LangChain and Kubernetes."
    db.save_analysis(job, analysis, jd_text=description, db_path=jobs_db)

    found = db.analysis_for("Acme Robotics", "AI Engineer", description, db_path=jobs_db)

    assert found["match_percentage"] == 78
    assert found["job_id"] == job


def test_reformatted_whitespace_is_still_the_same_description(job, jobs_db, analysis):
    db.save_analysis(
        job, analysis, jd_text="We need Python, LangChain and Kubernetes.", db_path=jobs_db
    )

    # Re-copied from the page with different wrapping and casing.
    found = db.analysis_for(
        "Acme Robotics",
        "AI Engineer",
        "we need  Python,\n LangChain and\tKubernetes.",
        db_path=jobs_db,
    )

    assert found is not None


def test_a_changed_description_refuses_the_stored_analysis(job, jobs_db, analysis):
    """The check that makes reuse safe rather than merely cheap."""
    db.save_analysis(job, analysis, jd_text="The original posting.", db_path=jobs_db)

    found = db.analysis_for(
        "Acme Robotics", "AI Engineer", "A rewritten posting asking for Go and Rust.",
        db_path=jobs_db,
    )

    assert found is None


def test_asking_without_a_description_finds_any_stored_analysis(job, jobs_db, analysis):
    db.save_analysis(job, analysis, jd_text="The original posting.", db_path=jobs_db)

    assert db.analysis_for("Acme Robotics", "AI Engineer", db_path=jobs_db) is not None


def test_an_untracked_job_has_nothing_to_reuse(jobs_db):
    assert db.analysis_for("Nobody", "Nothing", "some text", db_path=jobs_db) is None


def test_a_tracked_but_unanalysed_job_has_nothing_to_reuse(job, jobs_db):
    assert (
        db.analysis_for("Acme Robotics", "AI Engineer", "anything", db_path=jobs_db)
        is None
    )


# =====================================================================
# HOW A SCORE IS SHOWN
# =====================================================================
def test_a_missing_score_renders_as_nothing_at_all():
    # Deliberately not "NA": that is what an unstated salary says, and it would
    # read as an analysis that ran and found nothing.
    assert ui.percentage(None) == ""


def test_a_score_renders_as_a_percentage():
    assert ui.percentage(78) == "78%"
    assert ui.percentage(0) == "0%"
