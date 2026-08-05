"""Workspace isolation and path-traversal defences."""

import pytest

import utils
import workspace


def test_each_user_gets_a_distinct_directory():
    assert workspace.workspace_dir(1) != workspace.workspace_dir(2)


def test_jobs_databases_are_isolated():
    assert workspace.jobs_db_path(1) != workspace.jobs_db_path(2)


@pytest.mark.parametrize(
    "attack",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "../../users.db",
        "subdir/../../escape.json",
    ],
)
def test_profile_paths_cannot_escape_the_workspace(attack):
    """A crafted profile name must stay inside the caller's own directory."""
    resolved = workspace.profile_path(1, attack)

    assert resolved.parent == workspace.profiles_dir(1).resolve()


def test_resolve_within_rejects_absolute_escape(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()

    resolved = workspace.resolve_within(inner, "../outside.txt")

    assert resolved.parent == inner.resolve()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Senior AI Engineer", "Senior_AI_Engineer"),
        ("../../etc/passwd", "passwd"),
        ("my resume!!.json", "my_resume_.json"),
        ("", "profile"),
        ("...", "profile"),
        ("C:\\path\\to\\file.json", "file.json"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert workspace.sanitize_filename(raw) == expected


def test_one_user_cannot_see_another_users_profiles():
    utils.save_profile(101, "mine", {"name": "User 101", "skills": []})
    utils.save_profile(102, "theirs", {"name": "User 102", "skills": []})

    assert workspace.list_profiles(101) == ["mine.json"]
    assert workspace.list_profiles(102) == ["theirs.json"]


def test_missing_profile_does_not_fall_back_to_another_users_file():
    """The old loader grabbed the first JSON it found, leaking other resumes."""
    utils.save_profile(201, "real", {"name": "User 201", "skills": ["python"]})

    loaded = utils.load_profile(202, "real.json")

    assert loaded == utils.EMPTY_PROFILE


def test_load_profile_without_filename_uses_own_latest():
    utils.save_profile(301, "only", {"name": "User 301", "skills": ["sql"]})

    assert utils.load_profile(301)["name"] == "User 301"


def test_load_profile_with_no_profiles_returns_empty():
    assert utils.load_profile(999) == utils.EMPTY_PROFILE


def test_save_and_delete_profile_roundtrip():
    filename = utils.save_profile(401, "target role", {"name": "X", "skills": []})

    assert filename == "target_role.json"
    assert utils.delete_profile(401, filename) is True
    assert workspace.list_profiles(401) == []


def test_delete_profile_rejects_traversal():
    assert utils.delete_profile(402, "../../users.db") is False


def test_last_sync_defaults_to_never():
    assert utils.get_last_sync(501) == "Never"

    utils.update_last_sync(501)

    assert utils.get_last_sync(501) != "Never"
