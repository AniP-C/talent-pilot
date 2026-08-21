"""Server operations invoked from the admin panel.

This module builds commands that run as root on behalf of a web request, so
the tests that matter are the refusals. A service name or a setting key that
reaches sudo unchecked is a remote shell, and the checks that stop that are
worth more coverage than the happy paths they guard.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import admin
import sysops


@pytest.fixture(autouse=True)
def _quiet_audit(users_db, monkeypatch):
    """Send audit rows to a throwaway database rather than the real one."""
    import auth

    auth.init_db(users_db)
    monkeypatch.setattr(admin, "USERS_DB_PATH", users_db)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / "talent-pilot.env"
    path.write_text(
        "# Talent Pilot configuration\n"
        "GEMINI_API_KEY=secret-key\n"
        "SIGNUP_CODE=letmein\n"
        "REGISTRATION_CLOSED=false\n"
        "LOG_LEVEL=INFO\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sysops, "ENV_FILE_PATH", path)
    return path


# =====================================================================
# READING SETTINGS
# =====================================================================
def test_settings_are_read_from_the_environment_file(env_file):
    values = sysops.read_settings()

    assert values["SIGNUP_CODE"] == "letmein"
    assert values["REGISTRATION_CLOSED"] == "false"
    assert values["GEMINI_API_KEY"] == "secret-key"


def test_a_setting_absent_from_the_file_reads_as_unset(env_file):
    assert sysops.read_settings()["GEMINI_MODEL"] == ""


def test_comments_and_blank_lines_are_ignored(env_file):
    env_file.write_text(
        "\n# SIGNUP_CODE=not-this-one\n\nSIGNUP_CODE=the-real-one\n", encoding="utf-8"
    )

    assert sysops.read_settings()["SIGNUP_CODE"] == "the-real-one"


def test_quoted_values_are_unwrapped(env_file):
    env_file.write_text('SIGNUP_CODE="quoted-code"\n', encoding="utf-8")

    assert sysops.read_settings()["SIGNUP_CODE"] == "quoted-code"


def test_a_missing_environment_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sysops, "ENV_FILE_PATH", tmp_path / "nothing-here.env")

    assert sysops.read_settings()["SIGNUP_CODE"] == ""


# =====================================================================
# VALIDATING SETTINGS
# =====================================================================
def test_a_key_that_is_not_on_the_list_is_refused():
    for key in ("ADMIN_EMAILS", "PUBLIC_URL", "PATH", "DATA_DIR", ""):
        with pytest.raises(sysops.OpsError):
            sysops.validate_setting(key, "anything")


def test_admin_emails_can_never_be_set_from_the_panel():
    """The one setting the panel must not touch.

    Editing it through the app would mean a stolen admin session can appoint
    more administrators, which is the whole reason the setting lives in a
    root-owned file instead of the database.
    """
    assert "ADMIN_EMAILS" not in sysops.SETTING_KEYS


def test_a_value_containing_a_line_break_is_refused():
    # Otherwise the value appends a second line to the file, setting a key
    # that is not on the allowlist at all.
    for value in ("ok\nADMIN_EMAILS=attacker@example.com", "ok\rmore"):
        with pytest.raises(sysops.OpsError):
            sysops.validate_setting("SIGNUP_CODE", value)


def test_a_boolean_only_accepts_true_or_false():
    assert sysops.validate_setting("REGISTRATION_CLOSED", "True") == "true"

    with pytest.raises(sysops.OpsError):
        sysops.validate_setting("REGISTRATION_CLOSED", "yes")


def test_a_number_setting_only_accepts_numbers():
    assert sysops.validate_setting("GMAIL_MAX_RESULTS", "40") == "40"

    with pytest.raises(sysops.OpsError):
        sysops.validate_setting("GMAIL_MAX_RESULTS", "lots")

    with pytest.raises(sysops.OpsError):
        sysops.validate_setting("GMAIL_THROTTLE_SECONDS", "slowly")


def test_a_choice_setting_only_accepts_its_options():
    assert sysops.validate_setting("LOG_LEVEL", "DEBUG") == "DEBUG"

    with pytest.raises(sysops.OpsError):
        sysops.validate_setting("LOG_LEVEL", "VERBOSE")


def test_an_overlong_value_is_refused():
    with pytest.raises(sysops.OpsError):
        sysops.validate_setting("SIGNUP_CODE", "x" * 600)


# =====================================================================
# WRITING SETTINGS
# =====================================================================
def test_writing_a_setting_goes_through_the_helper(monkeypatch, tmp_path, env_file):
    helper = tmp_path / "talent-pilot-envset"
    helper.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(sysops, "ENVSET_COMMAND", str(helper))

    called = []
    monkeypatch.setattr(sysops, "_run", lambda args: called.append(args) or "")

    sysops.write_setting("boss@example.com", "SIGNUP_CODE", "new-code")

    assert called == [["sudo", "-n", str(helper), "SIGNUP_CODE", "new-code"]]


def test_a_setting_change_is_audited(monkeypatch, tmp_path, env_file, users_db):
    helper = tmp_path / "talent-pilot-envset"
    helper.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(sysops, "ENVSET_COMMAND", str(helper))
    monkeypatch.setattr(sysops, "_run", lambda args: "")

    sysops.write_setting("boss@example.com", "SIGNUP_CODE", "new-code")

    entry = admin.audit_trail(db_path=users_db)[0]
    assert entry["action"] == admin.ACTION_SETTING_CHANGED
    assert entry["target"] == "SIGNUP_CODE"
    assert "new-code" in entry["detail"]


def test_a_secret_is_recorded_as_changed_but_never_quoted(
    monkeypatch, tmp_path, env_file, users_db
):
    helper = tmp_path / "talent-pilot-envset"
    helper.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(sysops, "ENVSET_COMMAND", str(helper))
    monkeypatch.setattr(sysops, "_run", lambda args: "")

    sysops.write_setting("boss@example.com", "GEMINI_API_KEY", "AIza-super-secret")

    entry = admin.audit_trail(db_path=users_db)[0]
    # An audit trail that quotes the API key is a copy of the API key.
    assert "AIza-super-secret" not in entry["detail"]
    assert entry["detail"] == "updated"


def test_writing_without_the_helper_installed_says_so(monkeypatch, env_file):
    monkeypatch.setattr(sysops, "ENVSET_COMMAND", "/nowhere/talent-pilot-envset")

    with pytest.raises(sysops.OpsError, match="not installed"):
        sysops.write_setting("boss@example.com", "SIGNUP_CODE", "new-code")


# =====================================================================
# SERVICES
# =====================================================================
def test_only_managed_services_can_be_restarted():
    for name in ("sshd", "nginx", "talent-pilot-api; rm -rf /", ""):
        with pytest.raises(sysops.OpsError, match="not a managed service"):
            sysops.restart_service("boss@example.com", name)


def test_a_restart_is_audited(monkeypatch, users_db):
    monkeypatch.setattr(sysops, "_run", lambda args: "")

    sysops.restart_service("boss@example.com", "talent-pilot-api")

    entry = admin.audit_trail(db_path=users_db)[0]
    assert entry["action"] == admin.ACTION_SERVICE_RESTARTED
    assert entry["target"] == "talent-pilot-api"


# =====================================================================
# LOGS
# =====================================================================
def test_only_named_log_files_can_be_read():
    for name in ("/etc/passwd", "../../etc/shadow", "app.log", ""):
        with pytest.raises(sysops.OpsError, match="not a readable log"):
            sysops.tail(name)


def test_a_log_is_tailed_and_filtered(monkeypatch, tmp_path):
    log = tmp_path / "sync.log"
    log.write_text(
        "\n".join(
            ["line one INFO", "line two SKIP already seen", "line three UPDATED"]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sysops, "LOG_FILES", {"Test": log})

    assert len(sysops.tail("Test")) == 3
    assert sysops.tail("Test", lines=1) == ["line three UPDATED"]
    assert sysops.tail("Test", contains="skip") == ["line two SKIP already seen"]


def test_a_log_file_that_does_not_exist_reads_as_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sysops, "LOG_FILES", {"Test": tmp_path / "absent.log"})

    assert sysops.tail("Test") == []


# =====================================================================
# THE MACHINE
# =====================================================================
def test_machine_health_reports_what_the_platform_offers():
    facts = sysops.machine()

    # Disk is the one field available everywhere the app runs.
    assert facts["disk"]["total"] > 0
    assert 0 <= facts["disk"]["percent"] <= 100
    # The rest are optional, and absent rather than raising where /proc is not.
    assert set(facts) == {"disk", "memory", "load", "uptime_seconds"}


def test_capabilities_describe_a_machine_that_cannot_do_server_actions(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sysops, "ENV_FILE_PATH", tmp_path / "absent.env")
    monkeypatch.setattr(sysops, "ENVSET_COMMAND", "/nowhere/envset")
    monkeypatch.setattr(sysops, "BACKUP_COMMAND", "/nowhere/backup")

    able = sysops.capabilities()

    assert able["env_file"] is False
    assert able["envset"] is False
    assert able["backup"] is False
    # And it says why, so the panel can explain rather than just disable.
    assert able["sudo_detail"]


# =====================================================================
# THE PRIVILEGED HELPER ITSELF
# =====================================================================
# The shell script is the boundary that still holds when the Python above is
# wrong, so it is tested as its own program rather than trusted.
BASH = shutil.which("bash")
HELPER = Path(__file__).resolve().parent.parent / "deploy" / "talent-pilot-envset"

needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")


def run_helper(env_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(HELPER), *args],
        capture_output=True,
        text=True,
        env={"ENV_FILE": str(env_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )


@needs_bash
def test_the_helper_replaces_a_key_in_place(tmp_path):
    env_path = tmp_path / "test.env"
    env_path.write_text(
        "# a comment\nGEMINI_API_KEY=keep-me\nSIGNUP_CODE=old\nLOG_LEVEL=INFO\n",
        encoding="utf-8",
    )

    result = run_helper(env_path, "SIGNUP_CODE", "brand-new")

    assert result.returncode == 0, result.stderr
    assert env_path.read_text(encoding="utf-8") == (
        "# a comment\nGEMINI_API_KEY=keep-me\nSIGNUP_CODE=brand-new\nLOG_LEVEL=INFO\n"
    )


@needs_bash
def test_the_helper_appends_a_key_that_was_not_there(tmp_path):
    env_path = tmp_path / "test.env"
    env_path.write_text("SIGNUP_CODE=old\n", encoding="utf-8")

    assert run_helper(env_path, "REGISTRATION_CLOSED", "true").returncode == 0
    assert "REGISTRATION_CLOSED=true" in env_path.read_text(encoding="utf-8")


@needs_bash
def test_the_helper_collapses_a_duplicated_key(tmp_path):
    """Two lines for one key is ambiguous; after a write it must not be."""
    env_path = tmp_path / "test.env"
    env_path.write_text("LOG_LEVEL=INFO\nOTHER=x\nLOG_LEVEL=DEBUG\n", encoding="utf-8")

    assert run_helper(env_path, "LOG_LEVEL", "WARNING").returncode == 0

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["LOG_LEVEL=WARNING", "OTHER=x"]


@needs_bash
def test_the_helper_refuses_a_key_that_is_not_allowlisted(tmp_path):
    env_path = tmp_path / "test.env"
    original = "SIGNUP_CODE=old\n"
    env_path.write_text(original, encoding="utf-8")

    for key in ("ADMIN_EMAILS", "PATH", "PUBLIC_URL", "lowercase"):
        result = run_helper(env_path, key, "attacker@example.com")

        assert result.returncode != 0
        assert env_path.read_text(encoding="utf-8") == original


@needs_bash
def test_the_helper_refuses_a_value_with_a_line_break(tmp_path):
    env_path = tmp_path / "test.env"
    original = "SIGNUP_CODE=old\n"
    env_path.write_text(original, encoding="utf-8")

    result = run_helper(
        env_path, "SIGNUP_CODE", "fine\nADMIN_EMAILS=attacker@example.com"
    )

    assert result.returncode != 0
    assert env_path.read_text(encoding="utf-8") == original


@needs_bash
def test_the_helper_needs_exactly_two_arguments(tmp_path):
    env_path = tmp_path / "test.env"
    env_path.write_text("SIGNUP_CODE=old\n", encoding="utf-8")

    assert run_helper(env_path, "SIGNUP_CODE").returncode != 0
    assert run_helper(env_path).returncode != 0
