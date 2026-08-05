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
