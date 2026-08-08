"""Talent-Pilot — job application tracker dashboard.

Streamlit entry point. Run with:  streamlit run app.py
"""

import json
from datetime import date, datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import auth
import db
import ui
import utils
import workspace
from ai import resume_parser
from config import (
    IS_HOSTED,
    JOB_SOURCES,
    REGISTRATION_CLOSED,
    SIGNUP_CODE,
    SYNC_LOG_FILE,
    TOKEN_TTL_DAYS,
    VALID_STATUSES,
    logger,
)
from integrations import gmail_client
from sync_controller import sync_inbox_to_db

st.set_page_config(
    page_title="Talent-Pilot",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

ui.inject_styles()


# =====================================================================
# SESSION
# =====================================================================
def get_signed_in_user() -> auth.User | None:
    data = st.session_state.get("user")
    return auth.User(**data) if data else None


SESSION_COOKIE = "tp_session"


def try_handoff_sign_in() -> bool:
    """Adopt a session handed over from the extension, if one is present.

    The extension opens the dashboard with ?handoff=<single-use code>, which
    is redeemed here so the user is not asked to sign in a second time.
    """
    code = st.query_params.get("handoff")
    if not code:
        return False

    # Cleared immediately so the code never lingers in the address bar,
    # bookmarks, or a shared link.
    st.query_params.clear()

    user = auth.consume_handoff_code(code)
    if user is None:
        st.warning("That sign-in link had already been used or expired. Please sign in.")
        return False

    sign_in(user)
    return True


def try_cookie_sign_in() -> bool:
    """Restore a session from the browser cookie.

    Streamlit's session_state lives in the websocket connection and is lost
    on reload, so without this every refresh would return to the login form.
    """
    try:
        token = st.context.cookies.get(SESSION_COOKIE)
    except Exception:  # noqa: BLE001 - older Streamlit without st.context
        return False

    if not token:
        return False

    user = auth.verify_token(token)
    if user is None:
        return False

    st.session_state.user = {
        "id": user.id,
        "email": user.email,
        "created_at": user.created_at,
    }
    db.create_table(workspace.jobs_db_path(user.id))
    return True


def _write_session_cookie(value: str, max_age: int) -> None:
    """Set the session cookie from inside a Streamlit component.

    Streamlit cannot set cookies server-side, and its component iframe is
    sandboxed without `allow-top-navigation`, so it cannot redirect to an
    endpoint that would. It *is* granted `allow-same-origin`, though, which
    means document.cookie inside the frame writes to the parent's origin.

    The consequence is that this cookie cannot be HttpOnly — cookies written
    by JavaScript never are. It carries a revocable API token rather than
    credentials, and is scoped SameSite=Lax and Secure when hosted.
    """
    attributes = f"path=/; max-age={max_age}; samesite=lax"
    if IS_HOSTED:
        attributes += "; secure"

    components.html(
        "<script>document.cookie = "
        f"{json.dumps(SESSION_COOKIE + '=')} + {json.dumps(value)} + "
        f"{json.dumps('; ' + attributes)};</script>",
        height=0,
    )


def ensure_session_cookie(user: auth.User) -> None:
    """Keep this sign-in alive across page reloads.

    Called on every render rather than once at login: the component has to be
    part of a normally-rendered page to execute, and an st.rerun() straight
    after sign-in would discard it before the browser ran the script. Writing
    the same cookie repeatedly is harmless, and the token is minted once per
    session and cached so the tokens table does not grow on every rerun.
    """
    token = st.session_state.get("session_token")

    if not token:
        token = auth.issue_token(user.id)
        st.session_state.session_token = token

    _write_session_cookie(token, TOKEN_TTL_DAYS * 24 * 3600)


def sign_in(user: auth.User) -> None:
    st.session_state.user = {
        "id": user.id,
        "email": user.email,
        "created_at": user.created_at,
    }
    db.create_table(workspace.jobs_db_path(user.id))
    logger.info("Account %s signed in to the dashboard", user.id)


def sign_out() -> None:
    user = st.session_state.get("user")
    if user:
        logger.info("Account %s signed out of the dashboard", user["id"])

    # Revoke server-side as well as clearing the cookie, so a copied token
    # cannot be replayed after signing out.
    try:
        token = st.context.cookies.get(SESSION_COOKIE)
        if token:
            auth.revoke_token(token)
    except Exception:  # noqa: BLE001 - cookie access is best-effort
        pass

    st.session_state.clear()
    _write_session_cookie("", 0)


# =====================================================================
# AUTH SCREEN
# =====================================================================
def render_auth_screen() -> None:
    _, center, _ = st.columns([1, 1.6, 1])

    with center:
        st.markdown(
            '<div class="auth-header"><h1>🎯 Talent-Pilot</h1>'
            "<p>Track applications, analyse job descriptions, and draft answers with AI.</p></div>",
            unsafe_allow_html=True,
        )
        st.write("")

        sign_in_tab, register_tab = st.tabs(["Sign in", "Create account"])

        with sign_in_tab:
            with st.form("sign_in_form"):
                email = st.text_input("Email", placeholder="you@example.com")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Sign in", use_container_width=True)

            if submitted:
                try:
                    sign_in(auth.authenticate(email, password))
                    st.rerun()
                except auth.RateLimitError as exc:
                    st.warning(str(exc))
                except auth.AuthError as exc:
                    st.error(str(exc))

        with register_tab:
            if REGISTRATION_CLOSED:
                st.info("Registration is closed on this instance.")
            else:
                with st.form("register_form"):
                    email = st.text_input("Email", placeholder="you@example.com")
                    password = st.text_input(
                        "Password",
                        type="password",
                        help=f"At least {auth.MIN_PASSWORD_LENGTH} characters.",
                    )
                    confirm = st.text_input("Confirm password", type="password")

                    # Only shown when the instance actually requires one, so
                    # local users never see a field they cannot fill.
                    signup_code = (
                        st.text_input(
                            "Invite code",
                            help="Required on this instance.",
                        )
                        if SIGNUP_CODE
                        else ""
                    )

                    submitted = st.form_submit_button(
                        "Create account", use_container_width=True
                    )

                if submitted:
                    if password != confirm:
                        st.error("Those passwords do not match.")
                    else:
                        try:
                            sign_in(auth.register(email, password, signup_code))
                            st.rerun()
                        except auth.AuthError as exc:
                            st.error(str(exc))

        st.caption(
            "Your data stays on this machine — each account gets its own local database."
        )


# =====================================================================
# SIDEBAR
# =====================================================================
def render_sidebar(user: auth.User) -> str | None:
    """Render the sidebar and return the selected profile filename."""
    st.sidebar.title("🎯 Talent-Pilot")
    ui.account_chip(user.email)

    if st.sidebar.button("Sign out", use_container_width=True):
        sign_out()
        st.rerun()

    st.sidebar.divider()

    # --- Active profile -------------------------------------------------
    st.sidebar.subheader("Active profile")
    profiles = workspace.list_profiles(user.id)

    if profiles:
        selected = st.sidebar.selectbox(
            "Resume used for AI analysis",
            profiles,
            format_func=utils.profile_display_name,
            label_visibility="collapsed",
        )
    else:
        st.sidebar.info("Upload a resume in **Profiles** to enable AI features.")
        selected = None

    st.sidebar.divider()

    # --- Gmail sync -----------------------------------------------------
    st.sidebar.subheader("Inbox sync")
    st.sidebar.caption(f"Last synced: {utils.get_last_sync(user.id)}")

    if not gmail_client.has_credentials_file():
        st.sidebar.caption(
            "⚠️ `credentials.json` not found — add Google OAuth credentials to enable sync."
        )
    elif not gmail_client.is_connected(user.id):
        render_gmail_connect(user)
    else:
        if st.sidebar.button("🔄 Sync inbox now", use_container_width=True):
            run_sync(user)

        if st.sidebar.button("Disconnect Gmail", use_container_width=True):
            gmail_client.disconnect(user.id)
            st.rerun()

    return selected


def render_gmail_connect(user: auth.User) -> None:
    """Offer the right Gmail consent flow for how the app is running.

    Locally, a button can open a browser on this machine. When hosted, that is
    impossible — the browser would open on the server — so the user follows a
    link and Google redirects them back with a code.
    """
    if not IS_HOSTED:
        if st.sidebar.button("Connect Gmail", use_container_width=True):
            with st.spinner("Opening Google sign-in in your browser…"):
                try:
                    gmail_client.authenticate_gmail(user.id, allow_interactive=True)
                    st.sidebar.success("Gmail connected.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    logger.error("Gmail connect failed for user %s: %s", user.id, exc)
                    st.sidebar.error(str(exc))
        return

    try:
        auth_url = gmail_client.build_auth_url(user.id)
    except gmail_client.GmailAuthError as exc:
        st.sidebar.error(str(exc))
        return

    st.sidebar.link_button("Connect Gmail", auth_url, use_container_width=True)
    st.sidebar.caption("You'll be returned here after approving access.")


def handle_gmail_callback(user: auth.User) -> None:
    """Complete a hosted Gmail consent redirect, if one just landed."""
    params = st.query_params

    # A handoff code also arrives as ?code=, so only treat this as an OAuth
    # callback when Google's state parameter is present too.
    if "code" not in params or "state" not in params:
        return

    code = params["code"]
    state = params["state"]

    # Clear the query string either way so a refresh cannot replay the code.
    st.query_params.clear()

    try:
        # The state is validated inside complete_auth against the value
        # recorded in this user's workspace when the link was built.
        gmail_client.complete_auth(user.id, code, state)
        st.sidebar.success("Gmail connected.")
        st.rerun()
    except gmail_client.GmailAuthError as exc:
        logger.warning("Gmail callback failed for user %s: %s", user.id, exc)
        st.sidebar.error(str(exc))


def run_sync(user: auth.User) -> None:
    """Run an inbox sync with live progress in the sidebar."""
    status_box = st.sidebar.status("Syncing inbox…", expanded=True)

    try:
        summary = sync_inbox_to_db(user.id, progress_callback=status_box.write)
        status_box.update(label="Sync complete", state="complete", expanded=False)
        st.sidebar.success(
            f"{summary['updated']} updated · {summary['created']} added · "
            f"{summary['skipped']} skipped"
        )
        st.rerun()
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        logger.error("Inbox sync failed for user %s: %s", user.id, exc)
        status_box.update(label="Sync failed", state="error")
        st.sidebar.error(str(exc))


# =====================================================================
# TAB: DASHBOARD
# =====================================================================
def render_dashboard(user: auth.User, db_path) -> None:
    jobs = db.get_all_jobs(db_path=db_path)

    if not jobs:
        st.info("No applications yet. Add your first one in the **Add application** tab.")
        return

    ui.render_metrics(db.get_stats(db_path=db_path))
    st.write("")

    frame = pd.DataFrame(jobs)

    # --- Filters --------------------------------------------------------
    search_col, status_col = st.columns([2, 3])
    with search_col:
        search = st.text_input(
            "Search", placeholder="Company or role…", label_visibility="collapsed"
        )
    with status_col:
        chosen = st.multiselect(
            "Status",
            VALID_STATUSES,
            format_func=ui.status_label,
            placeholder="All statuses",
            label_visibility="collapsed",
        )

    filtered = frame
    if search:
        haystack = filtered["company"].fillna("") + " " + filtered["role"].fillna("")
        filtered = filtered[haystack.str.contains(search, case=False, na=False)]
    if chosen:
        filtered = filtered[filtered["status"].isin(chosen)]

    if filtered.empty:
        st.warning("No applications match those filters.")
        return

    display = filtered.assign(status=filtered["status"].map(ui.status_label))[
        ["company", "role", "status", "date_applied", "source", "link"]
    ]

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "company": st.column_config.TextColumn("Company", width="medium"),
            "role": st.column_config.TextColumn("Role", width="large"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "date_applied": st.column_config.TextColumn("Applied", width="small"),
            "source": st.column_config.TextColumn("Source", width="small"),
            "link": st.column_config.LinkColumn("Posting", display_text="Open ↗"),
        },
    )
    st.caption(f"Showing {len(filtered)} of {len(frame)} applications.")

    st.divider()
    render_job_editor(jobs, db_path)


def render_job_editor(jobs: list[dict], db_path) -> None:
    """Status updates and deletion for a single application."""
    st.subheader("Update an application")

    options = {
        f"{job['company']} — {job['role']}  ({ui.status_label(job['status'])})": job["id"]
        for job in jobs
    }

    label = st.selectbox("Application", list(options), label_visibility="collapsed")
    job_id = options[label]
    current = next(job for job in jobs if job["id"] == job_id)

    status_col, button_col = st.columns([3, 1])
    with status_col:
        new_status = st.selectbox(
            "New status",
            VALID_STATUSES,
            index=VALID_STATUSES.index(current["status"])
            if current["status"] in VALID_STATUSES
            else 0,
            format_func=ui.status_label,
            label_visibility="collapsed",
        )
    with button_col:
        if st.button("Update", use_container_width=True):
            db.update_status(job_id, new_status, db_path=db_path)
            st.success("Status updated.")
            st.rerun()

    render_timeline(job_id, db_path)

    if current.get("notes"):
        with st.expander("Notes"):
            st.text(current["notes"])

    with st.expander("Delete this application"):
        st.warning(f"This permanently removes **{current['company']} — {current['role']}**.")
        if st.button("Delete permanently", type="secondary"):
            db.delete_job(job_id, db_path=db_path)
            st.rerun()


def render_timeline(job_id: int, db_path) -> None:
    """Show how an application moved through the hiring stages.

    The jobs table only knows where something stands *now*, which cannot
    answer "how long have they sat on this?" or "when did it go quiet?".
    """
    history = db.get_status_history(job_id, db_path=db_path)

    if not history:
        return

    with st.expander(f"Stage timeline ({len(history)} events)", expanded=False):
        for entry in history:
            stamp = _readable_timestamp(entry["occurred_at"])
            arrow = (
                f"{ui.status_label(entry['from_status'])} → {ui.status_label(entry['to_status'])}"
                if entry["from_status"]
                else ui.status_label(entry["to_status"])
            )

            if entry["applied"]:
                st.markdown(f"**{stamp}** · {arrow}  \n*via {entry['source']}*")
            else:
                # An observation the rank guard declined to apply. Showing it
                # is what makes a surprising status explainable instead of
                # looking like the sync simply missed an email.
                st.markdown(
                    f"**{stamp}** · ~~{arrow}~~ *(not applied — would move backwards)*  \n"
                    f"*via {entry['source']}*"
                )

            if entry["reason"]:
                st.caption(entry["reason"])

        st.caption(_duration_summary(history))


def _readable_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return value or "—"


def _duration_summary(history: list[dict]) -> str:
    """How long this application has been running, and how long since it moved."""
    applied = [entry for entry in history if entry["applied"]]
    if not applied:
        return ""

    try:
        first = datetime.fromisoformat(applied[0]["occurred_at"])
        last = datetime.fromisoformat(applied[-1]["occurred_at"])
    except (ValueError, TypeError):
        return ""

    now = datetime.now(first.tzinfo)
    return (
        f"Open {(now - first).days} days · "
        f"{(now - last).days} days since the last change"
    )


# =====================================================================
# TAB: ADD APPLICATION
# =====================================================================
def render_add_form(db_path, profiles: list[str]) -> None:
    st.subheader("Add an application")

    with st.form("add_job_form", clear_on_submit=True):
        left, right = st.columns(2)
        with left:
            company = st.text_input("Company *")
            job_status = st.selectbox(
                "Status", VALID_STATUSES, format_func=ui.status_label
            )
            source = st.selectbox("Source", JOB_SOURCES)
        with right:
            role = st.text_input("Role *")
            date_applied = st.date_input("Date applied", value=date.today())
            resume_used = st.selectbox(
                "Resume used",
                ["None"] + profiles,
                format_func=lambda name: "None"
                if name == "None"
                else utils.profile_display_name(name),
            )

        link = st.text_input("Job posting link")
        jd = st.text_area("Job description", height=160)
        notes = st.text_area("Notes", height=80)

        submitted = st.form_submit_button("Add application", use_container_width=True)

    if not submitted:
        return

    try:
        db.add_job(
            company=company,
            role=role,
            jd=jd,
            status=job_status,
            date_applied=date_applied.strftime("%Y-%m-%d"),
            link=link,
            notes=notes,
            source=source,
            resume_used=None if resume_used == "None" else resume_used,
            db_path=db_path,
        )
        st.success(f"Added {company} — {role}.")
        st.rerun()
    except db.DuplicateJobError:
        st.warning(f"You are already tracking {company} — {role}.")
    except ValueError as exc:
        st.error(str(exc))


# =====================================================================
# TAB: ANALYZER
# =====================================================================
@st.cache_data(show_spinner=False)
def cached_analysis(jd_text: str, resume_string: str) -> dict:
    """Cache by content, so re-analysing the same pairing is free."""
    return resume_parser.analyze_jd(jd_text, resume_string)


def render_analyzer(user: auth.User, db_path, selected_profile: str | None) -> None:
    st.subheader("Job description analyzer")

    if not selected_profile:
        st.info("Upload a resume profile first — the analyzer compares against it.")
        return

    jobs = [job for job in db.get_all_jobs(db_path=db_path) if job.get("jd")]

    if not jobs:
        st.info("None of your applications have a job description saved yet.")
        return

    options = {f"{job['company']} — {job['role']}": job["id"] for job in jobs}
    label = st.selectbox("Application to analyze", list(options))
    job = next(j for j in jobs if j["id"] == options[label])

    st.caption(f"Comparing against **{utils.profile_display_name(selected_profile)}**")

    if not st.button("Analyze match", use_container_width=True):
        return

    resume = utils.load_profile(user.id, selected_profile)

    with st.spinner("Gemini is comparing the job description to your resume…"):
        result = cached_analysis(job["jd"], json.dumps(resume))

    if "error" in result:
        st.error(result["message"])
        return

    st.write("")
    score_col, summary_col = st.columns([1, 3])
    with score_col:
        st.metric("Match score", f"{result['match_percentage']}%")
    with summary_col:
        st.info(result["summary"])

    matched_col, missing_col = st.columns(2)
    with matched_col:
        st.markdown("**✅ Matched skills**")
        for skill in result["matched_skills"] or ["—"]:
            st.markdown(f"- {skill}")
    with missing_col:
        st.markdown("**❌ Missing skills**")
        for skill in result["missing_skills"] or ["—"]:
            st.markdown(f"- {skill}")


# =====================================================================
# TAB: PROFILES
# =====================================================================
def render_profiles(user: auth.User) -> None:
    st.subheader("Resume profiles")
    st.caption(
        "Upload a PDF resume and Gemini converts it into a structured profile the "
        "AI features use. Keep one per target role."
    )

    with st.form("upload_profile_form"):
        name = st.text_input("Profile name", placeholder="e.g. AI Engineer")
        uploaded = st.file_uploader("PDF resume", type=["pdf"])
        submitted = st.form_submit_button("Convert and save", use_container_width=True)

    if submitted:
        if not uploaded:
            st.error("Please choose a PDF file.")
        elif not name.strip():
            st.error("Please name this profile.")
        else:
            convert_and_save_profile(user, name, uploaded)

    st.divider()

    profiles = workspace.list_profiles(user.id)
    if not profiles:
        st.info("No profiles yet.")
        return

    st.markdown("**Saved profiles**")
    for filename in profiles:
        row, actions = st.columns([4, 1])
        row.markdown(f"📄 {utils.profile_display_name(filename)}")
        if actions.button("Delete", key=f"del_{filename}", use_container_width=True):
            utils.delete_profile(user.id, filename)
            st.rerun()

        with st.expander("Preview", expanded=False):
            st.json(utils.load_profile(user.id, filename), expanded=False)


def convert_and_save_profile(user: auth.User, name: str, uploaded) -> None:
    with st.status("Parsing resume…", expanded=True) as status:
        try:
            status.write("Extracting text from the PDF…")
            raw_text = utils.extract_pdf_text(uploaded)

            status.write("Gemini is structuring the data…")
            structured = resume_parser.convert_pdf_to_json(raw_text)

            if "error" in structured:
                status.update(label="Conversion failed", state="error")
                st.error(structured["message"])
                return

            filename = utils.save_profile(user.id, name, structured)
            status.update(label=f"Saved {filename}", state="complete")
            st.success(f"Created profile **{utils.profile_display_name(filename)}**.")
            st.rerun()

        except utils.ResumeReadError as exc:
            status.update(label="Could not read that PDF", state="error")
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            logger.error("PDF parse failed for user %s: %s", user.id, exc)
            status.update(label="Could not read that PDF", state="error")
            st.error(f"Failed to process the PDF: {exc}")


# =====================================================================
# TAB: ACTIVITY
# =====================================================================
def render_activity(user: auth.User, db_path) -> None:
    """Recent sync decisions and stage changes.

    Hosted, the log files sit on a VM behind SSH, which in practice means
    nobody ever reads them. Every automated status change is an unattended
    decision about the user's data, so it belongs somewhere they can actually
    look.
    """
    st.subheader("Recent stage changes")

    changes = db.get_recent_status_changes(limit=40, db_path=db_path)

    if not changes:
        st.info("No stage changes recorded yet.")
    else:
        rows = [
            {
                "When": _readable_timestamp(entry["occurred_at"]),
                "Application": f"{entry['company']} — {entry['role']}",
                "Change": (
                    f"{entry['from_status']} → {entry['to_status']}"
                    if entry["from_status"]
                    else entry["to_status"]
                ),
                "Applied": "Yes" if entry["applied"] else "No (would move backwards)",
                "Source": entry["source"],
                "Detail": entry["reason"] or "",
            }
            for entry in changes
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Inbox sync log")
    st.caption(
        f"Written to `{SYNC_LOG_FILE}`. Every email the sync considered, and "
        "why it was or was not acted on."
    )

    lines = _tail(SYNC_LOG_FILE, 200)

    if not lines:
        st.info("No syncs have run yet.")
        return

    only_decisions = st.checkbox(
        "Show only decisions (hide progress lines)", value=True
    )

    if only_decisions:
        lines = [
            line
            for line in lines
            if any(tag in line for tag in ("SKIP", "UPDATED", "CREATED", "NOTED", "ERROR"))
        ]

    st.code("\n".join(lines[-120:]) or "No matching lines.", language="log")


def _tail(path, limit: int) -> list[str]:
    """Last ``limit`` lines of a log file, newest last."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in handle.readlines()[-limit:]]
    except (FileNotFoundError, OSError):
        return []


# =====================================================================
# TAB: SETTINGS
# =====================================================================
def render_settings(user: auth.User) -> None:
    st.subheader("Account")
    st.caption(f"Signed in as **{user.email}**")

    with st.form("change_password_form"):
        st.markdown("**Change password**")
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Update password")

    if submitted:
        if new != confirm:
            st.error("Those passwords do not match.")
        else:
            try:
                auth.change_password(user.id, current, new)
                st.success("Password updated. Other signed-in devices were logged out.")
            except auth.AuthError as exc:
                st.error(str(exc))

    st.divider()
    st.markdown("**Browser extension**")
    st.caption(
        "Sign in from the extension popup with these same credentials. "
        "Start the API with `uvicorn api.server:app --port 8000` first."
    )


# =====================================================================
# MAIN
# =====================================================================
def main() -> None:
    user = get_signed_in_user()

    # Restore order matters: an explicit handoff wins over an existing
    # cookie, so opening the dashboard from a second account works.
    if user is None and try_handoff_sign_in():
        user = get_signed_in_user()
    if user is None and try_cookie_sign_in():
        user = get_signed_in_user()

    if user is None:
        render_auth_screen()
        return

    ensure_session_cookie(user)

    db_path = workspace.jobs_db_path(user.id)
    db.create_table(db_path)

    # Runs before the sidebar so a completed connection shows immediately.
    handle_gmail_callback(user)

    selected_profile = render_sidebar(user)
    profiles = workspace.list_profiles(user.id)

    st.title("Job Application Tracker")

    (
        dashboard_tab,
        add_tab,
        analyzer_tab,
        profiles_tab,
        activity_tab,
        settings_tab,
    ) = st.tabs(
        [
            "📊 Dashboard",
            "➕ Add application",
            "🧠 Analyzer",
            "📄 Profiles",
            "📜 Activity",
            "⚙️ Settings",
        ]
    )

    with dashboard_tab:
        render_dashboard(user, db_path)
    with add_tab:
        render_add_form(db_path, profiles)
    with analyzer_tab:
        render_analyzer(user, db_path, selected_profile)
    with profiles_tab:
        render_profiles(user)
    with activity_tab:
        render_activity(user, db_path)
    with settings_tab:
        render_settings(user)


main()
