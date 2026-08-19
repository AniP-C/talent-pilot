"""Per-account usage metering.

The point of this table is pricing, so the failure that matters is a wrong
count: an event recorded twice, a batch recorded as one, or an account that
looks idle because its activity was filed under a typo.
"""

import pytest

import auth
import usage


@pytest.fixture(autouse=True)
def _init(users_db):
    auth.init_db(users_db)


@pytest.fixture
def account(users_db):
    return auth.register("user@example.com", "password123", db_path=users_db)


def test_an_event_is_counted_against_its_account(account, users_db):
    usage.record(account.id, usage.ANALYZE_JD, db_path=users_db)
    usage.record(account.id, usage.ANALYZE_JD, db_path=users_db)

    rows = usage.per_user(db_path=users_db)

    assert len(rows) == 1
    assert rows[0]["email"] == "user@example.com"
    assert rows[0]["events"][usage.ANALYZE_JD] == 2


def test_a_batch_counts_its_units_not_one(account, users_db):
    """An inbox sync pays for one model call per email. Recording the run as a
    single event would understate the most expensive thing the app does."""
    usage.record(account.id, usage.EMAIL_SYNC, quantity=17, db_path=users_db)

    assert usage.per_user(db_path=users_db)[0]["events"][usage.EMAIL_SYNC] == 17


def test_paid_units_exclude_the_free_actions(account, users_db):
    usage.record(account.id, usage.ANALYZE_JD, db_path=users_db)
    usage.record(account.id, usage.KEYWORD_SCAN, db_path=users_db)
    usage.record(account.id, usage.JOB_SAVE, db_path=users_db)
    usage.record(account.id, usage.SIGN_IN, db_path=users_db)

    row = usage.per_user(db_path=users_db)[0]

    # The three free actions are recorded and are not billable.
    assert row["paid_units"] == 1
    assert row["events"][usage.KEYWORD_SCAN] == 1


def test_an_unknown_event_is_refused(account, users_db):
    """A typo would otherwise create a category no report ever shows."""
    usage.record(account.id, "analyse.jd", db_path=users_db)

    assert usage.per_user(db_path=users_db)[0]["paid_units"] == 0


def test_an_account_that_did_nothing_still_appears(account, users_db):
    """Signups that never came back are exactly as interesting for pricing as
    the heavy users."""
    rows = usage.per_user(db_path=users_db)

    assert len(rows) == 1
    assert rows[0]["paid_units"] == 0
    assert all(count == 0 for count in rows[0]["events"].values())


def test_accounts_are_ordered_by_what_they_cost(users_db):
    light = auth.register("light@example.com", "password123", db_path=users_db)
    heavy = auth.register("heavy@example.com", "password123", db_path=users_db)

    usage.record(light.id, usage.ANALYZE_JD, db_path=users_db)
    usage.record(heavy.id, usage.EMAIL_SYNC, quantity=40, db_path=users_db)

    assert [row["email"] for row in usage.per_user(db_path=users_db)] == [
        "heavy@example.com",
        "light@example.com",
    ]


def test_one_accounts_usage_is_not_another_accounts(users_db):
    first = auth.register("first@example.com", "password123", db_path=users_db)
    auth.register("second@example.com", "password123", db_path=users_db)

    usage.record(first.id, usage.RESUME_UPLOAD, db_path=users_db)

    by_email = {row["email"]: row for row in usage.per_user(db_path=users_db)}

    assert by_email["first@example.com"]["events"][usage.RESUME_UPLOAD] == 1
    assert by_email["second@example.com"]["events"][usage.RESUME_UPLOAD] == 0


def test_totals_span_every_account(users_db):
    first = auth.register("first@example.com", "password123", db_path=users_db)
    second = auth.register("second@example.com", "password123", db_path=users_db)

    usage.record(first.id, usage.ANALYZE_JD, db_path=users_db)
    usage.record(second.id, usage.ANALYZE_JD, quantity=2, db_path=users_db)

    assert usage.totals(db_path=users_db)[usage.ANALYZE_JD] == 3


def test_active_accounts_counts_only_those_that_did_something(users_db):
    busy = auth.register("busy@example.com", "password123", db_path=users_db)
    auth.register("dormant@example.com", "password123", db_path=users_db)

    usage.record(busy.id, usage.SIGN_IN, db_path=users_db)

    assert usage.active_accounts(30, db_path=users_db) == 1


def test_a_metering_failure_never_breaks_the_caller(account, tmp_path):
    """Bookkeeping must not turn a successful analysis into an error the user
    sees. A path that cannot be opened is swallowed and logged."""
    unwritable = tmp_path / "nope" / "nested" / "users.db"
    unwritable.parent.mkdir(parents=True)
    unwritable.parent.chmod(0o500)

    try:
        usage.record(account.id, usage.ANALYZE_JD, db_path=unwritable)
    finally:
        unwritable.parent.chmod(0o700)


def test_recent_events_name_the_account(account, users_db):
    usage.record(account.id, usage.ANSWER_DRAFT, source="extension", db_path=users_db)

    entry = usage.recent(limit=5, db_path=users_db)[0]

    assert entry["email"] == "user@example.com"
    assert entry["event"] == usage.ANSWER_DRAFT
    assert entry["source"] == "extension"


def test_an_unknown_source_is_filed_rather_than_lost(account, users_db):
    usage.record(account.id, usage.ANALYZE_JD, source="carrier pigeon", db_path=users_db)

    assert usage.recent(limit=1, db_path=users_db)[0]["source"] == "api"
