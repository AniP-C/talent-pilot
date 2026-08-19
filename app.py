"""Talent-Pilot — job application tracker dashboard.

Streamlit entry point. Run with:  streamlit run app.py
"""

import json
from datetime import date, datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import auth
import autofill
import db
import posting
import scoring
import ui
import usage
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
    # Free, but it is what makes "active accounts" answerable — a signup count
    # includes everyone who never came back.
    usage.record(user.id, usage.SIGN_IN, source="dashboard")
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

            render_password_recovery()

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
                            user = auth.register(email, password, signup_code)
                            # Issued now rather than offered later in Settings:
                            # recovery codes are only any use if they exist
                            # *before* the password is forgotten, and nobody
                            # goes looking for them while they still remember
                            # it. Held in session state so they survive the
                            # rerun and can be shown once, on the way in.
                            st.session_state["new_recovery_codes"] = (
                                auth.issue_recovery_codes(user.id)
                            )
                            sign_in(user)
                            st.rerun()
                        except auth.AuthError as exc:
                            st.error(str(exc))

        st.caption(
            "Your data stays on this machine — each account gets its own local database."
        )
        ui.extension_callout()


def render_password_recovery() -> None:
    """The way back in for someone who has forgotten their password.

    There is no reset email: the only mail scope this app holds is read-only,
    so it cannot send one. A recovery code issued when the account was created
    is what stands in for that link — and unlike a link it works when the
    address on the account is one you can no longer open either.
    """
    with st.expander("Forgotten your password?"):
        st.caption(
            "Enter one of the recovery codes you were given when the account "
            "was created. Each code works once."
        )

        with st.form("recover_form"):
            email = st.text_input("Email", placeholder="you@example.com")
            code = st.text_input("Recovery code", placeholder="XXXX-XXXX-XXXX")
            new = st.text_input(
                "New password",
                type="password",
                help=f"At least {auth.MIN_PASSWORD_LENGTH} characters.",
            )
            confirm = st.text_input("Confirm new password", type="password")
            submitted = st.form_submit_button("Reset password", use_container_width=True)

        if not submitted:
            return

        if new != confirm:
            st.error("Those passwords do not match.")
            return

        try:
            user = auth.reset_password_with_code(email, code, new)
        except auth.RateLimitError as exc:
            st.warning(str(exc))
            return
        except auth.AuthError as exc:
            st.error(str(exc))
            return

        # Signed straight in: having proved possession of a code and set a new
        # password, being asked to type it again immediately is friction with
        # nothing behind it.
        sign_in(user)
        st.rerun()


def render_new_recovery_codes() -> None:
    """Show a freshly issued set of codes, once.

    Nothing stores the plaintext, so this render is the only chance to keep
    them. It stays on screen until it is dismissed explicitly rather than
    disappearing on the next interaction.

    Called from exactly one place — above the tabs — so the download and
    dismiss buttons cannot be created twice in one run. Anything that issues a
    set puts it in session state and reruns, rather than rendering it in place.
    """
    codes = st.session_state.get("new_recovery_codes")

    if not codes:
        return

    st.warning(
        "**Save your recovery codes.** These are the only way back into this "
        "account if you forget your password — there is no reset email. Each "
        "code works once, and they are not shown again."
    )

    if st.session_state.get("recovery_codes_replaced"):
        st.caption("Any codes issued earlier have just stopped working.")

    st.code("\n".join(codes), language=None)

    keep, dismiss = st.columns([1, 3])
    with keep:
        st.download_button(
            "Download",
            data="\n".join(codes) + "\n",
            file_name="talent-pilot-recovery-codes.txt",
            mime="text/plain",
            use_container_width=True,
        )
    with dismiss:
        if st.button("I have saved them", use_container_width=True):
            del st.session_state["new_recovery_codes"]
            st.session_state.pop("recovery_codes_replaced", None)
            st.rerun()


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

    st.sidebar.divider()
    ui.extension_callout(st.sidebar)

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

        # Quantity, not one: a sync classifies a batch, and one model call is
        # paid for per email that reached the classifier. Counting the run as a
        # single event would understate the most expensive thing this app does.
        classified = (
            summary["updated"] + summary["created"] + summary["repeat"]
            + summary["noted"] + summary["failed"]
        )
        if classified:
            usage.record(
                user.id, usage.EMAIL_SYNC, source="sync", quantity=classified
            )

        line = (
            f"{summary['updated']} updated · {summary['created']} added · "
            f"{summary['skipped']} skipped"
        )
        # Said out loud rather than folded into "updated": a second interview
        # round leaves the stage where it was, so a run that reported only
        # "0 updated" read as if the email had never been seen.
        if summary.get("repeat"):
            line += f" · {summary['repeat']} further update(s) at the same stage"
        # Worth its own mention: a sync that moved no statuses can still be the
        # run that found out who is handling an application.
        if summary.get("contacts"):
            line += f" · {summary['contacts']} contact(s) found"

        st.sidebar.success(line)
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

    render_followups(db_path)

    frame = pd.DataFrame(jobs)

    # --- Filters --------------------------------------------------------
    search_col, status_col, remote_col = st.columns([2, 3, 1])
    with search_col:
        search = st.text_input(
            "Search", placeholder="Company, role or location…", label_visibility="collapsed"
        )
    with status_col:
        chosen = st.multiselect(
            "Status",
            VALID_STATUSES,
            format_func=ui.status_label,
            placeholder="All statuses",
            label_visibility="collapsed",
        )
    with remote_col:
        remote_only = st.checkbox("Remote only")

    filtered = frame
    if search:
        # Location joins the haystack: "which of these were in Berlin?" is the
        # same kind of question as "which were at Stripe?".
        haystack = (
            filtered["company"].fillna("")
            + " " + filtered["role"].fillna("")
            + " " + filtered["location"].fillna("")
        )
        filtered = filtered[haystack.str.contains(search, case=False, na=False)]
    if chosen:
        filtered = filtered[filtered["status"].isin(chosen)]
    if remote_only:
        filtered = filtered[filtered["remote"].fillna(0).astype(int) == 1]

    if filtered.empty:
        st.warning("No applications match those filters.")
        return

    # Two stored facts rendered into one readable cell each. The columns stay
    # structured so they can be filtered and compared; the formatting is a
    # presentation decision and lives in posting.py, shared with the API.
    #
    # An absent value is spelled out rather than left blank — a blank cell reads
    # as a field that failed rather than one the posting never stated, and the
    # difference decides whether it is worth looking into. This also carries into
    # the CSV the table's download button produces.
    display = filtered.assign(
        status=filtered["status"].map(ui.status_label),
        where=filtered.apply(
            lambda row: posting.format_location(row["location"], row["remote"])
            or ui.NOT_STATED,
            axis=1,
        ),
        pay=filtered.apply(
            lambda row: posting.format_salary(
                row["salary_min"], row["salary_max"],
                row["salary_currency"], row["salary_period"],
            )
            or ui.NOT_STATED,
            axis=1,
        ),
        # Which CV went out. Recorded on every application since the extension
        # started saving them, and until now never shown anywhere — so the one
        # question a second resume profile exists to answer had no answer.
        resume=filtered["resume_used"].map(
            lambda name: utils.profile_display_name(name) if name else ui.NOT_STATED
        ),
    )[
        [
            "company", "role", "status", "where", "pay",
            "resume", "date_applied", "source", "link",
        ]
    ]

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "company": st.column_config.TextColumn("Company", width="medium"),
            "role": st.column_config.TextColumn("Role", width="large"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "where": st.column_config.TextColumn("Where", width="small"),
            "pay": st.column_config.TextColumn("Pay", width="small"),
            "resume": st.column_config.TextColumn("CV used", width="small"),
            "date_applied": st.column_config.DateColumn(
                "Applied", width="small", format="DD MMM YYYY"
            ),
            "source": st.column_config.TextColumn("Source", width="small"),
            # Rows the tracker only learned about by email have no posting URL
            # unless one turned up in the message, so this is routinely blank.
            "link": st.column_config.LinkColumn("Posting", display_text="Open ↗"),
        },
    )
    st.caption(f"Showing {len(filtered)} of {len(frame)} applications.")

    st.divider()
    render_job_editor(jobs, db_path)


def render_followups(db_path) -> None:
    """Applications that have gone quiet and are worth a nudge.

    The tracker could always say where an application stood; what it could not
    say was which ones were drifting. Status alone does not distinguish "applied
    on Tuesday" from "applied in March and never heard back", and the second is
    the one that needs a decision.
    """
    # Seeded before the widget rather than passed as `value=`, so Streamlit
    # takes the threshold from session state on every rerun instead of warning
    # about a widget that has both a default and a stored value.
    st.session_state.setdefault("followup_days", 10)
    threshold = st.session_state["followup_days"]

    followups = db.get_followups(db_path=db_path, quiet_after_days=threshold)

    header = f"🔔 Needs a nudge ({len(followups)})" if followups else "🔔 Needs a nudge"

    with st.expander(header, expanded=bool(followups)):
        st.number_input(
            "Consider an application quiet after this many days",
            min_value=1,
            max_value=180,
            step=1,
            key="followup_days",
        )

        if not followups:
            st.caption(
                f"Nothing has been silent for {threshold}+ days. "
                "Terminal statuses — offers and rejections — are never counted."
            )
            return

        st.caption(
            "Measured from the last real signal: an email from the employer, a "
            "stage change, or the date you applied. Longest silence first."
        )

        for job in followups:
            quiet = job["days_quiet"]
            line, action = st.columns([4, 1])

            with line:
                st.markdown(f"**{job['company']} — {job['role']}**")

                if quiet is None:
                    detail = "no dated activity on record"
                else:
                    detail = f"quiet for {quiet} days (last activity {job['last_activity']})"

                st.caption(f"{ui.status_label(job['status'])} · {detail}")

                contact = ui.contact_line(job)
                if contact:
                    st.caption(contact)

            with action:
                if job.get("link"):
                    st.link_button("Posting ↗", job["link"], use_container_width=True)


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

    # What the posting itself said. Read from its JSON-LD block when the
    # extension saved it, so it is a declared fact rather than a scrape of prose.
    #
    # Always rendered, with NA where nothing was stated. The emoji label is what
    # makes a bare "NA" mean something — it is answering a question the reader can
    # see, rather than sitting in an unexplained blank.
    where = posting.format_location(current.get("location"), current.get("remote"))
    pay = posting.format_salary(
        current.get("salary_min"), current.get("salary_max"),
        current.get("salary_currency"), current.get("salary_period"),
    )
    resume = current.get("resume_used")
    st.caption(
        f"📍 {where or ui.NOT_STATED}  ·  💰 {pay or ui.NOT_STATED}"
        f"  ·  📄 {utils.profile_display_name(resume) if resume else ui.NOT_STATED}"
        f"  ·  🗓️ Applied {current.get('date_applied') or ui.NOT_STATED}"
    )

    if current.get("link"):
        st.caption(f"🔗 [Open the posting]({current['link']})")

    # Who has actually been in touch. Captured by inbox sync from the headers
    # and signature of every email it classifies, all of which the tracker
    # previously discarded — so "who do I reply to?" had no answer without
    # going back to Gmail.
    contact = ui.contact_line(current)
    if contact:
        last_seen = (current.get("last_contact_at") or "")[:10]
        suffix = f" · last heard {last_seen}" if last_seen else ""
        st.caption(f"{contact}{suffix}")

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

            if entry["from_status"] == entry["to_status"]:
                # A further email about the stage it was already at — an
                # interview round two. "Interview → Interview" reads as a bug,
                # so it is spelled out instead.
                arrow = f"{ui.status_label(entry['to_status'])} · further update"
            elif entry["from_status"]:
                arrow = (
                    f"{ui.status_label(entry['from_status'])} → "
                    f"{ui.status_label(entry['to_status'])}"
                )
            else:
                arrow = ui.status_label(entry["to_status"])

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
    # "Update" rather than "change": a further email at the same stage counts
    # here too, and it is the more useful reading of "have I heard anything?".
    return (
        f"Open {(now - first).days} days · "
        f"{(now - last).days} days since the last update"
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

        # Filled in automatically when the extension saves a job — the posting
        # declares both. Here by hand, so a manually added row is not
        # permanently missing the columns the dashboard shows.
        place_col, remote_col = st.columns([3, 1])
        with place_col:
            location = st.text_input("Location", placeholder="e.g. Bengaluru, Karnataka")
        with remote_col:
            remote = st.checkbox("Remote", value=False)

        pay_min, pay_max, pay_currency, pay_period = st.columns([2, 2, 1, 1])
        with pay_min:
            salary_min = st.number_input(
                "Salary from", min_value=0, value=0, step=1000, format="%d"
            )
        with pay_max:
            salary_max = st.number_input(
                "Salary to", min_value=0, value=0, step=1000, format="%d"
            )
        with pay_currency:
            salary_currency = st.text_input("Currency", placeholder="INR", max_chars=3)
        with pay_period:
            salary_period = st.selectbox("Per", ["", *posting.SALARY_PERIODS])

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
            location=location,
            remote=remote,
            # 0 from a number_input means "not stated", which normalise_salary
            # drops — the same path the extension's values take.
            salary=posting.normalise_salary(
                salary_min or None, salary_max or None, salary_currency, salary_period
            ),
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
def cached_analysis(jd_text: str, resume_string: str, resume_text: str = "") -> dict:
    """Cache by content, so re-analysing the same pairing is free.

    ``resume_text`` is part of the key as well as the call: re-uploading a
    resume changes what the keyword pass sees even when the parsed profile
    comes back identical.
    """
    return resume_parser.analyze_jd(jd_text, resume_string, resume_text)


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
    resume_text = utils.load_profile_text(user.id, selected_profile)

    with st.spinner("Gemini is comparing the job description to your resume…"):
        result = cached_analysis(job["jd"], json.dumps(resume), resume_text)

    # Counted per analysis the user asked for, including the ones served from
    # cache: this is the measure of what the product is worth to them, and the
    # cache is our saving rather than a reason to charge them less.
    usage.record(user.id, usage.ANALYZE_JD, source="dashboard")

    if "error" in result:
        st.error(result["message"])
        return

    st.write("")

    coverage = result.get("coverage", {})
    keywords = result.get("keyword_coverage", {})

    # Two numbers because they answer different questions. A resume can read
    # well to a person and still never reach one, because the first filter is
    # frequently a literal string match that does not know Azure experience
    # transfers to AWS.
    fit_col, ats_col, summary_col = st.columns([1, 1, 3])
    with fit_col:
        st.metric("Recruiter fit", f"{result['match_percentage']}%")
        if coverage.get("required_total"):
            st.caption(
                f"{coverage['required_met']:g}/{coverage['required_total']} must-haves"
                + (
                    f" · {coverage['preferred_met']:g}/{coverage['preferred_total']} preferred"
                    if coverage.get("preferred_total")
                    else ""
                )
            )
    with ats_col:
        if keywords.get("scored"):
            st.metric("Keyword coverage", f"{keywords['score']}%")
            st.caption(f"{len(keywords['matched'])}/{keywords['total']} terms present")
        else:
            st.metric("Keyword coverage", "—")
            st.caption("No known terms found in this posting")
    with summary_col:
        st.info(result["summary"])

    # The requirement table is the score's working. A percentage nobody can
    # take apart is the thing this replaced.
    requirements = result.get("requirements") or []

    scored = [r for r in requirements if r["kind"] in scoring.SCOREABLE_KINDS]

    if scored:
        st.markdown("**How that score is made up**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Requirement": r["skill"],
                        "Weight": "Must have" if r["importance"] == "required" else "Preferred",
                        "Kind": {
                            scoring.DOMAIN: "Industry",
                            scoring.META: "Ways of working",
                        }.get(r["kind"], "Skill"),
                        "Evidence": {
                            "demonstrated": "✅ Demonstrated",
                            "partial": "🟡 Partial",
                            "absent": "❌ Absent",
                        }.get(r["status"], r["status"]),
                        "Where": r["evidence"] or "—",
                    }
                    for r in scored
                ]
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Requirement": st.column_config.TextColumn(width="medium"),
                "Weight": st.column_config.TextColumn(width="small"),
                "Kind": st.column_config.TextColumn(width="small"),
                "Evidence": st.column_config.TextColumn(width="small"),
                "Where": st.column_config.TextColumn(width="large"),
            },
        )

    else:
        matched_col, missing_col = st.columns(2)
        with matched_col:
            st.markdown("**✅ Matched skills**")
            for skill in result["matched_skills"] or ["—"]:
                st.markdown(f"- {skill}")
        with missing_col:
            st.markdown("**❌ Missing skills**")
            for skill in result["missing_skills"] or ["—"]:
                st.markdown(f"- {skill}")

    # Shown, and shown apart, whether or not anything else scored. A posting
    # that opens "you must have experience in SOLD Simplification" is naming
    # its own internal programme: nobody applying from outside has it, no edit
    # to a resume can produce it, and scoring it cost one real candidate
    # sixteen points and then told them it was a gap to close. Dropping it
    # silently would be its own dishonesty — the job did say it.
    for entry in coverage.get("not_scored") or []:
        st.caption(
            f"ℹ️ **{entry['skill']}** looks like something internal to this "
            "employer, so it is not counted for or against you."
        )

    # The terms a filter looks for and cannot find. These are the literal
    # strings worth surfacing on the resume — provided they are true.
    if keywords.get("missing"):
        st.markdown("**Terms this posting uses that your resume does not**")
        st.caption(
            "A keyword filter matches text, not meaning. Add only the ones you "
            "can genuinely claim."
        )
        st.markdown(" · ".join(f"`{term}`" for term in keywords["missing"]))


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

            # The PDF's own words are kept beside the parsed profile. The
            # keyword pass reads them, because approximating a literal filter
            # means measuring the document the employer actually receives.
            filename = utils.save_profile(user.id, name, structured, raw_text=raw_text)
            # Metered here rather than at the button: this is the point past
            # which the model call has actually been paid for.
            usage.record(user.id, usage.RESUME_UPLOAD, source="dashboard")

            # Seed the application-form answers from the resume, so the
            # questionnaire arrives mostly filled in. Never overwrites an
            # answer the user has already given.
            status.write("Pre-filling your application answers…")
            autofill.seed_from_resume(user.id, structured)

            status.update(label=f"Saved {filename}", state="complete")
            st.success(f"Created profile **{utils.profile_display_name(filename)}**.")

            missing = autofill.completeness(user.id)["missing"]
            if missing:
                st.info(
                    f"Next: finish the **📝 Application answers** tab — "
                    f"{missing} questions still need an answer. They are asked "
                    "once and reused on every application."
                )
            st.rerun()

        except utils.ResumeReadError as exc:
            status.update(label="Could not read that PDF", state="error")
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            logger.error("PDF parse failed for user %s: %s", user.id, exc)
            status.update(label="Could not read that PDF", state="error")
            st.error(f"Failed to process the PDF: {exc}")


# =====================================================================
# TAB: APPLICATION ANSWERS
# =====================================================================
def render_autofill(user: auth.User) -> None:
    """The questionnaire behind the extension's form suggestions.

    Application forms ask the same two dozen questions forever, usually buried
    in a paragraph of legal text. Answering them once here is what lets the
    extension suggest an answer in the page without a model call.
    """
    st.subheader("Application answers")
    st.caption(
        "Answered once and reused on every application. The extension suggests "
        "these as you fill a form — nothing is submitted for you."
    )
    # These answers only pay off inside the extension, so the page that
    # collects them is the one place a missing install is worth naming.
    ui.extension_callout(
        blurb="it is what suggests these answers while you fill a form."
    )

    stats = autofill.completeness(user.id)
    bank = autofill.load(user.id)

    done, total = stats["answered"], stats["total"]
    st.progress(done / total if total else 0.0, text=f"{done} of {total} answered")

    if stats["missing"]:
        st.caption(
            "Blank answers are simply not suggested, so it is fine to skip any "
            "that do not apply to you."
        )

    # --- The catalogue, grouped ----------------------------------------
    with st.form("autofill_form"):
        submitted_values: dict[str, str] = {}

        for group in autofill.GROUPS:
            fields = [f for f in autofill.FIELDS if f.group == group]
            answered = sum(1 for f in fields if bank["answers"].get(f.key))

            with st.expander(
                f"{group}  ·  {answered}/{len(fields)}", expanded=answered < len(fields)
            ):
                for field in fields:
                    current = bank["answers"].get(field.key, "")
                    key = f"af_{field.key}"

                    if field.kind == "yes_no":
                        choices = ["", "Yes", "No"]
                        index = choices.index(current) if current in choices else 0
                        submitted_values[field.key] = st.selectbox(
                            field.question,
                            choices,
                            index=index,
                            key=key,
                            format_func=lambda v: "— not answered —" if not v else v,
                            help=field.help or None,
                        )
                    elif field.kind == "choice":
                        choices = [""] + field.options
                        index = choices.index(current) if current in choices else 0
                        submitted_values[field.key] = st.selectbox(
                            field.question,
                            choices,
                            index=index,
                            key=key,
                            format_func=lambda v: "— not answered —" if not v else v,
                            help=field.help or None,
                        )
                    else:
                        submitted_values[field.key] = st.text_input(
                            field.question,
                            value=current,
                            key=key,
                            help=field.help or None,
                        )

        saved = st.form_submit_button("Save answers", use_container_width=True)

    if saved:
        autofill.set_answers(user.id, submitted_values)
        st.success("Saved. The extension picks these up on its next page load.")
        st.rerun()

    st.divider()

    # --- Anything the catalogue does not cover --------------------------
    st.markdown("**Your own questions**")
    st.caption(
        "Anything a form asks that is not above. Answers drafted with AI in the "
        "extension are saved here automatically, so the same question is instant "
        "and free next time."
    )

    if bank["custom"]:
        for entry in bank["custom"]:
            row, actions = st.columns([5, 1])
            with row:
                st.markdown(f"**{entry['question']}**")
                st.caption(entry["answer"])
            if actions.button(
                "Remove", key=f"rm_{entry['question'][:40]}", use_container_width=True
            ):
                autofill.remove_custom(user.id, entry["question"])
                st.rerun()
    else:
        st.caption("None saved yet.")

    with st.form("custom_answer_form", clear_on_submit=True):
        question = st.text_input(
            "Question", placeholder="e.g. Do you hold a valid driving licence?"
        )
        answer = st.text_input("Your answer", placeholder="e.g. Yes")
        added = st.form_submit_button("Add answer")

    if added:
        try:
            autofill.add_custom(user.id, question, answer)
            st.success("Added.")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


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
                "Change": _change_label(entry),
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
            if any(
                tag in line
                for tag in (
                    "SKIP", "UPDATED", "CREATED", "REPEAT", "NOTED", "MATCH", "ERROR",
                )
            )
        ]

    st.code("\n".join(lines[-120:]) or "No matching lines.", language="log")


def _change_label(entry: dict) -> str:
    """How one history row reads in the recent-changes table.

    Same from and to is not a non-event: it is the second interview round, or
    the rescheduled assessment. Naming it is the difference between the user
    seeing that the email landed and concluding the sync ignored it.
    """
    if entry["from_status"] == entry["to_status"]:
        return f"{entry['to_status']} (further update)"
    if entry["from_status"]:
        return f"{entry['from_status']} → {entry['to_status']}"
    return entry["to_status"]


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
    st.markdown("**Recovery codes**")

    remaining = auth.count_recovery_codes(user.id)

    if remaining:
        st.caption(
            f"{remaining} unused code(s). Each one can set a new password once, "
            "without needing the old one."
        )
    else:
        # Accounts created before recovery codes existed have none, and so does
        # anyone who has spent the lot. Both are one forgotten password away
        # from an account nobody can open.
        st.caption(
            "No recovery codes on this account. Without one, a forgotten "
            "password cannot be reset — there is no reset email."
        )

    if st.button("Generate a new set"):
        st.session_state["new_recovery_codes"] = auth.issue_recovery_codes(user.id)
        st.session_state["recovery_codes_replaced"] = True
        # Shown at the top of the page rather than here, so the one render of
        # a set of codes is the same render wherever it was issued.
        st.rerun()

    st.caption("A new set appears at the top of the page and replaces the old one.")

    st.divider()
    st.markdown("**Browser extension**")
    st.caption(
        "Sign in from the extension popup with these same credentials. "
        "Start the API with `uvicorn api.server:app --port 8000` first."
    )
    ui.extension_callout()


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

    # Above the tabs, on the way in from a fresh registration. The codes are
    # never recoverable after this render, so they are not put behind a tab
    # the user has no reason to open yet.
    render_new_recovery_codes()

    # Surfaced on the tab itself so an unfinished questionnaire is visible
    # without having to go looking for it.
    setup = autofill.completeness(user.id)
    answers_badge = f" ({setup['missing']})" if setup["missing"] else ""

    (
        dashboard_tab,
        add_tab,
        analyzer_tab,
        profiles_tab,
        answers_tab,
        activity_tab,
        settings_tab,
    ) = st.tabs(
        [
            "📊 Dashboard",
            "➕ Add application",
            "🧠 Analyzer",
            "📄 Profiles",
            f"📝 Application answers{answers_badge}",
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
    with answers_tab:
        render_autofill(user)
    with activity_tab:
        render_activity(user, db_path)
    with settings_tab:
        render_settings(user)


main()
