"""Deterministic scoring of a resume against a job description.

The point of moving the arithmetic out of the model is that it becomes
testable, so these are the tests that could not previously exist: the same
input always produces the same number, a must-have counts for more than a
nice-to-have, and the score can never contradict the classifications it was
derived from.
"""

import pytest

import scoring


def req(skill, importance="required", status="demonstrated", evidence="x"):
    return {
        "skill": skill,
        "importance": importance,
        "status": status,
        "evidence": evidence,
    }


# =====================================================================
# REQUIREMENT COVERAGE
# =====================================================================
def test_everything_demonstrated_scores_full():
    result = scoring.score_requirements([req("Python"), req("SQL")])
    assert result["score"] == 100


def test_everything_absent_scores_zero():
    result = scoring.score_requirements(
        [req("Python", status="absent"), req("SQL", status="absent")]
    )
    assert result["score"] == 0


def test_partial_evidence_earns_half():
    """Adjacent experience — another cloud, the technique without the tool —
    is worth something and is not worth full marks."""
    result = scoring.score_requirements([req("AWS", status="partial")])
    assert result["score"] == 50


def test_a_missing_must_have_costs_more_than_a_missing_preferred():
    """Scoring these alike is what let the old number call a candidate a 90%
    match while listing their absent must-haves in the same breath."""
    missing_required = scoring.score_requirements(
        [req("Python", status="absent"), req("FinTech", "preferred")]
    )
    missing_preferred = scoring.score_requirements(
        [req("Python"), req("FinTech", "preferred", status="absent")]
    )

    assert missing_required["score"] < missing_preferred["score"]
    assert missing_required["score"] == 20   # preferred side only
    assert missing_preferred["score"] == 80  # required side only


def test_must_haves_take_full_weight_when_nothing_is_preferred():
    """Otherwise a job stating only must-haves could never exceed 80%."""
    result = scoring.score_requirements([req("Python"), req("SQL")])
    assert result["score"] == 100


def test_only_preferred_requirements_still_scores_out_of_100():
    result = scoring.score_requirements([req("FinTech", "preferred")])
    assert result["score"] == 100


def test_the_score_is_reported_with_its_working():
    result = scoring.score_requirements(
        [
            req("Python"),
            req("AWS", status="partial"),
            req("Go", status="absent"),
            req("FinTech", "preferred", status="absent"),
        ]
    )

    assert result["required_total"] == 3
    assert result["required_met"] == 1.5
    assert result["preferred_total"] == 1
    assert result["preferred_met"] == 0.0


def test_an_empty_analysis_is_flagged_not_scored_as_zero():
    """Zero would read as a terrible match rather than an analysis that did
    not happen."""
    result = scoring.score_requirements([])
    assert result["scored"] is False
    assert result["score"] == 0


def test_the_same_input_always_scores_the_same():
    requirements = [req("Python"), req("AWS", status="partial"), req("Go", status="absent")]
    scores = {scoring.score_requirements(requirements)["score"] for _ in range(10)}
    assert len(scores) == 1


def test_the_score_never_contradicts_the_classifications():
    """The specific failure this replaced: a dozen absent requirements, 90%."""
    requirements = [req(f"Skill {n}", status="absent") for n in range(12)]
    requirements.append(req("Python"))

    result = scoring.score_requirements(requirements)

    assert result["score"] < 20


# =====================================================================
# CLEANING WHAT THE MODEL RETURNED
# =====================================================================
def test_malformed_entries_are_dropped_not_scored_as_absent():
    """A broken entry is not evidence of a gap, and must not drag the score."""
    cleaned = scoring.normalise_requirements(
        [req("Python"), {"skill": ""}, "not a dict", None, {}]
    )
    assert [r["skill"] for r in cleaned] == ["Python"]


def test_a_repeated_skill_is_only_weighted_once():
    cleaned = scoring.normalise_requirements([req("Python"), req("python"), req("PYTHON")])
    assert len(cleaned) == 1


def test_an_unknown_status_is_treated_as_absent():
    cleaned = scoring.normalise_requirements([req("Python", status="probably?")])
    assert cleaned[0]["status"] == "absent"


def test_an_unknown_importance_is_treated_as_a_must_have():
    """Assuming something is optional is the assumption that inflates a score."""
    cleaned = scoring.normalise_requirements([req("Python", importance="whatever")])
    assert cleaned[0]["importance"] == "required"


def test_a_non_list_is_survivable():
    assert scoring.normalise_requirements(None) == []
    assert scoring.normalise_requirements("requirements") == []


# =====================================================================
# KEYWORD COVERAGE
# =====================================================================
def test_a_term_the_job_wants_and_the_resume_has():
    result = scoring.keyword_coverage("We need strong Python.", "I write Python daily.")
    assert result["matched"] == ["Python"]
    assert result["score"] == 100


def test_a_term_the_job_wants_and_the_resume_lacks():
    result = scoring.keyword_coverage("Experience with AWS required.", "I have used Azure.")
    assert "AWS" in result["missing"]
    assert result["score"] == 0


def test_terms_the_job_never_mentions_are_not_counted():
    """The denominator is what this job asks for, not the whole vocabulary."""
    result = scoring.keyword_coverage("We need Python.", "Python, Rust, Kafka, Terraform.")
    assert result["total"] == 1


def test_one_skill_spelled_two_ways_is_one_entry():
    """A naive matcher counts 'LLM' as a hit and 'Large Language Models' as a
    separate miss — the same skill scored both ways, in the same report."""
    result = scoring.keyword_coverage(
        "Work on LLMs and large language models.", "Shipped an LLM feature."
    )
    assert result["matched"] == ["LLM"]
    assert result["missing"] == []
    assert result["total"] == 1


def test_gcp_and_google_cloud_are_the_same_requirement():
    result = scoring.keyword_coverage(
        "GCP or Google Cloud Platform experience.", "Deployed on Google Cloud."
    )
    assert result["matched"] == ["GCP"]
    assert result["total"] == 1


def test_javascript_and_js_are_the_same_requirement():
    result = scoring.keyword_coverage("Strong JavaScript and JS tooling.", "I know js.")
    assert result["matched"] == ["JavaScript"]
    assert result["total"] == 1


@pytest.mark.parametrize(
    "boilerplate",
    ["collaborate", "best practices", "solutions", "technical", "development"],
)
def test_job_description_prose_is_never_a_keyword(boilerplate):
    """The failure mode of counting words instead of skills: a report padded
    with generic English that no resume can meaningfully 'match'."""
    result = scoring.keyword_coverage(
        f"You will {boilerplate} across teams.", "Unrelated resume text."
    )
    assert result["total"] == 0


def test_a_term_is_not_matched_inside_a_longer_word():
    result = scoring.keyword_coverage("We use Java.", "I have JavaScript experience.")
    assert result["missing"] == ["Java"]


def test_the_go_language_is_not_matched_by_the_english_verb():
    """A false match inflates the number this module exists to make honest, so
    bare 'go' is deliberately not a spelling of the language."""
    result = scoring.keyword_coverage(
        "Experience with Golang.", "I go to conferences and go deep on problems."
    )
    assert result["missing"] == ["Go"]


def test_punctuated_terms_still_match():
    result = scoring.keyword_coverage(
        "Node.js and CI/CD pipelines.", "Built Node.js services with CI/CD."
    )
    assert set(result["matched"]) == {"Node.js", "CI/CD"}


def test_a_term_is_not_matched_inside_a_punctuated_name():
    """`\\b` counts "." as a boundary, so `\\bjs\\b` matches the tail of
    "node.js" — a posting wanting Node.js also appeared to want JavaScript."""
    result = scoring.keyword_coverage("We use Node.js here.", "Node.js services.")
    assert result["matched"] == ["Node.js"]
    assert "JavaScript" not in result["matched"] + result["missing"]


def test_sql_is_not_matched_inside_postgresql():
    result = scoring.keyword_coverage("PostgreSQL tuning.", "I use Postgres.")
    assert result["total"] == 1
    assert result["matched"] == ["PostgreSQL"]


def test_matching_ignores_case_and_spacing():
    result = scoring.keyword_coverage("POSTGRES   experience", "postgresql tuning")
    assert result["matched"] == ["PostgreSQL"]


def test_a_posting_with_no_known_terms_is_flagged_not_scored_as_zero():
    result = scoring.keyword_coverage("A role about people and process.", "A resume.")
    assert result["scored"] is False
    assert result["total"] == 0


def test_keyword_coverage_needs_no_network_and_is_stable():
    scores = {
        scoring.keyword_coverage("Python and AWS.", "Python only.")["score"]
        for _ in range(10)
    }
    assert scores == {50}


# =====================================================================
# THE CASE THAT PROMPTED THE REWORK
# =====================================================================
def test_the_genai_posting_no_longer_scores_ninety():
    """A real analysis: strong on LLM/RAG/prompting, absent on most named
    tools. The model returned 90% for exactly this set of findings.
    """
    requirements = [
        req("LLM integration"),
        req("RAG"),
        req("Prompt Engineering"),
        req("Python"),
        req("Cloud platform", status="partial"),          # Azure, not AWS/GCP
        req("Vector databases", status="partial"),         # embeddings, unnamed tools
        req("OpenAI", status="absent"),
        req("Anthropic Claude", status="absent"),
        req("Hugging Face", status="absent"),
        req("Go", status="absent"),
        req("Node.js", status="absent"),
        req("CI/CD for AI", status="absent"),
        req("Observability", status="absent"),
        req("Security and compliance", status="absent"),
        req("FinTech or SaaS domain", "preferred", status="absent"),
        req("LLMOps", "preferred", status="absent"),
        req("Model fine-tuning", "preferred", status="absent"),
    ]

    result = scoring.score_requirements(scoring.normalise_requirements(requirements))

    # Four of fourteen must-haves demonstrated outright, two more partial, and
    # none of the three preferred. That earns 5 of 14 on the required side and
    # nothing on the preferred: 0.8 x (5/14) = 29.
    assert result["score"] == 29
    assert result["required_met"] == 5.0
    assert result["required_total"] == 14
    assert result["preferred_met"] == 0.0

    # Worth recording because it is the whole argument for this module: a
    # third-party keyword scanner independently put the same resume against the
    # same posting at 30%, from completely different inputs. The model's 90 was
    # not a different opinion, it was wrong.
    assert abs(result["score"] - 30) <= 5
