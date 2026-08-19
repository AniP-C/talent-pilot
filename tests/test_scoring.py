"""Deterministic scoring of a resume against a job description.

The point of moving the arithmetic out of the model is that it becomes
testable, so these are the tests that could not previously exist: the same
input always produces the same number, a must-have counts for more than a
nice-to-have, and the score can never contradict the classifications it was
derived from.
"""

import pytest

import scoring


def req(skill, importance="required", status="demonstrated", evidence="x", kind="skill"):
    return {
        "skill": skill,
        "importance": importance,
        "status": status,
        "evidence": evidence,
        "kind": kind,
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


# =====================================================================
# ALTERNATIVES ARE ONE REQUIREMENT
# =====================================================================
# A job asking for "Python, Go, or Node.js" is asking for one of them. Listing
# the two the candidate lacks as their own absent must-haves invents gaps the
# job never asked for, inflates the denominator, and drags a good candidate
# under. Guarded by a prompt rule, and pinned here so the arithmetic that makes
# it matter cannot drift.
KDK_REQUIREMENTS = [
    req("Backend experience in Python, Go, or Node.js"),
    req("LLM applications using OpenAI, Claude, or Hugging Face", status="partial"),
    req("LangChain or LlamaIndex"),
    req("Retrieval-Augmented Generation"),
    req("Vector database such as Pinecone, Weaviate, or pgvector", status="partial"),
    req("Cloud platform (AWS, GCP, or Azure)"),
    req("APIs"),
    req("Microservices", status="absent"),
    req("Prompt Engineering"),
    req("Model Integration"),
    req("Security and Compliance best practices", status="absent"),
    req("AI Performance Monitoring and Optimization", status="partial"),
    req("B.Tech / BCA or technical certification"),
    req("FinTech or SaaS domain", "preferred", status="absent"),
    req("LLMOps", "preferred", status="absent"),
    req("Model Fine-tuning", "preferred", status="absent"),
    req("CI/CD for AI Applications", "preferred", status="absent"),
    req("Observability for AI systems", "preferred", status="partial"),
]


def test_grouped_alternatives_score_the_candidate_fairly():
    """Eight must-haves demonstrated, three partial, two genuinely absent."""
    result = scoring.score_requirements(scoring.normalise_requirements(KDK_REQUIREMENTS))

    assert result["required_total"] == 13
    assert result["required_met"] == 9.5
    assert result["score"] == 60


def test_splitting_alternatives_invents_gaps_and_sinks_the_score():
    """The observed failure: Go, Node.js and LlamaIndex reported as gaps for a
    candidate whose Python and LangChain already satisfy those requirements."""
    split = KDK_REQUIREMENTS + [
        req("Go", status="absent"),
        req("Node.js", status="absent"),
        req("LlamaIndex", status="absent"),
        req("Hugging Face", status="absent"),
    ]

    correct = scoring.score_requirements(scoring.normalise_requirements(KDK_REQUIREMENTS))
    inflated = scoring.score_requirements(scoring.normalise_requirements(split))

    # Four requirements the job never separately asked for, and the score falls
    # far enough to flip the verdict from "worth tailoring" to "weak fit".
    assert inflated["required_total"] == 17
    assert inflated["score"] < correct["score"] - 10
    assert correct["score"] >= 50 > inflated["score"]


# =====================================================================
# ALTERNATIVES: "either Python or Java" is one requirement
# =====================================================================
# Found on a real Barclays advert scored against a real resume. The requirement
# analysis has always known that a choice is one requirement — it is rule 2 of
# its prompt — and the keyword pass did not, so the same posting was read two
# incompatible ways. It reported Java, Django, Spring and LlamaIndex as gaps
# that the candidate satisfied through the stated alternative, and counted
# React, Angular and TypeScript as three gaps where the posting offers one
# choice. Every one of those sends the user off to add a skill the employer
# never asked them for.
def test_a_choice_between_two_languages_is_one_requirement():
    result = scoring.keyword_coverage(
        "Expertise using either Python or Java.", "I write Python daily."
    )

    assert result["total"] == 1
    assert result["missing"] == []
    assert result["score"] == 100


def test_a_comma_list_ending_in_or_is_one_requirement():
    result = scoring.keyword_coverage(
        "Modern frontend technologies such as React, Angular or TypeScript.",
        "Backend engineer. Python, FastAPI.",
    )

    assert result["total"] == 1
    # One entry naming the whole choice, not three separate failures.
    assert result["missing"] == ["TypeScript / React / Angular"]


def test_a_comma_list_ending_in_and_stays_separate():
    """The distinction the whole thing rests on: "A, B or C" is a menu and
    "A, B and C" is a shopping list."""
    result = scoring.keyword_coverage(
        "Using microservices, REST APIs and Kubernetes.",
        "Built REST APIs.",
    )

    assert result["total"] == 3
    assert result["matched"] == ["REST API"]
    assert sorted(result["missing"]) == ["Kubernetes", "Microservices"]


def test_a_slash_is_a_choice():
    result = scoring.keyword_coverage(
        "Python with FastAPI/Django.", "Shipped services in FastAPI."
    )

    # Two requirements, not one: "with" is not a separator, so the language and
    # the framework stay apart. The framework choice does collapse, and is
    # named by the half the resume actually has.
    assert result["total"] == 2
    assert result["matched"] == ["FastAPI"]
    assert result["missing"] == ["Python"]


def test_a_long_framework_choice_collapses_to_one():
    result = scoring.keyword_coverage(
        "AI frameworks such as LangChain, LangGraph, LlamaIndex, "
        "Semantic Kernel, Spring AI or LangChain4j.",
        "Built agents with LangGraph.",
    )

    assert result["total"] == 1
    assert result["matched"] == ["LangGraph"]
    assert result["missing"] == []


def test_a_word_between_two_terms_breaks_the_run():
    """"Python or Java, including Python with FastAPI" — "including" is not a
    separator, and treating it as one would merge two different sentences."""
    result = scoring.keyword_coverage(
        "We use Kubernetes. Terraform is also required.", "Neither, sorry."
    )

    assert result["total"] == 2


def test_or_inside_a_word_is_not_a_choice():
    """A naive search for "or" matches "orchestration" and "for", which would
    silently merge unrelated requirements into one."""
    result = scoring.keyword_coverage(
        "You will use Kubernetes for orchestration, Terraform for infrastructure.",
        "No infrastructure experience.",
    )

    assert result["total"] == 2


def test_a_satisfied_choice_is_named_by_what_the_resume_has():
    result = scoring.keyword_coverage(
        "Vector stores: Pinecone, Weaviate or pgvector.",
        "Used pgvector and Weaviate in production.",
    )

    assert result["total"] == 1
    # Named in vocabulary order rather than resume order, so the same posting
    # always reads the same way.
    assert result["matched"] == ["Weaviate / pgvector"]


def test_langgraph_is_visible():
    """It was absent from the vocabulary entirely, so a resume naming it five
    times and a posting asking for it by name could not see each other."""
    result = scoring.keyword_coverage(
        "Experience with LangGraph is essential.", "Built multi-agent systems in LangGraph."
    )

    assert result["matched"] == ["LangGraph"]


# =====================================================================
# WHAT CANNOT BE SCORED
# =====================================================================
# A real Barclays advert opened with "you will be required to have an
# experience in SOLD Simplification" — its own multi-year internal programme.
# It was extracted as one of five must-haves, so it removed sixteen points from
# a candidate who could not possibly have had it, and then appeared in the list
# headed "gaps a recruiter would probe" as something to go and fix.
def test_an_employer_internal_requirement_does_not_score():
    with_internal = scoring.score_requirements(
        [
            req("Python"),
            req("RAG"),
            req("SOLD Simplification", status="absent", kind="employer_internal"),
        ]
    )
    without = scoring.score_requirements([req("Python"), req("RAG")])

    assert with_internal["score"] == without["score"] == 100
    assert with_internal["required_total"] == 2


def test_it_is_still_reported_rather_than_dropped():
    """The job did say it. Silently discarding a stated requirement would be
    its own kind of dishonesty."""
    result = scoring.score_requirements(
        [req("Python"), req("SOLD Simplification", status="absent", kind="employer_internal")]
    )

    assert [entry["skill"] for entry in result["not_scored"]] == ["SOLD Simplification"]


def test_a_posting_of_nothing_but_internals_is_not_scored():
    result = scoring.score_requirements(
        [req("Project Atlas", status="absent", kind="employer_internal")]
    )

    assert result["scored"] is False
    assert result["score"] == 0
    assert len(result["not_scored"]) == 1


def test_domain_experience_still_counts():
    """A candidate either has payments experience or does not, and hiding that
    would flatter the score. Contrast with meta below, which was excluded after
    a real advert showed it is absent for everybody."""
    result = scoring.score_requirements(
        [
            req("Python"),
            req("financial services", status="absent", kind="domain"),
        ]
    )

    assert result["required_total"] == 2
    assert result["score"] == 50


def test_the_sixteen_points_come_back():
    """The exact shape of the Barclays run: five must-haves, three met, one of
    the five unwinnable."""
    as_scored = scoring.score_requirements(
        [
            req("Python"),
            req("GenAI delivery"),
            req("enterprise applications", status="partial"),
            req("cloud-native and DevSecOps", status="absent"),
            req("SOLD Simplification", status="absent", kind="employer_internal"),
        ]
    )

    # 2.5 of four real must-haves, rather than 2.5 of five.
    assert as_scored["required_total"] == 4
    assert as_scored["score"] == 62

    # What it scored while the programme name counted as a must-have.
    as_it_was = scoring.score_requirements(
        [
            req("Python"),
            req("GenAI delivery"),
            req("enterprise applications", status="partial"),
            req("cloud-native and DevSecOps", status="absent"),
            req("SOLD Simplification", status="absent"),
        ]
    )
    assert as_it_was["score"] == 50


# =====================================================================
# NORMALISING WHAT THE MODEL RETURNED
# =====================================================================
def test_an_unrecognised_kind_is_scored():
    """The default must never be the one that quietly removes a requirement
    from the denominator."""
    cleaned = scoring.normalise_requirements(
        [{"skill": "Python", "importance": "required", "status": "absent", "kind": "vibes"}]
    )

    assert cleaned[0]["kind"] == scoring.SKILL


def test_a_missing_kind_is_scored():
    cleaned = scoring.normalise_requirements(
        [{"skill": "Python", "importance": "required", "status": "absent"}]
    )

    assert cleaned[0]["kind"] == scoring.SKILL
    assert scoring.score_requirements(cleaned)["required_total"] == 1


def test_a_stated_kind_survives_normalisation():
    cleaned = scoring.normalise_requirements(
        [
            {
                "skill": "SOLD Simplification",
                "importance": "required",
                "status": "absent",
                "kind": "EMPLOYER_INTERNAL",
            }
        ]
    )

    assert cleaned[0]["kind"] == scoring.EMPLOYER_INTERNAL


# =====================================================================
# COMPETENCY WORDING
# =====================================================================
# Barclays closes every advert with "you may be assessed on the key critical
# skills relevant for success in role, such as risk and controls, change and
# transformation, business acumen strategic thinking and digital and
# technology". The model classified all five as meta and marked all five
# absent, along with "secure coding practices" and "effective unit testing
# practices" — seven of twelve must-haves were phrases no resume contains, so
# the score was measuring how little a CV reads like an HR framework.
def test_competency_wording_does_not_score():
    with_meta = scoring.score_requirements(
        [
            req("Python"),
            req("business acumen", status="absent", kind="meta"),
            req("strategic thinking", status="absent", kind="meta"),
        ]
    )

    assert with_meta["score"] == 100
    assert with_meta["required_total"] == 1


def test_it_is_reported_so_it_can_be_prepared_for():
    result = scoring.score_requirements(
        [req("Python"), req("risk and controls", status="absent", kind="meta")]
    )

    assert [entry["skill"] for entry in result["not_scored"]] == ["risk and controls"]


def test_the_two_unscored_kinds_stay_distinguishable():
    """They are not the same thing and are not shown together: one cannot be
    acquired from outside, the other cannot be written on a CV at all."""
    result = scoring.score_requirements(
        [
            req("Python"),
            req("SOLD Simplification", status="absent", kind="employer_internal"),
            req("business acumen", status="absent", kind="meta"),
        ]
    )

    kinds = {entry["skill"]: entry["kind"] for entry in result["not_scored"]}
    assert kinds == {
        "SOLD Simplification": scoring.EMPLOYER_INTERNAL,
        "business acumen": scoring.META,
    }


# =====================================================================
# THE PAGE IS NOT THE JOB
# =====================================================================
def test_the_technology_word_cloud_is_not_a_requirement():
    """Barclays' careers page ends with every technology the bank uses
    anywhere. It put C++, C#, Kotlin and MongoDB into one candidate's list of
    terms a filter would screen them out for, on a role asking for none."""
    page = (
        "AI Engineer\n"
        "You will build with Python and FastAPI, delivering RAG systems.\n"
        "This role is based in our Pune office.\n"
        "What you'll get in return\n"
        "Competitive holiday allowance\n"
        "Kotlin C++ F# Objective-C Hadoop DB2 MongoDB Kubernetes\n"
    )

    result = scoring.keyword_coverage(page, "Python, FastAPI and RAG.")

    assert sorted(result["matched"]) == ["FastAPI", "Python", "RAG"]
    assert result["missing"] == []


def test_a_posting_with_no_furniture_is_untouched():
    """An ordinary ATS posting is the description and nothing else."""
    page = "We need Python, Kubernetes and Terraform."

    assert scoring.keyword_coverage(page, "Python only.")["total"] == 3
