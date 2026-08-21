"""How an application question gets answered, and by what.

The failure this exists to prevent is the one that shipped: every question,
including "Are you based out in Pune? Mention Y/N", handed to a prompt that
asks for a professional paragraph over six thousand characters of resume — so
every question came back as the same pitch. Nothing here calls a model; the
routing decision is the thing under test, and it is made before any call.
"""

import pytest

import autofill
from ai import resume_parser


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """Give each test its own workspace so saved details do not leak between."""
    monkeypatch.setattr(autofill.workspace, "WORKSPACES_DIR", tmp_path)


@pytest.fixture
def saved_details():
    """A user who has filled in the details these questions ask about."""
    autofill.set_answers(
        1,
        {
            "location": "Bengaluru, Karnataka",
            "notice_period": "60 days",
            "willing_to_relocate": "Yes",
        },
    )
    return 1


# =====================================================================
# WHAT SHAPE OF ANSWER A QUESTION WANTS
# =====================================================================
@pytest.mark.parametrize(
    "question",
    [
        "Are you based out in Pune? Mention Y/N",
        "Are you an immediate joiner? Mention Y/N",
        "If N, then is your notice period less than 30 days?",
        "Notice period?",
        "How many years of experience do you have with Python?",
        "Do you have a valid work permit?",
        "Expected CTC?",
    ],
)
def test_a_closed_question_wants_a_word(question):
    assert resume_parser.classify_question(question) == resume_parser.BRIEF


@pytest.mark.parametrize(
    "question",
    [
        "What interests you about working for this company?",
        "Why do you want to work here?",
        "Tell us about yourself",
        "Describe a difficult project you delivered",
        "Explain your approach to testing",
    ],
)
def test_an_open_question_wants_a_paragraph(question):
    assert resume_parser.classify_question(question) == resume_parser.OPEN


def test_why_beats_the_closed_construction():
    """"Why are you interested" contains "are you" and is still prose."""
    assert resume_parser.classify_question(
        "Why are you interested in this role?"
    ) == resume_parser.OPEN


# =====================================================================
# ANSWERING FROM WHAT THE USER ALREADY SAVED
# =====================================================================
def test_a_saved_detail_is_returned_without_a_model_call(saved_details):
    result = resume_parser.generate_smart_answer(
        user_id=1,
        question="Notice period?",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
    )

    assert result["suggested_answer"] == "60 days"
    assert result["source"] == resume_parser.FROM_PROFILE
    # The metering hangs off this: an answer that cost nothing is not a paid
    # unit, and recording it as one overstates what a price has to cover.
    assert result["billable"] is False


def test_a_question_asking_for_the_value_takes_it_verbatim(saved_details):
    """"What is your current location?" contains "is your" and is not yes/no."""
    result = resume_parser.generate_smart_answer(
        user_id=1,
        question="What is your current location?",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
    )

    assert result["suggested_answer"] == "Bengaluru, Karnataka"
    assert result["billable"] is False


def test_a_yes_no_field_answers_a_yes_no_question_directly(saved_details):
    result = resume_parser.generate_smart_answer(
        user_id=1,
        question="Are you willing to relocate?",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
    )

    assert result["suggested_answer"] == "Yes"
    assert result["billable"] is False


def test_the_forms_own_phrasings_reach_the_saved_detail(saved_details):
    """The phrasings that were missing, which sent answerable questions away."""
    rules = autofill.build_rules(1)

    assert autofill.match("Are you based out in Pune? Mention Y/N", rules)["key"] == "location"
    assert autofill.match("Are you an immediate joiner?", rules)["key"] == "notice_period"


def test_a_derived_answer_is_not_handed_over_verbatim(saved_details):
    """"60 days" is a wrong answer in a box that takes Y or N."""
    fact = resume_parser._saved_detail(1, "Is your notice period less than 30 days?")

    assert fact["answer"] == "60 days"
    assert resume_parser._needs_restating(
        "Is your notice period less than 30 days?", fact
    )


# =====================================================================
# REFUSING TO INVENT FACTS
# =====================================================================
@pytest.mark.parametrize(
    "question",
    [
        "Are you based out in Pune? Mention Y/N",
        "Are you an immediate joiner? Mention Y/N",
        "What is your current CTC?",
    ],
)
def test_a_factual_question_with_nothing_saved_asks_the_user(question):
    """No model call, and no invented fact.

    Where somebody lives and how long their notice runs are facts about them.
    A model asked for one produces a plausible one, which is worse than an
    empty box because it is wrong in a way nobody checks before submitting.
    """
    result = resume_parser.generate_smart_answer(
        user_id=1,
        question=question,
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
    )

    assert result["source"] == resume_parser.NEEDS_PROFILE
    assert result["suggested_answer"] == ""
    assert result["billable"] is False
    assert "Application answers" in result["message"]


# =====================================================================
# ANSWER MEMORY
# =====================================================================
def test_the_phrasing_that_was_falling_through_now_has_a_home():
    """This is the question that landed in the general grab-bag."""
    assert (
        resume_parser.categorize_question(
            "What interests you about working for this company?"
        )
        == "why_company.txt"
    )


@pytest.mark.parametrize(
    "question",
    [
        "Why do you want to work here?",
        "What excites you about this company?",
        "What attracted you to us?",
        "Why join our team?",
    ],
)
def test_the_company_question_is_recognised_however_it_is_phrased(question):
    assert resume_parser.categorize_question(question) == "why_company.txt"


def test_the_general_bucket_only_offers_entries_about_the_same_thing():
    """The mechanism that turned one generic paragraph into the house style.

    Every uncategorised question shares general.txt, so handing the whole file
    back as "answers like yours" offered a pitch about GenAI experience as the
    model for a question about notice periods — and each accepted draft
    appended another copy of it.
    """
    stored = (
        "--- Q: What is your notice period arrangement? ---\n"
        "Sixty days from resignation.\n\n"
        "--- Q: Describe your GenAI architecture experience ---\n"
        "I have built RAG pipelines with LangChain and LangGraph."
    )

    kept = resume_parser._entries_resembling(stored, "Confirm your notice period")

    assert "Sixty days" in kept
    assert "LangChain" not in kept


def test_an_unrelated_question_gets_no_exemplars():
    stored = "--- Q: Do you own a laptop? ---\nYes."

    assert resume_parser._entries_resembling(stored, "Preferred joining date") == ""
