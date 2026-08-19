"""The deterministic half of the calibration set.

Three postings, each carrying a failure this analyser actually made against a
real advert, reduced to a fixture that runs with no network:

* ``enterprise-bank`` — an internal programme name, four "A or B or C" choices,
  a competency-framework paragraph, and a careers-page tail ending in a word
  cloud of every technology the employer uses anywhere.
* ``startup-backend`` — a clean ATS-style posting. Nothing to trim, nothing to
  set aside. It is here to catch a fix that only works on messy input.
* ``vague-generalist`` — culture and a "get in touch". Almost nothing to score.

The model's half of the calibration — how consistently it extracts and
classifies — cannot run here, because it costs money and is not deterministic.
That lives in ``deploy/calibrate.py``, which runs the same fixtures against the
live model and reports the spread.
"""

from pathlib import Path

import pytest

import posting
import scoring

FIXTURES = Path(__file__).parent / "fixtures"
POSTINGS = FIXTURES / "postings"


def load(name: str) -> str:
    return (POSTINGS / f"{name}.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def resume() -> str:
    return (FIXTURES / "synthetic-cv.txt").read_text(encoding="utf-8")


# =====================================================================
# THE PAGE IS NOT THE JOB
# =====================================================================
def test_the_careers_tail_is_removed():
    page = load("enterprise-bank")
    trimmed = posting.trim_to_description(page)

    assert len(trimmed) < len(page)
    # Everything that is the job survives.
    assert "LEDGER Consolidation" in trimmed
    assert "MLOps and LLMOps" in trimmed
    # Everything that is the employer's marketing does not.
    assert "Objective-C" not in trimmed
    assert "holiday allowance" not in trimmed
    assert "Working flexibly" not in trimmed


def test_a_clean_posting_is_left_alone():
    page = load("startup-backend")

    assert posting.trim_to_description(page) == page


def test_the_word_cloud_never_becomes_a_requirement(resume):
    """It put C++, C#, Kotlin and MongoDB into one candidate's list of terms a
    filter would screen them out for, on a role asking for none of them."""
    result = scoring.keyword_coverage(load("enterprise-bank"), resume)
    reported = " ".join(result["matched"] + result["missing"])

    for marketing_only in ("C++", "C#", "Kotlin", "Objective-C", "Hadoop", "DB2"):
        assert marketing_only not in reported


# =====================================================================
# ALTERNATIVES
# =====================================================================
def test_a_choice_is_never_reported_as_several_gaps(resume):
    result = scoring.keyword_coverage(load("enterprise-bank"), resume)

    # The resume has Python and FastAPI, so neither the language choice nor the
    # framework choice is a gap — and Java, Django and Spring Boot must not be
    # named as things to go and learn.
    assert not any("Java" == entry for entry in result["missing"])
    assert not any(entry.startswith("Django") for entry in result["missing"])

    # The frontend choice is a real gap, and it is one gap.
    frontend = [e for e in result["missing"] if "React" in e or "Angular" in e]
    assert len(frontend) == 1
    assert "TypeScript" in frontend[0]


def test_the_framework_choice_is_satisfied_by_one_of_them(resume):
    result = scoring.keyword_coverage(load("enterprise-bank"), resume)
    matched = " ".join(result["matched"])

    assert "LangChain" in matched or "LangGraph" in matched
    assert not any("LlamaIndex" == entry for entry in result["missing"])
    assert not any("Semantic Kernel" == entry for entry in result["missing"])


# =====================================================================
# THE RESUME'S REAL STRENGTHS ARE VISIBLE
# =====================================================================
@pytest.mark.parametrize(
    "term", ["LangGraph", "RAG", "FastAPI", "Python", "Generative AI"]
)
def test_what_the_resume_is_built_on_is_seen(resume, term):
    """LangGraph was absent from the vocabulary entirely while being both what
    the posting asked for and what the resume evidenced best."""
    result = scoring.keyword_coverage(load("enterprise-bank"), resume)

    assert any(term in entry for entry in result["matched"])


# =====================================================================
# A POSTING WITH ALMOST NOTHING IN IT
# =====================================================================
def test_a_vague_posting_yields_almost_no_requirements(resume):
    result = scoring.keyword_coverage(load("vague-generalist"), resume)

    assert result["total"] <= 1


def test_too_few_requirements_is_flagged_rather_than_scored():
    """One missing requirement out of two is 50%, and it reads as a judgement
    rather than as arithmetic over almost no evidence."""
    thin = scoring.score_requirements(
        [
            {"skill": "Python", "importance": "required", "status": "demonstrated",
             "kind": "skill", "evidence": "x"},
            {"skill": "SQL", "importance": "required", "status": "absent",
             "kind": "skill", "evidence": ""},
        ]
    )

    assert thin["thin"] is True
    assert thin["score"] == 50  # still computed, just not presented as certain


def test_a_full_posting_is_not_flagged_thin(resume):
    requirements = [
        {"skill": name, "importance": "required", "status": "demonstrated",
         "kind": "skill", "evidence": "x"}
        for name in ("Python", "FastAPI", "RAG", "Docker", "REST API")
    ]

    assert scoring.score_requirements(requirements)["thin"] is False


# =====================================================================
# THE VOCABULARY NOTICES ITS OWN GAPS
# =====================================================================
def test_the_internal_programme_is_reported_for_review():
    """Not acted on — "LEDGER" is not a skill and neither is "Meridian". This
    is the list a person reads to decide what the vocabulary is missing."""
    terms = scoring.unknown_terms(load("enterprise-bank"))

    assert any("LEDGER" in term for term in terms)


def test_a_requisition_code_is_not_offered_as_a_skill():
    terms = scoring.unknown_terms("Reference Code: JR-0000111867. You will use Python.")

    assert not any(term.lower().startswith("jr") for term in terms)


def test_a_known_pair_joined_by_punctuation_is_not_unknown():
    """"FastAPI/Django" is two terms the vocabulary holds, joined by the
    posting's own slash."""
    terms = scoring.unknown_terms("Build with Python and FastAPI/Django daily.")

    assert not any("/" in term for term in terms)


def test_a_posting_of_known_terms_reports_nothing_to_review():
    terms = scoring.unknown_terms(load("startup-backend"))
    surprises = [t for t in terms if t.lower() not in {"europe", "we"}]

    # Proper nouns can survive; actual technologies should not.
    for known in ("Python", "Kafka", "Terraform", "Prometheus", "Grafana"):
        assert known not in surprises


# =====================================================================
# A POSTING THAT SAYS NOTHING GETS NO SCORE
# =====================================================================
# The worst output this tool produced, found by the calibration script on its
# first run: handed four sentences of culture, the model read the requirements
# off the *resume*, marked all twenty-two demonstrated, and returned 100%. A
# rule in the prompt mostly stops it; this guard is deterministic, so it holds
# whatever the model does.
def test_a_posting_naming_no_technology_is_not_scoreable():
    assert scoring.terms_named(load("vague-generalist")) < scoring.MIN_TERMS_IN_POSTING


@pytest.mark.parametrize("name", ["enterprise-bank", "startup-backend"])
def test_a_real_posting_is_scoreable(name):
    assert scoring.terms_named(load(name)) >= scoring.MIN_TERMS_IN_POSTING


def test_counting_the_posting_ignores_the_resume():
    """The check has to be on the posting alone, because the failure it catches
    is the model importing the resume's skills into the job."""
    assert scoring.terms_named("We value curiosity and ownership.") == 0


def test_the_furniture_does_not_make_a_posting_look_substantive():
    """A page whose only technology words are in the employer's marketing word
    cloud has still not stated any requirements."""
    page = (
        "Software Engineer\n"
        "We're growing fast and value curiosity and ownership.\n"
        "Our technology\n"
        "Kotlin C++ Kubernetes Hadoop MongoDB Terraform Kafka\n"
    )

    assert scoring.terms_named(page) < scoring.MIN_TERMS_IN_POSTING
