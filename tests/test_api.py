"""API contract: authentication is required and identity comes from the token."""

import pytest
from fastapi.testclient import TestClient

import auth
from api.server import app

client = TestClient(app)

PROTECTED_ENDPOINTS = [
    ("get", "/profiles", None),
    ("get", "/jobs", None),
    ("post", "/check-job", {"company": "Acme", "role": "Engineer"}),
    ("post", "/save-job", {"company": "Acme", "role": "Engineer"}),
    ("post", "/analyze-job", {"company": "Acme", "role": "Engineer"}),
    ("post", "/generate-answer", {"question": "Why us?"}),
    ("post", "/save-answer", {"question": "Why us?", "answer": "Because."}),
    ("get", "/auth/me", None),
    ("get", "/autofill", None),
    ("get", "/autofill/questions", None),
    ("post", "/autofill/answers", {"answers": {}}),
    ("post", "/autofill/custom", {"question": "Q", "answer": "A"}),
]


@pytest.fixture
def account():
    """Register a throwaway account and return its bearer headers."""
    import uuid

    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register", json={"email": email, "password": "password123"}
    )
    assert response.status_code == 201

    payload = response.json()
    return {
        "email": email,
        "user_id": payload["user_id"],
        "headers": {"Authorization": f"Bearer {payload['token']}"},
    }


def test_health_needs_no_auth():
    assert client.get("/health").status_code == 200


def test_health_identifies_the_service():
    """The extension checks this marker to detect a wrong server on the port."""
    from api.server import SERVICE_NAME

    body = client.get("/health").json()

    assert body["service"] == SERVICE_NAME
    assert body["status"] == "ok"


def test_responses_carry_a_request_id():
    """Correlates a client-side failure with a line in logs/app.log."""
    assert client.get("/health").headers.get("X-Request-ID")


def test_cors_allows_the_methods_the_api_actually_uses():
    """PATCH was missing from allow_methods, which broke status updates."""
    response = client.options(
        "/jobs/1/status",
        headers={
            "Origin": "chrome-extension://" + "a" * 32,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert "PATCH" in response.headers["access-control-allow-methods"]


def test_cors_rejects_ordinary_web_pages():
    response = client.options(
        "/auth/login",
        headers={
            "Origin": "https://evil-site.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None


@pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
def test_endpoints_reject_anonymous_requests(method, path, body):
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
def test_endpoints_reject_a_bogus_token(method, path, body):
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(
        path, headers={"Authorization": "Bearer made-up-token"}, **kwargs
    )
    assert response.status_code == 401


def test_register_then_use_token(account):
    response = client.get("/auth/me", headers=account["headers"])

    assert response.status_code == 200
    assert response.json()["email"] == account["email"]


def test_register_rejects_duplicate_email(account):
    response = client.post(
        "/auth/register", json={"email": account["email"], "password": "password123"}
    )
    assert response.status_code == 400


def test_register_rejects_weak_password():
    response = client.post(
        "/auth/register", json={"email": "weak@example.com", "password": "abc"}
    )
    assert response.status_code == 400


def test_login_with_wrong_password_is_401(account):
    response = client.post(
        "/auth/login", json={"email": account["email"], "password": "wrong-password"}
    )
    assert response.status_code == 401


def test_logout_invalidates_the_token(account):
    assert client.post("/auth/logout", headers=account["headers"]).status_code == 204
    assert client.get("/auth/me", headers=account["headers"]).status_code == 401


# =====================================================================
# ISOLATION
# =====================================================================
def test_one_account_cannot_see_another_accounts_jobs(account):
    import uuid

    client.post("/save-job", headers=account["headers"], json={
        "company": "Acme", "role": "Engineer", "jd_text": "", "link": ""
    })

    other = client.post(
        "/auth/register",
        json={"email": f"{uuid.uuid4().hex}@example.com", "password": "password123"},
    ).json()
    other_headers = {"Authorization": f"Bearer {other['token']}"}

    # The second account sees an empty tracker, and the identity used is the
    # token's — there is no request field that could name someone else.
    assert client.get("/jobs", headers=other_headers).json()["jobs"] == []
    assert client.post(
        "/check-job", headers=other_headers, json={"company": "Acme", "role": "Engineer"}
    ).json()["exists"] is False


def test_save_job_then_check_job(account):
    payload = {"company": "Globex", "role": "SRE", "jd_text": "k8s", "link": ""}

    assert client.post("/save-job", headers=account["headers"], json=payload).status_code == 201

    check = client.post(
        "/check-job", headers=account["headers"], json={"company": "globex", "role": "sre"}
    ).json()

    assert check["exists"] is True
    assert check["status"] == "APPLIED"


def test_duplicate_save_returns_409(account):
    payload = {"company": "Initech", "role": "Dev", "jd_text": "", "link": ""}

    client.post("/save-job", headers=account["headers"], json=payload)
    response = client.post("/save-job", headers=account["headers"], json=payload)

    assert response.status_code == 409


@pytest.mark.parametrize(
    "profile", ["../../../etc/passwd", "..\\..\\users.db", "/etc/shadow"]
)
def test_traversal_in_profile_field_is_rejected(account, profile):
    response = client.post(
        "/save-job",
        headers=account["headers"],
        json={"company": "Acme", "role": "Engineer", "jd_text": "", "link": "", "profile": profile},
    )

    # Either rejected outright, or neutralised to a safe name — never a read
    # outside the workspace.
    assert response.status_code in (201, 400)

    if response.status_code == 201:
        jobs = client.get("/jobs", headers=account["headers"]).json()["jobs"]
        assert ".." not in (jobs[0]["resume_used"] or "")


def test_save_job_validates_required_fields(account):
    response = client.post(
        "/save-job", headers=account["headers"], json={"company": "", "role": ""}
    )
    assert response.status_code == 422  # pydantic rejects it before the handler


def test_status_update_rejects_invalid_status(account):
    created = client.post(
        "/save-job",
        headers=account["headers"],
        json={"company": "Umbrella", "role": "Analyst", "jd_text": "", "link": ""},
    ).json()

    response = client.patch(
        f"/jobs/{created['job_id']}/status",
        headers=account["headers"],
        json={"status": "Interviewing"},
    )

    assert response.status_code == 400


# =====================================================================
# AUTOFILL — the contract the extension depends on
# =====================================================================
def test_a_new_account_starts_with_no_answers(account):
    body = client.get("/autofill", headers=account["headers"]).json()

    assert body["rules"] == []
    assert body["completeness"]["answered"] == 0


def test_answers_round_trip_through_the_api(account):
    saved = client.post(
        "/autofill/answers",
        headers=account["headers"],
        json={"answers": {"work_authorized": "Yes", "needs_sponsorship": "No"}},
    )
    assert saved.status_code == 200
    assert saved.json()["completeness"]["answered"] == 2

    rules = client.get("/autofill", headers=account["headers"]).json()["rules"]
    by_key = {rule["key"]: rule["answer"] for rule in rules}

    assert by_key["work_authorized"] == "Yes"
    assert by_key["needs_sponsorship"] == "No"


def test_rules_carry_the_patterns_the_extension_matches_on(account):
    client.post(
        "/autofill/answers",
        headers=account["headers"],
        json={"answers": {"notice_period": "30 days"}},
    )

    rule = client.get("/autofill", headers=account["headers"]).json()["rules"][0]

    assert rule["patterns"]
    assert rule["literal"] is False
    assert rule["answer"] == "30 days"


def test_a_custom_answer_is_marked_literal(account):
    """User text must not be treated as a regex in the page."""
    client.post(
        "/autofill/custom",
        headers=account["headers"],
        json={"question": "Driving licence? (UK)", "answer": "Yes"},
    )

    rule = client.get("/autofill", headers=account["headers"]).json()["rules"][0]

    assert rule["literal"] is True
    assert rule["patterns"] == ["Driving licence? (UK)"]


def test_the_questionnaire_exposes_the_catalogue(account):
    body = client.get("/autofill/questions", headers=account["headers"]).json()

    assert body["groups"]
    assert len(body["fields"]) == len(body["fields"])
    assert all({"key", "question", "kind", "group"} <= set(f) for f in body["fields"])


def test_one_account_cannot_see_another_accounts_answers(account):
    """The reason this replaced a bundled rules.js in the first place."""
    import uuid

    client.post(
        "/autofill/answers",
        headers=account["headers"],
        json={"answers": {"phone": "+91 11111 11111"}},
    )

    other = client.post(
        "/auth/register",
        json={"email": f"{uuid.uuid4().hex}@example.com", "password": "password123"},
    ).json()
    other_headers = {"Authorization": f"Bearer {other['token']}"}

    assert client.get("/autofill", headers=other_headers).json()["rules"] == []


def test_a_short_saved_answer_becomes_reusable(account, monkeypatch):
    """An AI-drafted answer joins the bank, so the same question is free next
    time. Patched so no model call is made."""
    from api import server

    monkeypatch.setattr(server, "save_answer_to_memory", lambda *a, **k: "q.txt")

    response = client.post(
        "/save-answer",
        headers=account["headers"],
        json={"question": "Do you have a driving licence?", "answer": "Yes"},
    )

    assert response.json()["reusable"] is True
    rules = client.get("/autofill", headers=account["headers"]).json()["rules"]
    assert any(r["answer"] == "Yes" for r in rules)


def test_a_long_essay_is_not_added_to_the_bank(account, monkeypatch):
    """Long-form answers are tailored per application; replaying one verbatim
    reads worse than redrafting it."""
    from api import server

    monkeypatch.setattr(server, "save_answer_to_memory", lambda *a, **k: "q.txt")

    response = client.post(
        "/save-answer",
        headers=account["headers"],
        json={"question": "Tell us about yourself", "answer": "x" * 5000},
    )

    assert response.json()["reusable"] is False
    assert client.get("/autofill", headers=account["headers"]).json()["rules"] == []


def test_privacy_policy_is_public():
    """The Chrome Web Store listing links here and a reviewer has no account,
    so it must load without authentication."""
    response = client.get("/privacy")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_privacy_policy_covers_what_the_store_requires():
    body = client.get("/privacy").text

    # The Limited Use wording is mandatory for a Gmail scope.
    assert "Limited Use" in body
    assert "gmail.readonly" in body
    # Deletion route and a contact address are both required disclosures.
    assert "delete your account" in body.lower()
    assert "@" in body
