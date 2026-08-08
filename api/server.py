"""Local API used by the Chrome extension.

Identity always comes from the bearer token, never from the request body — a
caller cannot act on another account by naming its email address.
"""

import io
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import autofill
import db
import job_fields
import utils
import workspace
from ai.resume_parser import (
    analyze_jd,
    convert_pdf_to_json,
    generate_smart_answer,
    save_answer_to_memory,
)
from config import (
    ALLOWED_ORIGIN_REGEX,
    DASHBOARD_URL,
    DEFAULT_STATUS,
    LOG_DIR,
    PUBLIC_URL,
    REGISTRATION_CLOSED,
    SIGNUP_CODE,
    TOKEN_TTL_DAYS,
    TRUST_PROXY_HEADERS,
    VALID_STATUSES,
    logger,
)

# Identifies this specific service. The extension checks for it so that
# "something else is listening on port 8000" produces a useful message rather
# than a confusing HTTP error from an unrelated server.
SERVICE_NAME = "talent-pilot-api"

app = FastAPI(
    title="Job AI Assistant API",
    version="2.0.0",
    description="Backend for the AI Job Copilot browser extension.",
)

# Only the extension may call this server. A regex keeps ordinary web pages —
# the real risk against a service listening on localhost — from reaching it,
# without hardcoding an extension id that changes between installs.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

bearer_scheme = HTTPBearer(auto_error=False)


# =====================================================================
# LOGGING
# =====================================================================
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with a correlation id, outcome, and duration.

    Without this, a failing extension call left no trace on the server side,
    which made "is it even reaching this process?" impossible to answer.
    """
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()

    logger.debug(
        "[%s] --> %s %s from %s",
        request_id,
        request.method,
        request.url.path,
        request.client.host if request.client else "unknown",
    )

    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        # exc_info gives the full traceback in the log file, while the client
        # still gets a generic message.
        logger.exception(
            "[%s] !!! %s %s failed after %.0fms",
            request_id,
            request.method,
            request.url.path,
            elapsed,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. See logs/app.log."},
        )

    elapsed = (time.perf_counter() - started) * 1000
    # Every request is logged at INFO. On a local single-user service the
    # volume is trivial, and an empty log is itself the answer to "is the
    # extension even reaching this process?".
    level = logger.warning if response.status_code >= 400 else logger.info
    level(
        "[%s] <-- %s %s %s (%.0fms)",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
    )

    response.headers["X-Request-ID"] = request_id
    return response


@app.on_event("startup")
def log_startup() -> None:
    # The bound address is not known here — uvicorn prints that itself — so
    # this reports only what this process actually controls.
    logger.info("=" * 62)
    logger.info("Talent-Pilot API v%s ready (service=%s)", app.version, SERVICE_NAME)
    logger.info("Logs:            %s", LOG_DIR / "app.log")
    logger.info("Allowed origins: %s", ALLOWED_ORIGIN_REGEX)
    logger.info("Trust proxy hdr: %s", TRUST_PROXY_HEADERS)

    if REGISTRATION_CLOSED:
        logger.info("Registration:    CLOSED")
    elif SIGNUP_CODE:
        logger.info("Registration:    invite code required")
    else:
        logger.warning(
            "Registration:    OPEN to anyone who can reach this server. "
            "Set SIGNUP_CODE before exposing it publicly."
        )

    logger.info("Reminder: the Streamlit dashboard is a separate server.")
    logger.info("=" * 62)


# =====================================================================
# AUTH DEPENDENCY
# =====================================================================
def current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> auth.User:
    """Resolve the bearer token to an account, or reject the request."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = auth.verify_token(credentials.credentials)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


# =====================================================================
# REQUEST / RESPONSE MODELS
# =====================================================================
class CredentialsRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(CredentialsRequest):
    # Only required when SIGNUP_CODE is configured (i.e. hosted instances).
    signup_code: str = Field(default="", max_length=200)


class AuthResponse(BaseModel):
    token: str
    email: str
    user_id: int


class JobData(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(default="", max_length=50_000)
    link: str = Field(default="", max_length=2000)
    profile: Optional[str] = Field(default=None, max_length=255)


class CheckJobRequest(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    company: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)
    jd_text: str = Field(default="", max_length=50_000)
    profile: Optional[str] = Field(default=None, max_length=255)


class SaveAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=20_000)


class AutofillAnswersRequest(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)


class CustomAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=2000)


class StatusUpdateRequest(BaseModel):
    status: str


# =====================================================================
# HEALTH
# =====================================================================
@app.get("/health")
def health() -> dict:
    """Liveness plus service identity.

    ``service`` lets a client confirm it is talking to this API and not to
    whatever else happens to be bound to the port.
    """
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": app.version,
        # So the extension's "Dashboard" link points at the right place
        # whether this is a laptop or a hosted instance.
        "dashboard_url": PUBLIC_URL or DASHBOARD_URL,
        # Lets a client show or hide the invite-code field instead of making
        # the user discover the requirement by failing.
        "registration": {
            "open": not REGISTRATION_CLOSED,
            "invite_required": bool(SIGNUP_CODE),
        },
    }


# =====================================================================
# AUTHENTICATION
# =====================================================================
@app.post("/auth/register", response_model=AuthResponse, status_code=201)
def register(payload: RegisterRequest, request: Request) -> AuthResponse:
    client_ip = _client_ip(request)

    # Registration is rate limited on the source address too, so an invite
    # code cannot be brute-forced.
    if auth.is_locked_out(f"ip:{client_ip}"):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Please try again later."
        )

    try:
        user = auth.register(payload.email, payload.password, payload.signup_code)
    except auth.AuthError as exc:
        logger.info("Registration rejected for %r: %s", payload.email, exc)
        auth.record_failed_attempt(f"ip:{client_ip}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Give the new account a workspace immediately so later calls never race.
    db.create_table(workspace.jobs_db_path(user.id))
    logger.info("Account %s registered via API from %s", user.id, client_ip)

    return AuthResponse(
        token=auth.issue_token(user.id), email=user.email, user_id=user.id
    )


@app.post("/auth/login", response_model=AuthResponse)
def login(payload: CredentialsRequest, request: Request) -> AuthResponse:
    try:
        user = auth.authenticate(
            payload.email, payload.password, client_ip=_client_ip(request)
        )
    except auth.RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    db.create_table(workspace.jobs_db_path(user.id))
    logger.info("Account %s signed in via API", user.id)

    return AuthResponse(
        token=auth.issue_token(user.id), email=user.email, user_id=user.id
    )


@app.post("/auth/logout", status_code=204)
def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    if credentials:
        auth.revoke_token(credentials.credentials)


@app.get("/auth/me")
def me(user: auth.User = Depends(current_user)) -> dict:
    return {"user_id": user.id, "email": user.email}


# Name of the cookie that keeps a dashboard session alive across reloads.
SESSION_COOKIE = "tp_session"


@app.get("/auth/set-session")
def set_session(code: str) -> RedirectResponse:
    """Redeem a handoff code, set a session cookie, and land on the dashboard.

    Streamlit can read cookies but cannot set them, so this endpoint does it.
    Both run on the same origin behind the reverse proxy, so the cookie set
    here is visible to the dashboard.
    """
    target = PUBLIC_URL or DASHBOARD_URL
    user = auth.consume_handoff_code(code)

    if user is None:
        return RedirectResponse(url=f"{target}/?session=expired", status_code=303)

    token = auth.issue_token(user.id)
    response = RedirectResponse(url=f"{target}/", status_code=303)

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=TOKEN_TTL_DAYS * 24 * 3600,
        httponly=True,           # unreadable from page JavaScript
        secure=bool(PUBLIC_URL),  # only over HTTPS when hosted
        samesite="lax",
        path="/",
    )

    logger.info("Session cookie issued for account %s", user.id)
    return response


@app.get("/auth/end-session")
def end_session(request: Request) -> RedirectResponse:
    """Revoke the session token and clear the cookie."""
    target = PUBLIC_URL or DASHBOARD_URL

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth.revoke_token(token)

    response = RedirectResponse(url=f"{target}/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.post("/auth/handoff")
def handoff(user: auth.User = Depends(current_user)) -> dict:
    """Mint a single-use code so the dashboard can adopt this session.

    Lets the extension open the dashboard already signed in, instead of
    making the user authenticate a second time.
    """
    code = auth.issue_handoff_code(user.id)
    target = PUBLIC_URL or DASHBOARD_URL

    return {
        "code": code,
        "url": f"{target}/?handoff={code}",
        "expires_in": auth.HANDOFF_TTL_SECONDS,
    }


# =====================================================================
# PROFILES
# =====================================================================
@app.get("/profiles")
def get_profiles(user: auth.User = Depends(current_user)) -> dict:
    """List the signed-in user's resume profiles."""
    names = workspace.list_profiles(user.id)
    return {
        "profiles": [
            {"filename": name, "label": utils.profile_display_name(name)}
            for name in names
        ]
    }


@app.post("/profiles/upload", status_code=201)
async def upload_profile(
    file: UploadFile = File(...),
    name: str = Form(...),
    user: auth.User = Depends(current_user),
) -> dict:
    """Parse an uploaded PDF resume into a structured profile.

    Lets the extension onboard a resume without switching to the dashboard.
    """
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    contents = await file.read()

    if not contents:
        raise HTTPException(status_code=400, detail="That file is empty.")
    if len(contents) > utils.MAX_RESUME_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"That file is larger than {utils.MAX_RESUME_BYTES // (1024 * 1024)} MB.",
        )

    try:
        raw_text = utils.extract_pdf_text(io.BytesIO(contents))
    except utils.ResumeReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    structured = convert_pdf_to_json(raw_text)

    if "error" in structured:
        raise HTTPException(status_code=502, detail=structured["message"])

    filename = utils.save_profile(user.id, name, structured)

    # Pre-fill the questionnaire from what the resume already says, so the user
    # confirms a mostly-complete form instead of typing their own phone number
    # in again. Existing answers are never overwritten.
    autofill.seed_from_resume(user.id, structured)

    logger.info("Profile %s uploaded via extension by user %s", filename, user.id)

    return {
        "message": "Profile created.",
        "filename": filename,
        "label": utils.profile_display_name(filename),
        "setup": autofill.completeness(user.id),
    }


@app.delete("/profiles/{filename}", status_code=204)
def delete_profile(filename: str, user: auth.User = Depends(current_user)) -> None:
    """Remove one of the signed-in user's profiles."""
    if not utils.delete_profile(user.id, _validated_profile(user.id, filename)):
        raise HTTPException(status_code=404, detail="Profile not found.")


# =====================================================================
# JOBS
# =====================================================================
@app.get("/jobs")
def list_jobs(user: auth.User = Depends(current_user)) -> dict:
    db_path = workspace.jobs_db_path(user.id)
    db.create_table(db_path)
    return {"jobs": db.get_all_jobs(db_path=db_path)}


@app.post("/check-job")
def check_job(payload: CheckJobRequest, user: auth.User = Depends(current_user)) -> dict:
    """Report whether this company+role is already tracked."""
    exists, job_status = db.check_if_applied(
        payload.company, payload.role, db_path=workspace.jobs_db_path(user.id)
    )
    return {"exists": exists, "status": job_status}


@app.post("/save-job", status_code=201)
def save_job(job: JobData, user: auth.User = Depends(current_user)) -> dict:
    db_path = workspace.jobs_db_path(user.id)
    db.create_table(db_path)

    # The scraper reads untrusted markup, so a job title arriving in `company`
    # is a real outcome rather than a hypothetical one. It has to be caught
    # here as well as in the extension: a stale extension build, or any other
    # client, would otherwise write a row that no later email can match.
    try:
        company = job_fields.validate_company(job.company, job.role)
        role = job_fields.validate_role(job.role)
    except job_fields.InvalidJobField as exc:
        logger.warning(
            "Rejected job from user %s: company=%r role=%r - %s",
            user.id, job.company, job.role, exc,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        job_id = db.add_job(
            company=company,
            role=role,
            jd=job.jd_text,
            status=DEFAULT_STATUS,
            link=job.link,
            notes="Added via browser extension",
            source="Web Extension",
            resume_used=_validated_profile(user.id, job.profile),
            db_path=db_path,
        )
    except db.DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("Extension saved job #%s for user %s", job_id, user.id)
    return {"message": "Job saved successfully.", "job_id": job_id}


@app.patch("/jobs/{job_id}/status")
def update_job_status(
    job_id: int, payload: StatusUpdateRequest, user: auth.User = Depends(current_user)
) -> dict:
    if payload.status.upper() not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of: {', '.join(VALID_STATUSES)}",
        )

    updated = db.update_status(
        job_id, payload.status, db_path=workspace.jobs_db_path(user.id)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Job not found.")

    return {"message": "Status updated."}


# =====================================================================
# AI
# =====================================================================
@app.post("/analyze-job")
def analyze_job(job: JobData, user: auth.User = Depends(current_user)) -> dict:
    """Score the signed-in user's resume against a job description."""
    resume = utils.load_profile(user.id, _validated_profile(user.id, job.profile))
    analysis = analyze_jd(job.jd_text, json.dumps(resume))

    if "error" in analysis:
        raise HTTPException(status_code=502, detail=analysis["message"])

    return analysis


@app.post("/generate-answer")
def generate_answer(req: AnswerRequest, user: auth.User = Depends(current_user)) -> dict:
    resume = utils.load_profile(user.id, _validated_profile(user.id, req.profile))

    result = generate_smart_answer(
        user_id=user.id,
        question=req.question,
        company=req.company,
        role=req.role,
        jd_text=req.jd_text,
        active_resume_str=json.dumps(resume),
    )

    if "error" in result:
        raise HTTPException(status_code=502, detail=result["message"])

    return result


@app.post("/save-answer")
def save_answer(req: SaveAnswerRequest, user: auth.User = Depends(current_user)) -> dict:
    filename = save_answer_to_memory(user.id, req.question, req.answer)

    # Short answers also join the autofill bank, so a question answered once
    # with AI is suggested instantly — and for free — the next time it appears.
    # Long-form essays are deliberately excluded: they are tailored per
    # application, and replaying one verbatim would be worse than redrafting.
    saved_for_autofill = False
    if len(req.answer) <= autofill.MAX_ANSWER_LENGTH:
        try:
            autofill.add_custom(user.id, req.question, req.answer)
            saved_for_autofill = True
        except ValueError as exc:
            logger.info("Not adding answer to autofill for user %s: %s", user.id, exc)

    return {
        "message": "Saved to your memory bank.",
        "file": filename,
        "reusable": saved_for_autofill,
    }


# =====================================================================
# AUTOFILL ANSWER BANK
# =====================================================================
@app.get("/autofill")
def get_autofill(user: auth.User = Depends(current_user)) -> dict:
    """Matching rules for the signed-in user, consumed by the content script.

    This is what replaced the bundled rules.js. The extension holds no personal
    data of its own; it asks for the current user's answers on demand, so one
    build serves everybody.
    """
    return {
        "rules": autofill.build_rules(user.id),
        "completeness": autofill.completeness(user.id),
    }


@app.get("/autofill/questions")
def get_autofill_questions(user: auth.User = Depends(current_user)) -> dict:
    """The catalogue plus the user's current answers, for the setup screen."""
    bank = autofill.load(user.id)

    return {
        "groups": autofill.GROUPS,
        "fields": [field.as_dict() for field in autofill.FIELDS],
        "answers": bank["answers"],
        "custom": bank["custom"],
        "completeness": autofill.completeness(user.id),
    }


@app.post("/autofill/answers")
def save_autofill_answers(
    req: AutofillAnswersRequest, user: auth.User = Depends(current_user)
) -> dict:
    autofill.set_answers(user.id, req.answers)
    return {"message": "Answers saved.", "completeness": autofill.completeness(user.id)}


@app.post("/autofill/custom", status_code=201)
def add_autofill_custom(
    req: CustomAnswerRequest, user: auth.User = Depends(current_user)
) -> dict:
    try:
        autofill.add_custom(user.id, req.question, req.answer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"message": "Saved.", "completeness": autofill.completeness(user.id)}


# =====================================================================
# HELPERS
# =====================================================================
def _client_ip(request: Request) -> str:
    """Best-effort source address for rate limiting.

    ``X-Forwarded-For`` is only consulted when TRUST_PROXY_HEADERS is on,
    because a client can set that header freely — honouring it on a
    directly-exposed server would let an attacker defeat the per-IP limit by
    inventing a new address on every request.
    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most entry is the original client; the rest are proxies.
            return forwarded.split(",")[0].strip()

    return request.client.host if request.client else "unknown"


def _validated_profile(user_id: int, filename: Optional[str]) -> Optional[str]:
    """Reject a profile name that is anything other than a plain filename.

    Without this, a crafted ``profile`` value would be joined straight onto a
    filesystem path and could read files outside the workspace. Rejecting
    rather than silently rewriting also keeps the stored ``resume_used`` value
    equal to the file that will actually be loaded later.
    """
    if not filename:
        return None

    if filename != Path(filename).name or filename in {".", ".."}:
        logger.warning("Blocked unsafe profile path %r from user %s", filename, user_id)
        raise HTTPException(status_code=400, detail="Invalid profile name.")

    try:
        workspace.profile_path(user_id, filename)
    except workspace.UnsafePathError:
        logger.warning("Blocked unsafe profile path %r from user %s", filename, user_id)
        raise HTTPException(status_code=400, detail="Invalid profile name.") from None

    return filename
