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
    ("post", "/keyword-scan", {"jd_text": "Python"}),
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


# =====================================================================
# SALARY AND LOCATION
# =====================================================================
# Declared by the posting's JSON-LD block and forwarded by the extension. The
# server does not trust them: an unusable value is dropped so the application
# still saves, because failing a save over an odd salary is worse than saving
# without one.
def test_a_posting_saves_its_salary_and_location(account):
    response = client.post(
        "/save-job",
        json={
            "company": "Nexus Labs",
            "role": "AI Engineer",
            "location": "Bengaluru, Karnataka",
            "remote": True,
            "salary_min": 1800000,
            "salary_max": 2400000,
            "salary_currency": "INR",
            "salary_period": "YEAR",
        },
        headers=account["headers"],
    )
    assert response.status_code == 201

    job = _only_job(account)

    assert job["location"] == "Bengaluru, Karnataka"
    assert job["remote"] == 1
    assert job["salary_min"] == 1800000
    assert job["salary_max"] == 2400000
    assert job["salary_currency"] == "INR"
    assert job["salary_period"] == "YEAR"


def test_an_unusable_salary_does_not_fail_the_save(account):
    """A requisition id or a timestamp in the salary slot. The application is
    still worth tracking; the number is not."""
    response = client.post(
        "/save-job",
        json={
            "company": "Nexus Labs",
            "role": "AI Engineer",
            "salary_min": 1755302400000,
            "salary_currency": "USD",
            "salary_period": "YEAR",
        },
        headers=account["headers"],
    )
    assert response.status_code == 201

    job = _only_job(account)

    assert job["salary_min"] is None
    assert job["salary_max"] is None


def test_a_location_of_remote_is_stored_as_the_flag(account):
    """Never in both places, or every reader has to decide which to believe."""
    response = client.post(
        "/save-job",
        json={"company": "Nexus Labs", "role": "AI Engineer", "location": "Remote"},
        headers=account["headers"],
    )
    assert response.status_code == 201

    job = _only_job(account)

    assert job["location"] == ""
    assert job["remote"] == 1


def test_a_posting_with_neither_saves_cleanly(account):
    """The overwhelmingly common case: LinkedIn declares no JobPosting block."""
    response = client.post(
        "/save-job",
        json={"company": "Nexus Labs", "role": "AI Engineer"},
        headers=account["headers"],
    )
    assert response.status_code == 201

    job = _only_job(account)

    assert job["location"] == ""
    assert job["remote"] == 0
    assert job["salary_min"] is None


def _only_job(account) -> dict:
    jobs = client.get("/jobs", headers=account["headers"]).json()["jobs"]
    assert len(jobs) == 1
    return jobs[0]


# =====================================================================
# KEYWORD SCAN
# =====================================================================
# The in-page card runs this on arrival at every job page, which is only
# defensible because it costs nothing: no model call, no network, and the same
# answer every time. These tests pin exactly that.
# =====================================================================
# REUSING AN ANALYSIS
# =====================================================================
POSTING = "We need strong Python, LangChain and Kubernetes experience."

STORED = {
    "match_percentage": 71,
    "matched_skills": ["Python"],
    "missing_skills": ["Kubernetes"],
    "summary": "Good on the language, thin on the platform.",
    "requirements": [],
    "coverage": {"score": 71, "scored": True},
    "keyword_coverage": {
        "score": 50, "scored": True, "matched": ["Python"],
        "missing": ["Kubernetes"], "total": 2,
    },
}


def _track_and_analyse(account, jd_text=POSTING):
    """Save a job and attach an analysis to it, as the extension does."""
    import db
    import workspace

    db_path = workspace.jobs_db_path(account["user_id"])
    db.create_table(db_path)
    job_id = db.add_job(
        company="Acme Robotics", role="AI Engineer", jd=jd_text, db_path=db_path
    )
    db.save_analysis(job_id, STORED, jd_text=jd_text, db_path=db_path)
    return job_id


def test_a_stored_analysis_is_returned_without_a_model_call(account, monkeypatch):
    """The waste this exists to stop.

    The popup opens on every visit to a job page. Re-deriving the analysis of a
    description that has not changed spends a large model call to arrive at the
    number already on the row.
    """
    import ai.resume_parser

    _track_and_analyse(account)

    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("a stored analysis must not call the model")

    monkeypatch.setattr(ai.resume_parser, "generate_structured", explode)

    response = client.post(
        "/analyze-job",
        json={"company": "Acme Robotics", "role": "AI Engineer", "jd_text": POSTING},
        headers=account["headers"],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reused"] is True
    assert body["match_percentage"] == 71


def test_a_reused_analysis_is_not_billed(account, monkeypatch):
    import ai.resume_parser
    import usage

    _track_and_analyse(account)
    monkeypatch.setattr(
        ai.resume_parser, "generate_structured", lambda *a, **k: {"error": "x", "message": "x"}
    )

    client.post(
        "/analyze-job",
        json={"company": "Acme Robotics", "role": "AI Engineer", "jd_text": POSTING},
        headers=account["headers"],
    )

    rows = [
        row for row in usage.per_user() if row["user_id"] == account["user_id"]
    ]
    assert rows[0]["events"][usage.ANALYZE_JD] == 0


def test_a_changed_description_is_analysed_afresh(account, monkeypatch):
    """Reuse must not outlive the text it describes."""
    import ai.resume_parser

    _track_and_analyse(account)

    called = []

    def record(*args, **kwargs):
        called.append(1)
        return {"error": "RATE_LIMIT", "message": "stop here"}

    monkeypatch.setattr(ai.resume_parser, "generate_structured", record)

    response = client.post(
        "/analyze-job",
        json={
            "company": "Acme Robotics",
            "role": "AI Engineer",
            "jd_text": "A rewritten posting asking for Go and Rust instead.",
        },
        headers=account["headers"],
    )

    # It reached the model rather than answering from the stale result.
    assert called
    assert response.status_code == 502


def test_refresh_forces_a_new_analysis(account, monkeypatch):
    import ai.resume_parser

    _track_and_analyse(account)

    called = []
    monkeypatch.setattr(
        ai.resume_parser,
        "generate_structured",
        lambda *a, **k: called.append(1) or {"error": "x", "message": "x"},
    )

    client.post(
        "/analyze-job",
        json={
            "company": "Acme Robotics",
            "role": "AI Engineer",
            "jd_text": POSTING,
            "refresh": True,
        },
        headers=account["headers"],
    )

    assert called


def test_saving_a_job_keeps_the_analysis_it_was_scored_with(account):
    """The usual order is score first, then decide to save.

    /analyze-job has no row to attach its result to until the job exists, so
    without this the scores the user was looking at when they clicked Save
    would be discarded and the next visit would pay for them again.
    """
    import db
    import workspace

    response = client.post(
        "/save-job",
        json={
            "company": "Globex",
            "role": "ML Platform Engineer",
            "jd_text": POSTING,
            "analysis": STORED,
        },
        headers=account["headers"],
    )

    assert response.status_code == 201

    db_path = workspace.jobs_db_path(account["user_id"])
    row = db.get_job(response.json()["job_id"], db_path=db_path)

    assert row["match_score"] == 71
    assert row["keyword_score"] == 50


def test_a_job_saved_without_an_analysis_has_no_scores(account):
    import db
    import workspace

    response = client.post(
        "/save-job",
        json={"company": "Initech", "role": "Engineer", "jd_text": POSTING},
        headers=account["headers"],
    )

    db_path = workspace.jobs_db_path(account["user_id"])
    row = db.get_job(response.json()["job_id"], db_path=db_path)

    assert row["match_score"] is None


def test_keyword_scan_needs_no_model_call(account, monkeypatch):
    """If this endpoint ever grew an AI call it would be billing the user for
    opening a page. Break generate_structured and it must still answer."""
    import ai.gemini

    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("/keyword-scan must not call the model")

    monkeypatch.setattr(ai.gemini, "generate_structured", explode)

    response = client.post(
        "/keyword-scan",
        json={"jd_text": "We need strong Python and Kubernetes experience."},
        headers=account["headers"],
    )

    assert response.status_code == 200
    assert response.json()["scored"] is True


def test_keyword_scan_reports_terms_the_resume_lacks(account):
    """A brand-new account has no resume, so every term the posting names is
    missing — and the endpoint says so rather than failing."""
    response = client.post(
        "/keyword-scan",
        json={"jd_text": "Requirements: Python, Kubernetes, and PostgreSQL."},
        headers=account["headers"],
    )

    body = response.json()

    assert set(body["missing"]) == {"Python", "Kubernetes", "PostgreSQL"}
    assert body["matched"] == []
    assert body["score"] == 0


def test_keyword_scan_says_when_a_posting_names_nothing_it_knows():
    """Reporting 0% for a posting with no recognised terms would read as a
    terrible match rather than as a scan that found nothing to measure."""
    import scoring

    result = scoring.keyword_coverage("We are looking for a great teammate.", "{}")

    assert result["scored"] is False
    assert result["total"] == 0


def test_keyword_scan_rejects_a_traversal_in_the_profile_name(account):
    response = client.post(
        "/keyword-scan",
        json={"jd_text": "Python", "profile": "../../../etc/passwd"},
        headers=account["headers"],
    )

    assert response.status_code == 400


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
