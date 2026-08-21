"""Administrative operations on accounts.

The panel these back can delete a person's entire workspace from a browser, so
the failures worth writing down are the destructive ones: deleting the wrong
thing, deleting more than was asked, leaving something behind that should have
gone, or losing the record that any of it happened.
"""

import json
import zipfile
from io import BytesIO

import pytest

import admin
import auth
import usage
import workspace


@pytest.fixture(autouse=True)
def _isolated(users_db, tmp_path, monkeypatch):
    """Give every test its own accounts database and workspace root.

    The workspace root has to be redirected explicitly: account ids restart at
    1 in each test, so a shared root would have one test's user 1 reading and
    deleting the previous one's files.
    """
    auth.init_db(users_db)
    monkeypatch.setattr(admin, "WORKSPACES_DIR", tmp_path / "workspaces")


@pytest.fixture
def alice(users_db):
    return auth.register("alice@example.com", "password123", db_path=users_db)


@pytest.fixture
def bob(users_db):
    return auth.register("bob@example.com", "password123", db_path=users_db)


def make_workspace(user_id: int, *, files: dict[str, str]) -> None:
    """Put some files where that account's workspace would be."""
    root = admin.WORKSPACES_DIR / str(user_id)

    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# =====================================================================
# WHO IS AN ADMINISTRATOR
# =====================================================================
def test_only_named_addresses_are_administrators(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_EMAILS", frozenset({"boss@example.com"}))

    assert admin.is_admin("boss@example.com")
    # However they happened to type it when registering.
    assert admin.is_admin("  BOSS@Example.COM  ")
    assert not admin.is_admin("alice@example.com")
    assert not admin.is_admin("")


def test_an_instance_with_no_named_admin_has_no_panel(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_EMAILS", frozenset())

    assert not admin.configured()
    assert not admin.is_admin("anyone@example.com")


# =====================================================================
# READING ACCOUNTS
# =====================================================================
def test_an_account_that_never_came_back_still_appears(alice, users_db):
    rows = admin.accounts(db_path=users_db)

    assert [row["email"] for row in rows] == ["alice@example.com"]
    assert rows[0]["paid_units"] == 0
    assert rows[0]["last_login_at"] is None
    # It never used the app, so nothing should have been created for it.
    assert rows[0]["workspace"]["exists"] is False


def test_reading_an_account_does_not_create_its_workspace(alice, users_db):
    admin.accounts(db_path=users_db)

    assert not (admin.WORKSPACES_DIR / str(alice.id)).exists()


def test_usage_is_counted_against_the_right_account(alice, bob, users_db):
    usage.record(alice.id, usage.ANALYZE_JD, quantity=3, db_path=users_db)
    usage.record(alice.id, usage.EMAIL_SYNC, quantity=12, db_path=users_db)
    usage.record(bob.id, usage.KEYWORD_SCAN, quantity=5, db_path=users_db)

    rows = {row["email"]: row for row in admin.accounts(db_path=users_db)}

    assert rows["alice@example.com"]["paid_units"] == 15
    # Keyword scans are free, so they must not reach the billable total.
    assert rows["bob@example.com"]["paid_units"] == 0
    assert rows["bob@example.com"]["events"][usage.KEYWORD_SCAN] == 5


def test_sessions_and_recovery_codes_are_counted(alice, users_db):
    auth.issue_token(alice.id, db_path=users_db)
    auth.issue_token(alice.id, db_path=users_db)
    auth.issue_recovery_codes(alice.id, db_path=users_db)

    row = admin.account(alice.id, db_path=users_db)

    assert row["sessions"] == 2
    assert row["recovery_left"] == auth.RECOVERY_CODE_COUNT


def test_workspace_contents_are_reported(alice, users_db):
    make_workspace(
        alice.id,
        files={
            "profiles/senior.json": '{"name": "Alice"}',
            "answers/why-us.txt": "Because.",
            "gmail_token.json": "{}",
            "last_sync.txt": "2026-08-19T09:12",
        },
    )

    facts = admin.account(alice.id, db_path=users_db)["workspace"]

    assert facts["exists"] is True
    assert facts["profiles"] == 1
    assert facts["answers"] == 1
    assert facts["gmail_connected"] is True
    assert facts["last_sync"] == "2026-08-19T09:12"
    assert facts["bytes"] > 0


def test_the_summary_adds_up_across_accounts(alice, bob, users_db):
    usage.record(alice.id, usage.RESUME_UPLOAD, db_path=users_db)
    usage.record(bob.id, usage.ANSWER_DRAFT, quantity=2, db_path=users_db)

    figures = admin.summary(db_path=users_db)

    assert figures["accounts"] == 2
    assert figures["paid_units"] == 3
    assert figures["active_30d"] == 2
    assert figures["never_returned"] == 2


# =====================================================================
# CHANGING ACCOUNTS
# =====================================================================
def test_renaming_an_account_leaves_its_data_where_it_was(alice, users_db):
    make_workspace(alice.id, files={"profiles/senior.json": "{}"})

    admin.change_email("boss@example.com", alice.id, "alice@newdomain.com", db_path=users_db)

    assert auth.get_user(alice.id, db_path=users_db).email == "alice@newdomain.com"
    # The workspace is keyed on the id, so nothing on disk should have moved.
    assert (admin.WORKSPACES_DIR / str(alice.id) / "profiles" / "senior.json").is_file()


def test_an_address_already_in_use_is_refused(alice, bob, users_db):
    with pytest.raises(auth.AuthError):
        admin.change_email("boss@example.com", bob.id, alice.email, db_path=users_db)

    assert auth.get_user(bob.id, db_path=users_db).email == "bob@example.com"


def test_a_reset_password_works_and_the_old_one_stops(alice, users_db):
    admin.reset_password("boss@example.com", alice.id, "a-brand-new-password", db_path=users_db)

    assert (
        auth.authenticate(
            "alice@example.com", "a-brand-new-password", db_path=users_db
        ).id
        == alice.id
    )

    with pytest.raises(auth.AuthError):
        auth.authenticate("alice@example.com", "password123", db_path=users_db)


def test_a_reset_password_is_never_written_to_the_trail(alice, users_db):
    admin.reset_password("boss@example.com", alice.id, "hunter2-hunter2", db_path=users_db)

    trail = admin.audit_trail(db_path=users_db)

    assert trail[0]["action"] == admin.ACTION_PASSWORD_RESET
    assert "hunter2" not in json.dumps(trail)


def test_a_reset_password_signs_every_device_out(alice, users_db):
    token = auth.issue_token(alice.id, db_path=users_db)

    admin.reset_password("boss@example.com", alice.id, "a-brand-new-password", db_path=users_db)

    assert auth.verify_token(token, db_path=users_db) is None


def test_revoking_sessions_also_clears_handoff_codes(alice, users_db):
    token = auth.issue_token(alice.id, db_path=users_db)
    code = auth.issue_handoff_code(alice.id, db_path=users_db)

    assert admin.revoke_sessions("boss@example.com", alice.id, db_path=users_db) == 1

    assert auth.verify_token(token, db_path=users_db) is None
    # Otherwise a device signed out a moment ago walks back in on a code that
    # was issued before the revocation.
    assert auth.consume_handoff_code(code, db_path=users_db) is None


def test_clearing_lockouts_lets_a_locked_account_try_again(alice, users_db):
    identifier = f"email:{alice.email}"

    for _ in range(20):
        auth.record_failed_attempt(identifier, db_path=users_db)

    assert auth.is_locked_out(identifier, db_path=users_db)

    admin.clear_lockouts("boss@example.com", alice.email, db_path=users_db)

    assert not auth.is_locked_out(identifier, db_path=users_db)


def test_clearing_one_lockout_leaves_the_others(alice, bob, users_db):
    auth.record_failed_attempt(f"email:{alice.email}", db_path=users_db)
    for _ in range(20):
        auth.record_failed_attempt(f"email:{bob.email}", db_path=users_db)

    admin.clear_lockouts("boss@example.com", alice.email, db_path=users_db)

    assert auth.is_locked_out(f"email:{bob.email}", db_path=users_db)


def test_disconnecting_gmail_removes_only_the_token(alice, users_db):
    make_workspace(
        alice.id, files={"gmail_token.json": "{}", "profiles/senior.json": "{}"}
    )

    assert admin.disconnect_gmail("boss@example.com", alice.id, db_path=users_db) is True

    root = admin.WORKSPACES_DIR / str(alice.id)
    assert not (root / "gmail_token.json").exists()
    assert (root / "profiles" / "senior.json").is_file()


def test_disconnecting_gmail_twice_is_not_an_error(alice, users_db):
    assert admin.disconnect_gmail("boss@example.com", alice.id, db_path=users_db) is False


# =====================================================================
# EXPORT
# =====================================================================
def test_an_export_carries_the_workspace_but_not_the_gmail_token(alice, users_db):
    make_workspace(
        alice.id,
        files={
            "profiles/senior.json": '{"name": "Alice"}',
            "answers/why-us.txt": "Because.",
            "gmail_token.json": '{"refresh_token": "secret"}',
        },
    )

    name, payload = admin.export_account("boss@example.com", alice.id, db_path=users_db)

    with zipfile.ZipFile(BytesIO(payload)) as archive:
        contained = set(archive.namelist())
        account = json.loads(archive.read("account.json"))

    assert name.startswith(f"talent-pilot-account-{alice.id}-")
    assert "profiles/senior.json" in contained
    assert "answers/why-us.txt" in contained
    # A live key to somebody's mailbox does not belong in a downloaded file.
    assert "gmail_token.json" not in contained
    assert account["email"] == "alice@example.com"


# =====================================================================
# DELETION
# =====================================================================
def test_deleting_an_account_removes_the_row_the_files_and_the_usage(alice, users_db):
    usage.record(alice.id, usage.ANALYZE_JD, db_path=users_db)
    auth.issue_token(alice.id, db_path=users_db)
    make_workspace(alice.id, files={"jobs.db": "not really a database"})

    result = admin.delete_account("boss@example.com", alice.id, db_path=users_db)

    assert result["email"] == "alice@example.com"
    assert result["workspace_removed"] is True
    assert auth.get_user(alice.id, db_path=users_db) is None
    assert not (admin.WORKSPACES_DIR / str(alice.id)).exists()
    # The FK cascade only fires on a connection that asked for foreign keys.
    assert usage.totals(db_path=users_db)[usage.ANALYZE_JD] == 0


def test_deleting_an_account_leaves_the_others_untouched(alice, bob, users_db):
    make_workspace(alice.id, files={"a.txt": "alice"})
    make_workspace(bob.id, files={"b.txt": "bob"})

    admin.delete_account("boss@example.com", alice.id, db_path=users_db)

    assert [row["email"] for row in admin.accounts(db_path=users_db)] == [
        "bob@example.com"
    ]
    assert (admin.WORKSPACES_DIR / str(bob.id) / "b.txt").is_file()


def test_deleting_an_account_frees_its_address_for_reuse(alice, users_db):
    for _ in range(20):
        auth.record_failed_attempt(f"email:{alice.email}", db_path=users_db)

    admin.delete_account("boss@example.com", alice.id, db_path=users_db)

    # Left behind, the old lockout would greet whoever registered next with
    # that address.
    replacement = auth.register("alice@example.com", "password123", db_path=users_db)
    assert auth.authenticate(
        "alice@example.com", "password123", db_path=users_db
    ).id == replacement.id


def test_the_record_of_a_deletion_outlives_the_account(alice, users_db):
    admin.delete_account("boss@example.com", alice.id, db_path=users_db)

    trail = admin.audit_trail(db_path=users_db)

    assert trail[0]["action"] == admin.ACTION_DELETED
    assert trail[0]["actor"] == "boss@example.com"
    assert "alice@example.com" in trail[0]["detail"]


def test_acting_on_an_account_that_does_not_exist_is_refused(users_db):
    for action in (
        lambda: admin.delete_account("boss@example.com", 999, db_path=users_db),
        lambda: admin.revoke_sessions("boss@example.com", 999, db_path=users_db),
        lambda: admin.reset_password("boss@example.com", 999, "password123", db_path=users_db),
        lambda: admin.change_email("boss@example.com", 999, "x@example.com", db_path=users_db),
        lambda: admin.export_account("boss@example.com", 999, db_path=users_db),
        lambda: admin.disconnect_gmail("boss@example.com", 999, db_path=users_db),
    ):
        with pytest.raises(auth.AuthError):
            action()


def test_a_path_outside_the_workspace_root_is_never_deleted(monkeypatch, tmp_path):
    """The last guard on an rmtree built from a value that arrived over HTTP."""
    monkeypatch.setattr(admin, "WORKSPACES_DIR", tmp_path / "workspaces")

    with pytest.raises((workspace.UnsafePathError, ValueError)):
        admin._remove_workspace("../../etc")


# =====================================================================
# THE AUDIT TRAIL
# =====================================================================
def test_every_change_is_recorded_with_who_did_it(alice, users_db):
    admin.revoke_sessions("boss@example.com", alice.id, db_path=users_db)
    admin.issue_recovery_codes("boss@example.com", alice.id, db_path=users_db)
    admin.change_email("boss@example.com", alice.id, "alice@elsewhere.com", db_path=users_db)

    trail = admin.audit_trail(db_path=users_db)

    assert [entry["action"] for entry in trail] == [
        admin.ACTION_EMAIL_CHANGED,
        admin.ACTION_RECOVERY_ISSUED,
        admin.ACTION_SESSIONS_REVOKED,
    ]
    assert {entry["actor"] for entry in trail} == {"boss@example.com"}


def test_a_rename_records_both_addresses(alice, users_db):
    admin.change_email("boss@example.com", alice.id, "alice@elsewhere.com", db_path=users_db)

    detail = admin.audit_trail(db_path=users_db)[0]["detail"]

    assert "alice@example.com" in detail
    assert "alice@elsewhere.com" in detail


def test_reading_accounts_writes_nothing_to_the_trail(alice, users_db):
    admin.accounts(db_path=users_db)
    admin.summary(db_path=users_db)
    admin.account(alice.id, db_path=users_db)

    assert admin.audit_trail(db_path=users_db) == []
