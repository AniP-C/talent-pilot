"""The resume's own text, kept beside its parsed profile.

Why it is kept at all: the keyword pass exists to approximate a literal
filter, and a literal filter reads the document the employer receives. It was
reading the model's re-encoding of that document instead — a flat skills list
and condensed bullets — so anything the parser normalised, merged or dropped
was invisible to the one pass whose whole job is to be literal.
"""

import pytest

import scoring
import utils
import workspace


@pytest.fixture
def user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path)
    return 1


PROFILE = {"name": "Test", "skills": ["python"], "experience": [], "education": []}


# =====================================================================
# TYPESETTING GLYPHS
# =====================================================================
# LaTeX — which is what most engineering CVs are built with — substitutes a
# single glyph for "fi", "fl" and "ffi". pypdf returns the glyph, so a real
# resume extracts as "workﬂows" and "identiﬁcation", and a literal search for
# "workflow" misses a document that visibly says workflow.
@pytest.mark.parametrize(
    "extracted,expected",
    [
        ("automated workﬂows", "automated workflows"),
        ("root-cause identiﬁcation", "root-cause identification"),
        ("manual eﬀort", "manual effort"),
        ("conﬁdence-scored", "confidence-scored"),
        ("ﬃliate", "ffiliate"),
    ],
)
def test_ligatures_become_the_letters_they_stand_for(extracted, expected):
    assert utils.normalise_pdf_text(extracted) == expected


def test_invisible_characters_are_removed():
    """A soft hyphen and a non-breaking space look like nothing on the page and
    are fatal to an exact match."""
    assert utils.normalise_pdf_text("Fast­API") == "FastAPI"
    assert utils.normalise_pdf_text("REST APIs") == "REST APIs"


def test_smart_punctuation_is_flattened():
    assert utils.normalise_pdf_text("“RAG” — the technique") == '"RAG" - the technique'


def test_a_normalised_resume_matches_the_posting():
    """The end-to-end reason the substitution table exists."""
    before = scoring.keyword_coverage(
        "You will own CI/CD and automated workflows.", "Built automated workﬂows."
    )
    after = scoring.keyword_coverage(
        "You will own CI/CD and automated workflows.",
        utils.normalise_pdf_text("Built automated workﬂows."),
    )

    assert "workflows" not in str(before)  # not in the vocabulary either way
    assert after["scored"] is True


# =====================================================================
# STORING IT
# =====================================================================
def test_the_text_is_saved_beside_the_profile(user_id):
    utils.save_profile(user_id, "main", PROFILE, raw_text="Aniruddh. LangGraph. RAG.")

    assert utils.load_profile_text(user_id, "main.json") == "Aniruddh. LangGraph. RAG."


def test_the_profile_itself_does_not_carry_the_text(user_id):
    """Nothing should be paying to send a resume twice in one prompt."""
    utils.save_profile(user_id, "main", PROFILE, raw_text="a very long resume " * 200)

    assert utils.load_profile(user_id, "main.json") == PROFILE


def test_a_profile_with_no_stored_text_reads_empty(user_id):
    """Every profile uploaded before this existed. Empty is a normal answer,
    and the caller falls back to the parsed profile."""
    utils.save_profile(user_id, "legacy", PROFILE)

    assert utils.load_profile_text(user_id, "legacy.json") == ""


def test_the_newest_profile_is_used_when_none_is_named(user_id):
    utils.save_profile(user_id, "only", PROFILE, raw_text="the text")

    assert utils.load_profile_text(user_id) == "the text"


def test_no_profiles_at_all_reads_empty(user_id):
    assert utils.load_profile_text(user_id) == ""


def test_the_text_is_listed_as_a_profile_by_nothing(user_id):
    """The .txt sits in the same directory; it must not appear as a profile."""
    utils.save_profile(user_id, "main", PROFILE, raw_text="text")

    assert workspace.list_profiles(user_id) == ["main.json"]


def test_deleting_a_profile_deletes_the_resume(user_id):
    """Leaving the text behind would keep the document in the workspace after
    the user asked for it to be gone — and a later profile saved under the same
    name would silently inherit somebody else's words."""
    utils.save_profile(user_id, "main", PROFILE, raw_text="private history")
    utils.delete_profile(user_id, "main.json")

    assert utils.load_profile_text(user_id, "main.json") == ""
    assert not workspace.profile_text_path(user_id, "main.json").exists()


def test_a_traversing_name_cannot_reach_out_of_the_workspace(user_id):
    path = workspace.profile_text_path(user_id, "../../escape.json")

    assert path.parent == workspace.profiles_dir(user_id)


def test_one_users_resume_text_is_invisible_to_another(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path)

    utils.save_profile(1, "main", PROFILE, raw_text="user one's resume")
    utils.save_profile(2, "main", PROFILE, raw_text="user two's resume")

    assert utils.load_profile_text(1, "main.json") == "user one's resume"
    assert utils.load_profile_text(2, "main.json") == "user two's resume"


# =====================================================================
# WHAT IT CHANGES
# =====================================================================
def test_the_scan_sees_what_the_parser_dropped():
    """The failure this fixes. The parsed profile is a summary; a term the
    model condensed away is gone from it, and a filter reading the real
    document would have found it."""
    jd = "We need Kubernetes and Terraform experience."
    parsed = '{"skills": ["kubernetes"], "experience": []}'
    real_text = "Ran Kubernetes clusters. Wrote Terraform for the whole estate."

    assert scoring.keyword_coverage(jd, parsed)["missing"] == ["Terraform"]
    assert scoring.keyword_coverage(jd, real_text)["missing"] == []


# =====================================================================
# THE WHOLE ANALYSIS
# =====================================================================
def _stub_analysis(monkeypatch, requirements):
    """Replace the model call so the plumbing around it can be tested."""
    import ai.resume_parser as parser

    monkeypatch.setattr(
        parser,
        "generate_structured",
        lambda *a, **k: {"requirements": requirements, "summary": "stubbed"},
    )
    return parser


def test_the_keyword_pass_reads_the_resume_not_the_profile(monkeypatch):
    parser = _stub_analysis(monkeypatch, [])

    result = parser.analyze_jd(
        "Terraform required.",
        '{"skills": ["python"]}',
        "I have written a great deal of Terraform.",
    )

    assert result["keyword_coverage"]["matched"] == ["Terraform"]


def test_it_falls_back_to_the_profile_when_no_text_is_stored(monkeypatch):
    """Profiles uploaded before the text was kept must still score."""
    parser = _stub_analysis(monkeypatch, [])

    result = parser.analyze_jd("Terraform required.", '{"skills": ["terraform"]}')

    assert result["keyword_coverage"]["matched"] == ["Terraform"]


def test_an_internal_programme_is_not_listed_as_a_gap(monkeypatch):
    """Both clients label this list "gaps a recruiter would probe", so it must
    not contain something no outsider could ever have."""
    parser = _stub_analysis(
        monkeypatch,
        [
            {"skill": "Python", "importance": "required", "kind": "skill",
             "status": "demonstrated", "evidence": "throughout"},
            {"skill": "SOLD Simplification", "importance": "required",
             "kind": "employer_internal", "status": "absent", "evidence": ""},
        ],
    )

    result = parser.analyze_jd("Python and SOLD Simplification.", '{"skills": ["python"]}')

    assert result["missing_skills"] == []
    assert result["match_percentage"] == 100
    # Still reported, so the posting's own words are not silently discarded.
    assert [e["skill"] for e in result["coverage"]["not_scored"]] == ["SOLD Simplification"]
