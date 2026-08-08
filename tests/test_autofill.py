"""The per-user answer bank behind the extension's form suggestions.

This replaced extension/rules.js, a file that shipped one person's real name,
phone number and email inside the extension — which meant the extension could
only ever be used by whoever built it.
"""

import json

import pytest

import autofill
import workspace


@pytest.fixture
def user_id(tmp_path, monkeypatch):
    """An isolated workspace, so answers never touch the real data directory."""
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path)
    return 1


# =====================================================================
# THE CATALOGUE
# =====================================================================
def test_every_field_key_is_unique():
    keys = [field.key for field in autofill.FIELDS]
    assert len(keys) == len(set(keys))


def test_every_field_has_patterns_and_a_group():
    for field in autofill.FIELDS:
        assert field.patterns, f"{field.key} has no patterns"
        assert field.group, f"{field.key} has no group"


def test_choice_fields_offer_options():
    for field in autofill.FIELDS:
        if field.kind == "choice":
            assert field.options, f"{field.key} is a choice with no options"


# =====================================================================
# MATCHING
# =====================================================================
@pytest.fixture
def full_bank(user_id):
    autofill.set_answers(
        user_id,
        {
            "full_name": "Priya Sharma",
            "first_name": "Priya",
            "last_name": "Sharma",
            "email": "priya@example.com",
            "phone": "+91 90000 00000",
            "work_authorized": "Yes",
            "needs_sponsorship": "No",
            "criminal_record": "No",
            "notice_period": "30 days",
            "willing_to_relocate": "Yes",
            "gender": "I prefer not to say",
        },
    )
    return autofill.build_rules(user_id)


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Are you legally authorized to work in the United States?", "Yes"),
        ("Will you now or in the future require visa sponsorship?", "No"),
        ("What is your notice period?", "30 days"),
        ("Are you willing to relocate?", "Yes"),
        ("Email address", "priya@example.com"),
        ("Phone number", "+91 90000 00000"),
    ],
)
def test_common_questions_resolve(label, expected, full_bank):
    assert autofill.match(label, full_bank)["answer"] == expected


def test_first_name_is_matched_before_full_name(full_bank):
    """Catalogue order encodes specificity. The old rules.js tested 'full name'
    first, so every name field on every form filled with the full name."""
    assert autofill.match("First Name", full_bank)["answer"] == "Priya"
    assert autofill.match("Last Name", full_bank)["answer"] == "Sharma"
    assert autofill.match("Full legal name", full_bank)["answer"] == "Priya Sharma"


def test_authorisation_and_sponsorship_do_not_collide(full_bank):
    """They are opposites. Filling both with one value is the classic mistake,
    and it is the difference between being screened in and screened out."""
    authorized = autofill.match("Are you legally authorized to work here?", full_bank)
    sponsorship = autofill.match(
        "Do you require sponsorship for employment visa status?", full_bank
    )

    assert authorized["key"] == "work_authorized"
    assert sponsorship["key"] == "needs_sponsorship"
    assert authorized["answer"] != sponsorship["answer"]


def test_a_compliance_question_buried_in_legal_text_still_matches(full_bank):
    """These arrive as a paragraph, not a tidy label."""
    label = (
        "For purposes of this application and in accordance with applicable "
        "federal and state law, please indicate whether you have ever been "
        "convicted of a felony or misdemeanour. A conviction will not "
        "necessarily disqualify you from employment."
    )
    assert autofill.match(label, full_bank)["key"] == "criminal_record"


def test_an_unanswered_question_is_not_suggested(user_id):
    autofill.set_answers(user_id, {"work_authorized": "Yes"})
    rules = autofill.build_rules(user_id)

    assert autofill.match("What is your notice period?", rules) is None


def test_an_unknown_question_returns_none(full_bank):
    assert autofill.match("What is your favourite colour?", full_bank) is None


def test_empty_label_returns_none(full_bank):
    assert autofill.match("", full_bank) is None
    assert autofill.match(None, full_bank) is None


# =====================================================================
# STORAGE
# =====================================================================
def test_answers_round_trip(user_id):
    autofill.set_answers(user_id, {"notice_period": "60 days"})
    assert autofill.load(user_id)["answers"]["notice_period"] == "60 days"


def test_an_emptied_answer_is_removed_not_blanked(user_id):
    """A field the user deliberately cleared must stop being suggested."""
    autofill.set_answers(user_id, {"notice_period": "60 days"})
    autofill.set_answers(user_id, {"notice_period": ""})

    assert "notice_period" not in autofill.load(user_id)["answers"]


def test_unknown_keys_are_ignored(user_id):
    autofill.set_answers(user_id, {"not_a_real_field": "x"})
    assert autofill.load(user_id)["answers"] == {}


def test_a_missing_file_reads_as_empty(user_id):
    assert autofill.load(user_id) == {"answers": {}, "custom": []}


def test_a_corrupt_file_reads_as_empty_rather_than_raising(user_id):
    workspace.autofill_path(user_id).write_text("{ not json", encoding="utf-8")
    assert autofill.load(user_id) == {"answers": {}, "custom": []}


def test_answers_are_length_capped(user_id):
    autofill.set_answers(user_id, {"notice_period": "x" * 10_000})
    stored = autofill.load(user_id)["answers"]["notice_period"]
    assert len(stored) == autofill.MAX_ANSWER_LENGTH


# =====================================================================
# CUSTOM ANSWERS
# =====================================================================
def test_a_custom_answer_is_matched_literally(user_id):
    """User text is not treated as a regex, so "(" cannot break the pattern."""
    autofill.add_custom(user_id, "Do you hold a valid driving licence? (UK)", "Yes")
    rules = autofill.build_rules(user_id)

    assert autofill.match(
        "Do you hold a valid driving licence? (UK)", rules
    )["answer"] == "Yes"


def test_re_answering_replaces_rather_than_appends(user_id):
    autofill.add_custom(user_id, "Preferred pronouns?", "she/her")
    autofill.add_custom(user_id, "Preferred pronouns?", "they/them")

    custom = autofill.load(user_id)["custom"]
    assert len(custom) == 1
    assert custom[0]["answer"] == "they/them"


def test_custom_matching_is_case_insensitive(user_id):
    autofill.add_custom(user_id, "Driving licence", "Yes")
    rules = autofill.build_rules(user_id)

    assert autofill.match("DRIVING LICENCE", rules) is not None


def test_a_custom_answer_can_be_removed(user_id):
    autofill.add_custom(user_id, "Driving licence", "Yes")
    autofill.remove_custom(user_id, "Driving licence")

    assert autofill.load(user_id)["custom"] == []


def test_a_blank_custom_entry_is_rejected(user_id):
    with pytest.raises(ValueError):
        autofill.add_custom(user_id, "", "Yes")
    with pytest.raises(ValueError):
        autofill.add_custom(user_id, "Question", "")


def test_custom_entries_are_capped(user_id):
    for index in range(autofill.MAX_CUSTOM_ENTRIES):
        autofill.add_custom(user_id, f"Question {index}", "Yes")

    with pytest.raises(ValueError, match="Remove one"):
        autofill.add_custom(user_id, "One too many", "Yes")


def test_catalogue_answers_take_precedence_over_custom(user_id):
    """Custom entries are appended last, so a curated pattern wins."""
    autofill.set_answers(user_id, {"notice_period": "30 days"})
    autofill.add_custom(user_id, "notice period", "90 days")

    assert autofill.match("What is your notice period?", autofill.build_rules(user_id))[
        "answer"
    ] == "30 days"


# =====================================================================
# SEEDING FROM A RESUME
# =====================================================================
RESUME = {
    "name": "Priya Sharma",
    "email": "priya@example.com",
    "phone": "+91 90000 00000",
    "location": "Bengaluru, India",
    "experience": [{"company": "Acme Robotics", "title": "Platform Engineer"}],
    "education": [{"institution": "BITS Pilani", "degree": "B.E.", "year": "2021"}],
}


def test_resume_fields_are_seeded(user_id):
    autofill.seed_from_resume(user_id, RESUME)
    answers = autofill.load(user_id)["answers"]

    assert answers["full_name"] == "Priya Sharma"
    assert answers["email"] == "priya@example.com"
    assert answers["current_company"] == "Acme Robotics"
    assert answers["current_title"] == "Platform Engineer"
    assert answers["university"] == "BITS Pilani"
    assert answers["graduation_year"] == "2021"


def test_a_name_is_split_into_first_and_last(user_id):
    autofill.seed_from_resume(user_id, RESUME)
    answers = autofill.load(user_id)["answers"]

    assert answers["first_name"] == "Priya"
    assert answers["last_name"] == "Sharma"


def test_seeding_never_overwrites_an_existing_answer(user_id):
    """A user correcting a badly-parsed phone number must not have that undone
    by re-uploading the same PDF."""
    autofill.set_answers(user_id, {"phone": "+91 99999 99999"})
    autofill.seed_from_resume(user_id, RESUME)

    assert autofill.load(user_id)["answers"]["phone"] == "+91 99999 99999"


def test_seeding_tolerates_a_sparse_resume(user_id):
    autofill.seed_from_resume(user_id, {"name": "Priya Sharma"})
    assert autofill.load(user_id)["answers"]["full_name"] == "Priya Sharma"


def test_seeding_tolerates_an_empty_resume(user_id):
    autofill.seed_from_resume(user_id, {})
    assert autofill.load(user_id)["answers"] == {}


def test_seeding_does_not_invent_answers_it_cannot_know(user_id):
    """A resume says nothing about criminal record or sponsorship. Guessing
    those would be worse than leaving them blank."""
    autofill.seed_from_resume(user_id, RESUME)
    answers = autofill.load(user_id)["answers"]

    assert "criminal_record" not in answers
    assert "needs_sponsorship" not in answers
    assert "work_authorized" not in answers


# =====================================================================
# COMPLETENESS
# =====================================================================
def test_completeness_counts_answered_fields(user_id):
    autofill.set_answers(user_id, {"work_authorized": "Yes", "notice_period": "30 days"})
    stats = autofill.completeness(user_id)

    assert stats["answered"] == 2
    assert stats["total"] == len(autofill.FIELDS)
    assert stats["missing"] == len(autofill.FIELDS) - 2


def test_completeness_on_an_empty_bank(user_id):
    stats = autofill.completeness(user_id)
    assert stats["answered"] == 0
    assert stats["missing"] == stats["total"]


# =====================================================================
# ISOLATION
# =====================================================================
def test_one_users_answers_are_invisible_to_another(tmp_path, monkeypatch):
    """The whole point of replacing rules.js: answers are per account."""
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path)

    autofill.set_answers(1, {"phone": "+91 11111 11111"})
    autofill.set_answers(2, {"phone": "+91 22222 22222"})

    assert autofill.load(1)["answers"]["phone"] == "+91 11111 11111"
    assert autofill.load(2)["answers"]["phone"] == "+91 22222 22222"


def test_stored_file_is_valid_json(user_id):
    autofill.set_answers(user_id, {"phone": "+91 90000 00000"})
    raw = workspace.autofill_path(user_id).read_text(encoding="utf-8")

    assert json.loads(raw)["answers"]["phone"] == "+91 90000 00000"
