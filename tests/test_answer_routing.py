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


# =====================================================================
# LENGTH AND TONE
#
# These used to be one hardcoded line in the prompt. The tests below pin the
# default, because the point of adding the controls was to give the user a
# choice — not to change what everyone who never touches them already gets.
# =====================================================================
@pytest.fixture
def captured_prompt(monkeypatch):
    """Capture the prompt instead of calling a model."""
    seen = {}

    def fake(prompt, schema, tag, temperature=None):
        seen["prompt"] = prompt
        seen["tag"] = tag
        seen["temperature"] = temperature
        return {"suggested_answer": "drafted", "confidence_score": 80, "memory_used": ""}

    monkeypatch.setattr(resume_parser, "generate_structured", fake)
    return seen


def test_an_unspecified_length_is_the_two_hundred_words_it_always_was(captured_prompt):
    resume_parser.generate_smart_answer(
        user_id=1,
        question="Tell us about yourself",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
    )

    assert "under 200 words" in captured_prompt["prompt"]


@pytest.mark.parametrize(
    "length,words",
    [("short", 60), ("standard", 200), ("long", 400)],
)
def test_the_chosen_length_sets_the_word_budget(captured_prompt, length, words):
    resume_parser.generate_smart_answer(
        user_id=1,
        question="Tell us about yourself",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
        length=length,
    )

    assert f"under {words} words" in captured_prompt["prompt"]


def test_an_unknown_length_falls_back_rather_than_breaking(captured_prompt):
    resume_parser.generate_smart_answer(
        user_id=1,
        question="Tell us about yourself",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
        length="enormous",
    )

    assert "under 200 words" in captured_prompt["prompt"]


def test_no_tone_adds_no_rule(captured_prompt):
    resume_parser.generate_smart_answer(
        user_id=1,
        question="Tell us about yourself",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
    )

    assert "6." not in captured_prompt["prompt"]


def test_a_tone_arrives_as_one_extra_rule(captured_prompt):
    resume_parser.generate_smart_answer(
        user_id=1,
        question="Tell us about yourself",
        company="Acme",
        role="Engineer",
        jd_text="",
        active_resume_str="{}",
        tone="formal",
    )

    assert "6. Write formally" in captured_prompt["prompt"]


# =====================================================================
# REFINING A DRAFT
# =====================================================================
def test_refining_skips_routing_entirely(captured_prompt, saved_details):
    """The bug this prevents: "shorten" returning a saved detail.

    "Notice period?" routes to the saved answer bank and returns "60 days"
    without a model call. Once there is a draft on screen the user is editing
    that text, and sending the question back through routing would replace
    their paragraph with two words.
    """
    result = resume_parser.refine_answer(
        user_id=1,
        question="Notice period?",
        previous_answer="I am currently serving a sixty day notice period at my employer.",
        instruction=resume_parser.SHORTEN,
    )

    assert result["suggested_answer"] == "drafted"
    assert result["source"] == resume_parser.FROM_MODEL
    assert captured_prompt["tag"] == "REFINE_ANSWER"
    assert "sixty day notice period" in captured_prompt["prompt"]


def test_every_refinement_is_billable(captured_prompt):
    result = resume_parser.refine_answer(
        user_id=1,
        question="Tell us about yourself",
        previous_answer="A paragraph about me.",
        instruction=resume_parser.REPHRASE,
    )

    assert result["billable"] is True


def test_shortening_is_measured_against_the_draft_in_hand():
    """A fixed band is not an edit.

    Cutting four hundred words to the sixty-word band would not shorten the
    answer, it would replace it. What "shorter" means depends on what is there.
    """
    long_draft = " ".join(["word"] * 400)
    short_draft = " ".join(["word"] * 80)

    assert resume_parser._refined_budget(resume_parser.SHORTEN, long_draft) == 220
    assert resume_parser._refined_budget(resume_parser.SHORTEN, short_draft) == 44


def test_expanding_grows_from_the_draft_and_stays_bounded():
    assert resume_parser._refined_budget(resume_parser.EXPAND, "one two three") > 3
    assert resume_parser._refined_budget(
        resume_parser.EXPAND, " ".join(["word"] * 900)
    ) <= 600


def test_refining_nothing_costs_nothing(monkeypatch):
    """No draft means no call, rather than a paid request for an empty edit."""

    def explode(*args, **kwargs):
        raise AssertionError("a model was called with no draft to revise")

    monkeypatch.setattr(resume_parser, "generate_structured", explode)

    result = resume_parser.refine_answer(
        user_id=1, question="Tell us about yourself", previous_answer="   ",
        instruction=resume_parser.SHORTEN,
    )

    assert result["error"] == "NO_DRAFT"


def test_a_free_text_instruction_is_passed_through_as_the_edit(captured_prompt):
    resume_parser.refine_answer(
        user_id=1,
        question="Tell us about yourself",
        previous_answer="A paragraph about me.",
        instruction="mention the Kafka migration",
    )

    assert "mention the Kafka migration" in captured_prompt["prompt"]
