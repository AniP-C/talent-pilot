"""Invite codes, closed registration, and sign-in rate limiting.

These guard the app once it is reachable from the internet, so they are
exercised by flipping the config values the deployment would set.
"""

import importlib

import pytest

import auth


@pytest.fixture(autouse=True)
def _init(users_db):
    auth.init_db(users_db)


@pytest.fixture
def invite_required(monkeypatch):
    monkeypatch.setattr(auth, "SIGNUP_CODE", "let-me-in-2026")
    return "let-me-in-2026"


# =====================================================================
# INVITE CODES
# =====================================================================
def test_correct_invite_code_is_accepted(users_db, invite_required):
    user = auth.register(
        "invited@example.com", "password123", invite_required, db_path=users_db
    )

    assert user.email == "invited@example.com"


@pytest.mark.parametrize("bad_code", ["", "wrong-code", "let-me-in", None])
def test_wrong_invite_code_is_rejected(users_db, invite_required, bad_code):
    with pytest.raises(auth.AuthError, match="invite code"):
        auth.register("nope@example.com", "password123", bad_code, db_path=users_db)


def test_invite_code_is_ignored_when_not_configured(users_db, monkeypatch):
    """Local instances leave SIGNUP_CODE empty and must stay frictionless."""
    monkeypatch.setattr(auth, "SIGNUP_CODE", "")

    user = auth.register("local@example.com", "password123", db_path=users_db)

    assert user.id > 0


def test_closed_registration_refuses_everyone(users_db, monkeypatch):
    monkeypatch.setattr(auth, "REGISTRATION_CLOSED", True)

    with pytest.raises(auth.AuthError, match="closed"):
        auth.register("late@example.com", "password123", db_path=users_db)


# =====================================================================
# RATE LIMITING
# =====================================================================
def test_repeated_failures_trigger_a_lockout(users_db, monkeypatch):
    monkeypatch.setattr(auth, "MAX_LOGIN_ATTEMPTS", 3)
    auth.register("target@example.com", "password123", db_path=users_db)

    for _ in range(3):
        with pytest.raises(auth.AuthError):
            auth.authenticate("target@example.com", "wrong", db_path=users_db)

    # The next attempt is refused before the password is even checked, so even
    # the correct password is turned away during the cooldown.
    with pytest.raises(auth.RateLimitError, match="Too many failed attempts"):
        auth.authenticate("target@example.com", "password123", db_path=users_db)


def test_lockout_is_scoped_to_the_identifier(users_db, monkeypatch):
    monkeypatch.setattr(auth, "MAX_LOGIN_ATTEMPTS", 3)
    auth.register("victim@example.com", "password123", db_path=users_db)
    auth.register("bystander@example.com", "password123", db_path=users_db)

    for _ in range(3):
        with pytest.raises(auth.AuthError):
            auth.authenticate("victim@example.com", "wrong", db_path=users_db)

    # One account being attacked must not lock anyone else out.
    assert auth.authenticate(
        "bystander@example.com", "password123", db_path=users_db
    ).email == "bystander@example.com"


def test_successful_sign_in_clears_the_counter(users_db, monkeypatch):
    monkeypatch.setattr(auth, "MAX_LOGIN_ATTEMPTS", 3)
    auth.register("user@example.com", "password123", db_path=users_db)

    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.authenticate("user@example.com", "wrong", db_path=users_db)

    auth.authenticate("user@example.com", "password123", db_path=users_db)

    # Two more failures would have crossed the threshold had it not reset.
    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.authenticate("user@example.com", "wrong", db_path=users_db)

    assert auth.authenticate("user@example.com", "password123", db_path=users_db)


def test_ip_is_limited_independently_of_email(users_db, monkeypatch):
    """Stops an attacker spraying many accounts from one address."""
    monkeypatch.setattr(auth, "MAX_LOGIN_ATTEMPTS", 3)

    for index in range(3):
        with pytest.raises(auth.AuthError):
            auth.authenticate(
                f"guess{index}@example.com",
                "wrong",
                client_ip="203.0.113.5",
                db_path=users_db,
            )

    assert auth.is_locked_out("ip:203.0.113.5", db_path=users_db) is True


def test_old_attempts_stop_counting(users_db, monkeypatch):
    """The window slides, so a lockout is temporary rather than permanent."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(auth, "MAX_LOGIN_ATTEMPTS", 3)
    auth.register("user@example.com", "password123", db_path=users_db)

    for _ in range(3):
        with pytest.raises(auth.AuthError):
            auth.authenticate("user@example.com", "wrong", db_path=users_db)

    assert auth.is_locked_out("email:user@example.com", db_path=users_db) is True

    # Age the recorded attempts past the lockout window.
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(users_db) as conn:
        conn.execute("UPDATE login_attempts SET attempted_at = ?", (stale,))

    assert auth.is_locked_out("email:user@example.com", db_path=users_db) is False
    assert auth.authenticate("user@example.com", "password123", db_path=users_db)


def test_clear_attempts_lifts_a_lockout(users_db, monkeypatch):
    monkeypatch.setattr(auth, "MAX_LOGIN_ATTEMPTS", 2)
    auth.register("user@example.com", "password123", db_path=users_db)

    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.authenticate("user@example.com", "wrong", db_path=users_db)

    auth.clear_attempts("email:user@example.com", db_path=users_db)

    assert auth.authenticate("user@example.com", "password123", db_path=users_db)
