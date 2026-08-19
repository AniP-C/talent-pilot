"""Account registration, sign-in, and token lifecycle."""

import pytest

import auth


@pytest.fixture(autouse=True)
def _init(users_db):
    auth.init_db(users_db)


def test_register_returns_user(users_db):
    user = auth.register("Test@Example.com ", "correct horse battery", db_path=users_db)

    assert user.id > 0
    # Emails are normalized so casing can never create a second account.
    assert user.email == "test@example.com"


def test_register_rejects_duplicate_email(users_db):
    auth.register("dup@example.com", "password123", db_path=users_db)

    with pytest.raises(auth.AuthError, match="already exists"):
        auth.register("DUP@example.com", "password123", db_path=users_db)


@pytest.mark.parametrize(
    "email,password",
    [
        ("not-an-email", "password123"),
        ("missing@domain", "password123"),
        ("valid@example.com", "short"),
        ("", "password123"),
    ],
)
def test_register_rejects_invalid_input(users_db, email, password):
    with pytest.raises(auth.AuthError):
        auth.register(email, password, db_path=users_db)


def test_authenticate_accepts_correct_password(users_db):
    created = auth.register("user@example.com", "password123", db_path=users_db)
    signed_in = auth.authenticate("user@example.com", "password123", db_path=users_db)

    assert signed_in.id == created.id


@pytest.mark.parametrize(
    "email,password",
    [
        ("user@example.com", "wrong-password"),
        ("nobody@example.com", "password123"),
    ],
)
def test_authenticate_rejects_bad_credentials(users_db, email, password):
    auth.register("user@example.com", "password123", db_path=users_db)

    with pytest.raises(auth.AuthError, match="Incorrect email or password"):
        auth.authenticate(email, password, db_path=users_db)


def test_password_is_not_stored_in_plaintext(users_db):
    import sqlite3

    auth.register("user@example.com", "password123", db_path=users_db)

    with sqlite3.connect(users_db) as conn:
        stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]

    assert "password123" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_hashes_are_salted_per_user(users_db):
    first = auth.hash_password("identical")
    second = auth.hash_password("identical")

    assert first != second
    assert auth.verify_password("identical", first)
    assert auth.verify_password("identical", second)


def test_verify_password_rejects_malformed_hash():
    assert auth.verify_password("anything", "garbage") is False


# =====================================================================
# TOKENS
# =====================================================================
def test_issued_token_resolves_to_its_user(users_db):
    user = auth.register("user@example.com", "password123", db_path=users_db)
    token = auth.issue_token(user.id, db_path=users_db)

    resolved = auth.verify_token(token, db_path=users_db)

    assert resolved is not None
    assert resolved.id == user.id


def test_unknown_token_is_rejected(users_db):
    assert auth.verify_token("not-a-real-token", db_path=users_db) is None
    assert auth.verify_token("", db_path=users_db) is None


def test_revoked_token_stops_working(users_db):
    user = auth.register("user@example.com", "password123", db_path=users_db)
    token = auth.issue_token(user.id, db_path=users_db)

    auth.revoke_token(token, db_path=users_db)

    assert auth.verify_token(token, db_path=users_db) is None


def test_token_is_stored_hashed(users_db):
    import sqlite3

    user = auth.register("user@example.com", "password123", db_path=users_db)
    token = auth.issue_token(user.id, db_path=users_db)

    with sqlite3.connect(users_db) as conn:
        stored = conn.execute("SELECT token_hash FROM api_tokens").fetchone()[0]

    # A database leak must not hand out live sessions.
    assert stored != token


def test_expired_token_is_rejected(users_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import sqlite3

    user = auth.register("user@example.com", "password123", db_path=users_db)
    token = auth.issue_token(user.id, db_path=users_db)

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with sqlite3.connect(users_db) as conn:
        conn.execute("UPDATE api_tokens SET expires_at = ?", (past,))

    assert auth.verify_token(token, db_path=users_db) is None


def test_changing_password_revokes_tokens(users_db):
    user = auth.register("user@example.com", "password123", db_path=users_db)
    token = auth.issue_token(user.id, db_path=users_db)

    auth.change_password(user.id, "password123", "new-password-456", db_path=users_db)

    assert auth.verify_token(token, db_path=users_db) is None
    assert auth.authenticate("user@example.com", "new-password-456", db_path=users_db)


def test_change_password_requires_current_password(users_db):
    user = auth.register("user@example.com", "password123", db_path=users_db)

    with pytest.raises(auth.AuthError, match="Current password is incorrect"):
        auth.change_password(user.id, "wrong", "new-password-456", db_path=users_db)


# =====================================================================
# HANDOFF CODES (extension -> dashboard sign-on)
# =====================================================================
def test_handoff_code_signs_in_the_right_user(users_db):
    user = auth.register("user@example.com", "password123", db_path=users_db)
    code = auth.issue_handoff_code(user.id, db_path=users_db)

    redeemed = auth.consume_handoff_code(code, db_path=users_db)

    assert redeemed is not None
    assert redeemed.id == user.id


def test_handoff_code_works_only_once(users_db):
    """A code travels in a URL, so replaying it must not work."""
    user = auth.register("user@example.com", "password123", db_path=users_db)
    code = auth.issue_handoff_code(user.id, db_path=users_db)

    assert auth.consume_handoff_code(code, db_path=users_db) is not None
    assert auth.consume_handoff_code(code, db_path=users_db) is None


@pytest.mark.parametrize("bogus", ["", "not-a-code", None])
def test_unknown_handoff_code_is_rejected(users_db, bogus):
    assert auth.consume_handoff_code(bogus, db_path=users_db) is None


def test_expired_handoff_code_is_rejected(users_db):
    import sqlite3
    from datetime import datetime, timedelta, timezone

    user = auth.register("user@example.com", "password123", db_path=users_db)
    code = auth.issue_handoff_code(user.id, db_path=users_db)

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(users_db) as conn:
        conn.execute("UPDATE handoff_codes SET expires_at = ?", (past,))

    assert auth.consume_handoff_code(code, db_path=users_db) is None


def test_handoff_code_is_stored_hashed(users_db):
    import sqlite3

    user = auth.register("user@example.com", "password123", db_path=users_db)
    code = auth.issue_handoff_code(user.id, db_path=users_db)

    with sqlite3.connect(users_db) as conn:
        stored = conn.execute("SELECT code_hash FROM handoff_codes").fetchone()[0]

    assert stored != code


# =====================================================================
# RECOVERY CODES
# =====================================================================
# There is no password reset email in this deployment — the only mail scope the
# app holds is read-only and cannot send — so a forgotten password used to mean
# an account nobody could open, its owner included.
def test_a_recovery_code_sets_a_new_password(users_db):
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    codes = auth.issue_recovery_codes(user.id, db_path=users_db)

    back_in = auth.reset_password_with_code(
        "recover@example.com", codes[0], "brand new password", db_path=users_db
    )

    assert back_in.id == user.id
    assert auth.authenticate(
        "recover@example.com", "brand new password", db_path=users_db
    ).id == user.id


def test_the_old_password_stops_working(users_db):
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    codes = auth.issue_recovery_codes(user.id, db_path=users_db)
    auth.reset_password_with_code(
        "recover@example.com", codes[0], "brand new password", db_path=users_db
    )

    with pytest.raises(auth.AuthError):
        auth.authenticate("recover@example.com", "old password", db_path=users_db)


def test_a_code_works_only_once(users_db):
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    codes = auth.issue_recovery_codes(user.id, db_path=users_db)
    auth.reset_password_with_code(
        "recover@example.com", codes[0], "first new password", db_path=users_db
    )

    with pytest.raises(auth.AuthError):
        auth.reset_password_with_code(
            "recover@example.com", codes[0], "second new password", db_path=users_db
        )


def test_hyphens_spaces_and_case_are_all_accepted(users_db):
    """These get written on paper and typed back weeks later."""
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    code = auth.issue_recovery_codes(user.id, db_path=users_db)[0]

    typed = f" {code.replace('-', ' ').lower()} "

    assert auth.reset_password_with_code(
        "recover@example.com", typed, "brand new password", db_path=users_db
    ).id == user.id


def test_a_code_does_not_open_another_account(users_db):
    """The code identifies a set, not a person; the email has to agree."""
    mine = auth.register("mine@example.com", "my password", db_path=users_db)
    auth.register("theirs@example.com", "their password", db_path=users_db)
    codes = auth.issue_recovery_codes(mine.id, db_path=users_db)

    with pytest.raises(auth.AuthError):
        auth.reset_password_with_code(
            "theirs@example.com", codes[0], "new password", db_path=users_db
        )

    # And theirs is untouched.
    assert auth.authenticate(
        "theirs@example.com", "their password", db_path=users_db
    ).email == "theirs@example.com"


def test_issuing_a_set_invalidates_the_previous_one(users_db):
    """Two live sets would mean a slip of paper from a year ago still works."""
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    first = auth.issue_recovery_codes(user.id, db_path=users_db)
    auth.issue_recovery_codes(user.id, db_path=users_db)

    with pytest.raises(auth.AuthError):
        auth.reset_password_with_code(
            "recover@example.com", first[0], "new password", db_path=users_db
        )


def test_codes_are_stored_hashed(users_db):
    """A copy of the database must not be a stack of live reset codes."""
    import sqlite3

    user = auth.register("recover@example.com", "old password", db_path=users_db)
    codes = auth.issue_recovery_codes(user.id, db_path=users_db)

    conn = sqlite3.connect(users_db)
    stored = [row[0] for row in conn.execute("SELECT code_hash FROM recovery_codes")]
    conn.close()

    assert codes[0] not in stored
    assert auth.normalize_recovery_code(codes[0]) not in stored


def test_a_reset_revokes_existing_tokens(users_db):
    """Whoever is resetting the password is not necessarily whoever is still
    signed in on another device."""
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    token = auth.issue_token(user.id, db_path=users_db)
    codes = auth.issue_recovery_codes(user.id, db_path=users_db)

    auth.reset_password_with_code(
        "recover@example.com", codes[0], "brand new password", db_path=users_db
    )

    assert auth.verify_token(token, db_path=users_db) is None


def test_a_short_new_password_is_refused(users_db):
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    codes = auth.issue_recovery_codes(user.id, db_path=users_db)

    with pytest.raises(auth.AuthError, match="at least"):
        auth.reset_password_with_code(
            "recover@example.com", codes[0], "short", db_path=users_db
        )

    # Refused before the code was spent — a typo in the new password must not
    # cost one of ten.
    assert auth.count_recovery_codes(user.id, db_path=users_db) == len(codes)


def test_wrong_codes_are_rate_limited(users_db):
    """A second door into the account cannot be the unlimited one."""
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    auth.issue_recovery_codes(user.id, db_path=users_db)

    for _ in range(auth.MAX_LOGIN_ATTEMPTS):
        with pytest.raises(auth.AuthError):
            auth.reset_password_with_code(
                "recover@example.com", "AAAA-AAAA-AAAA", "new password",
                db_path=users_db,
            )

    with pytest.raises(auth.RateLimitError):
        auth.reset_password_with_code(
            "recover@example.com", "AAAA-AAAA-AAAA", "new password", db_path=users_db
        )


def test_unused_codes_are_counted(users_db):
    user = auth.register("recover@example.com", "old password", db_path=users_db)
    codes = auth.issue_recovery_codes(user.id, db_path=users_db)

    assert auth.count_recovery_codes(user.id, db_path=users_db) == len(codes)

    auth.reset_password_with_code(
        "recover@example.com", codes[0], "brand new password", db_path=users_db
    )

    assert auth.count_recovery_codes(user.id, db_path=users_db) == len(codes) - 1
