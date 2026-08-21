"""The admin panel: everything in OPERATIONS.md, rendered.

One tab, visible only to the addresses in ADMIN_EMAILS, holding five sections —
who is using the instance, the accounts themselves, what they have cost, the
machine, and a record of every administrative action taken through here.

Nothing on this page is cached. Streamlit's ``cache_data`` would be the obvious
optimisation and it is the wrong one: the whole reason to look is to find out
what is true *now*, and a stale answer to "is the API running?" or "did that
account get deleted?" is worse than a slow one. Every render reads the live
database and the live filesystem, which on a deployment of this size is a few
milliseconds.

The panel runs on the server, beside the data it shows. That is what makes it
live without any synchronisation: there is no copy to go stale, because there
is no copy.
"""

import secrets
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import admin
import auth
import sysops
import usage
from config import REGISTRATION_CLOSED, SIGNUP_CODE

# Windows the account and usage views can be narrowed to. "All time" is first
# because the total is the number a price has to beat; the shorter windows
# answer "and is that still true?".
WINDOWS = {
    "All time": None,
    "Last 90 days": 90,
    "Last 30 days": 30,
    "Last 7 days": 7,
}


def render(user: auth.User) -> None:
    """Draw the whole panel for a signed-in administrator."""
    # app.py already refuses to create the tab for anyone else. Checked again
    # here because the cost is one comparison and the failure mode of getting
    # it wrong is every account on the instance.
    if not admin.is_admin(user.email):
        st.error("This section is for administrators.")
        return

    header, refresh = st.columns([5, 1])

    with header:
        st.caption(
            f"Signed in as **{user.email}** · reading live data from this server · "
            f"as of {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )

    with refresh:
        if st.button("↻ Refresh", use_container_width=True):
            st.rerun()

    overview, accounts, usage_tab, server, audit = st.tabs(
        ["Overview", "Accounts", "Usage", "Server", "Audit log"]
    )

    with overview:
        _render_overview()
    with accounts:
        _render_accounts(user)
    with usage_tab:
        _render_usage()
    with server:
        _render_server(user)
    with audit:
        _render_audit()


# =====================================================================
# OVERVIEW
# =====================================================================
def _render_overview() -> None:
    figures = admin.summary()

    top = st.columns(4)
    top[0].metric("Accounts", figures["accounts"])
    top[1].metric("Active (30 days)", figures["active_30d"])
    top[2].metric("Active (7 days)", figures["active_7d"])
    top[3].metric("Never came back", figures["never_returned"])

    bottom = st.columns(4)
    bottom[0].metric("Paid units (30 days)", figures["paid_units_30d"])
    bottom[1].metric("Paid units (all time)", figures["paid_units"])
    bottom[2].metric("Applications tracked", figures["jobs"])
    bottom[3].metric("User data on disk", _bytes(figures["bytes"]))

    st.caption(
        "A paid unit is one model call: a CV parsed, a job description "
        "analysed, an answer drafted, or a single email classified. It is the "
        "number a price has to beat."
    )

    st.divider()

    left, right = st.columns(2)

    with left:
        st.markdown("**Registration**")
        _render_registration_state()

    with right:
        st.markdown("**Services**")
        _render_service_strip()


def _render_registration_state() -> None:
    """Whether strangers can sign up right now.

    Read from the environment file rather than from ``config`` because config
    was resolved when this process started: if the invite code was rotated ten
    minutes ago and the services have not been restarted, the file is what is
    true and config is what is running. Saying both is the only honest answer.
    """
    try:
        settings = sysops.read_settings()
    except sysops.OpsError as exc:
        st.warning(str(exc))
        return

    closed = settings.get("REGISTRATION_CLOSED", "").lower() == "true"
    code = settings.get("SIGNUP_CODE") or SIGNUP_CODE

    if closed:
        st.error("Closed — every new account is refused.")
    elif code:
        st.success("Open, invite code required.")
        st.code(code, language=None)
    else:
        st.warning(
            "Open with **no invite code**. Anyone who can reach this server "
            "can register and spend the Gemini quota."
        )

    running_closed = REGISTRATION_CLOSED
    running_code = SIGNUP_CODE

    if closed != running_closed or code != running_code:
        st.caption(
            "⚠️ The file above differs from what these services loaded at "
            "start-up. Restart them from the **Server** tab to apply it."
        )


def _render_service_strip() -> None:
    statuses = sysops.service_status()

    if not statuses:
        st.caption("No systemd on this machine — services are not managed here.")
        return

    for entry in statuses:
        icon = "🟢" if entry["running"] else "🔴"
        st.write(f"{icon} `{entry['name']}` — {entry['state']}")


# =====================================================================
# ACCOUNTS
# =====================================================================
def _render_accounts(actor: auth.User) -> None:
    window = st.radio(
        "Usage window",
        list(WINDOWS),
        horizontal=True,
        key="admin_accounts_window",
    )

    rows = admin.accounts(days=WINDOWS[window])

    if not rows:
        st.info("No accounts yet.")
        return

    search = st.text_input(
        "Search", placeholder="Email or account id", key="admin_account_search"
    ).strip().lower()

    if search:
        rows = [
            row
            for row in rows
            if search in row["email"].lower() or search == str(row["user_id"])
        ]

        if not rows:
            st.info(f"Nothing matches “{search}”.")
            return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "ID": row["user_id"],
                    "Email": row["email"],
                    "Joined": _when(row["created_at"], date_only=True),
                    "Last seen": _when(row["last_login_at"]),
                    "Paid units": row["paid_units"],
                    "Jobs": row["workspace"]["jobs"],
                    "Sessions": row["sessions"],
                    "Recovery codes": row["recovery_left"],
                    "Gmail": "connected" if row["workspace"]["gmail_connected"] else "—",
                    "Data": _bytes(row["workspace"]["bytes"]),
                }
                for row in rows
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()

    # Drop a selection that the current filter has hidden. Streamlit refuses to
    # render a keyed selectbox whose remembered value is not among its options,
    # so without this, searching for anything while an account is selected
    # replaces the panel with an exception.
    choices = [row["user_id"] for row in rows]

    if st.session_state.get("admin_selected_account") not in choices:
        st.session_state.pop("admin_selected_account", None)

    chosen = st.selectbox(
        "Manage an account",
        choices,
        format_func=lambda value: next(
            f"{row['user_id']} — {row['email']}"
            for row in rows
            if row["user_id"] == value
        ),
        key="admin_selected_account",
    )

    selected = next(row for row in rows if row["user_id"] == chosen)
    _render_account_detail(actor, selected)


def _render_account_detail(actor: auth.User, row: dict) -> None:
    user_id = row["user_id"]
    facts = row["workspace"]
    is_self = user_id == actor.id

    with st.container(border=True):
        st.subheader(row["email"])
        st.caption(
            f"Account **{user_id}** · joined {_when(row['created_at'])} · "
            f"last seen {_when(row['last_login_at'])} · "
            f"workspace `{facts['path']}`"
        )

        figures = st.columns(4)
        figures[0].metric("Paid units", row["paid_units"])
        figures[1].metric("Applications", facts["jobs"])
        figures[2].metric("Resumes", facts["profiles"])
        figures[3].metric("Data", _bytes(facts["bytes"]))

        if facts["gmail_connected"]:
            st.caption(f"📧 Gmail connected · last sync {facts['last_sync'] or 'never'}")

        if is_self:
            st.caption("This is the account you are signed in with.")

        _render_account_activity(row)
        _render_account_applications(user_id)
        _render_account_access(actor, row)
        _render_account_data(actor, row, is_self)


def _render_account_activity(row: dict) -> None:
    with st.expander("Activity breakdown"):
        st.dataframe(
            pd.DataFrame(
                [
                    {"Action": label, "Units": row["events"].get(event, 0)}
                    for label, event in usage.REPORT_COLUMNS
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )


def _render_account_applications(user_id: int) -> None:
    """This user's own tracked applications.

    Behind a click rather than on the page, because it is the one thing in the
    panel that shows a person's content rather than their account. Opening it
    should be a decision.
    """
    with st.expander("Applications they are tracking"):
        jobs = admin.jobs_for(user_id)

        if not jobs:
            st.caption("Nothing tracked yet.")
            return

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ID": job.get("id"),
                        "Company": job.get("company"),
                        "Role": job.get("role"),
                        "Status": job.get("status"),
                        "Source": job.get("source"),
                        "Applied": job.get("date_applied"),
                    }
                    for job in jobs
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )


def _render_account_access(actor: auth.User, row: dict) -> None:
    user_id = row["user_id"]

    with st.expander("Access — password, recovery, sessions"):
        st.markdown("**Recovery codes**")
        st.caption(
            f"{row['recovery_left']} unused. Issuing a set is the better answer "
            "to a forgotten password than choosing one for them: they set their "
            "own, and you never handle a credential."
        )

        if st.button("Issue a new set", key=f"recovery_{user_id}"):
            codes = admin.issue_recovery_codes(actor.email, user_id)
            st.session_state[f"codes_{user_id}"] = codes
            st.rerun()

        issued = st.session_state.get(f"codes_{user_id}")
        if issued:
            st.success("Send these to them. They are not shown again.")
            st.code("\n".join(issued), language=None)
            if st.button("Done", key=f"codes_done_{user_id}"):
                st.session_state.pop(f"codes_{user_id}", None)
                st.rerun()

        st.divider()
        st.markdown("**Set a password directly**")
        st.caption(
            "For an account with no recovery codes left. Signs out every "
            "device, and they should change it once they are back in."
        )

        with st.form(f"password_{user_id}"):
            suggestion = st.session_state.get(
                f"suggested_{user_id}", secrets.token_urlsafe(12)
            )
            st.session_state[f"suggested_{user_id}"] = suggestion

            new_password = st.text_input("New password", value=suggestion)
            set_it = st.form_submit_button("Set password")

        if set_it:
            try:
                admin.reset_password(actor.email, user_id, new_password)
            except auth.AuthError as exc:
                st.error(str(exc))
            else:
                st.session_state.pop(f"suggested_{user_id}", None)
                st.success("Password set and every session revoked.")

        st.divider()
        st.markdown("**Sessions and lockouts**")

        left, right = st.columns(2)

        with left:
            if st.button(
                f"Sign out all devices ({row['sessions']})",
                key=f"revoke_{user_id}",
                use_container_width=True,
            ):
                count = admin.revoke_sessions(actor.email, user_id)
                st.success(f"Revoked {count} token(s).")

        with right:
            if st.button(
                "Clear failed sign-ins",
                key=f"lockout_{user_id}",
                use_container_width=True,
            ):
                count = admin.clear_lockouts(actor.email, row["email"])
                st.success(f"Cleared {count} attempt(s). They can try again now.")

        st.divider()
        st.markdown("**Email address**")
        st.caption(
            "The workspace is keyed on the account id, so nothing they have "
            "saved moves. Only how they sign in changes."
        )

        with st.form(f"email_{user_id}"):
            new_email = st.text_input("Email", value=row["email"])
            rename = st.form_submit_button("Save email")

        if rename and new_email.strip().lower() != row["email"].lower():
            try:
                updated = admin.change_email(actor.email, user_id, new_email)
            except auth.AuthError as exc:
                st.error(str(exc))
            else:
                st.success(f"Now signs in as {updated}.")
                st.rerun()


def _render_account_data(actor: auth.User, row: dict, is_self: bool) -> None:
    user_id = row["user_id"]

    with st.expander("Data — Gmail, export, deletion"):
        if row["workspace"]["gmail_connected"]:
            st.caption(
                "Removing the stored token ends inbox sync for them until they "
                "reconnect. The fix for a grant that has gone bad."
            )
            if st.button("Disconnect Gmail", key=f"gmail_{user_id}"):
                admin.disconnect_gmail(actor.email, user_id)
                st.success("Token removed.")
                st.rerun()
        else:
            st.caption("Gmail is not connected on this account.")

        st.divider()
        st.markdown("**Export**")
        st.caption(
            "Their profile, answers, applications and usage as a zip. The "
            "Gmail token is deliberately left out — it is a live key to their "
            "mailbox, not a record of their data."
        )

        if st.button("Prepare export", key=f"export_{user_id}"):
            name, payload = admin.export_account(actor.email, user_id)
            st.session_state[f"export_data_{user_id}"] = (name, payload)

        prepared = st.session_state.get(f"export_data_{user_id}")
        if prepared:
            st.download_button(
                f"Download {prepared[0]}",
                data=prepared[1],
                file_name=prepared[0],
                mime="application/zip",
                key=f"download_{user_id}",
            )

        st.divider()
        st.markdown("**Delete this account**")

        if is_self:
            st.info("You cannot delete the account you are signed in with.")
            return

        st.warning(
            f"Removes the account, its {row['workspace']['jobs']} tracked "
            f"application(s), every resume and answer, and "
            f"{_bytes(row['workspace']['bytes'])} of data. There is no undo "
            "outside a backup."
        )

        typed = st.text_input(
            f"Type **{row['email']}** to confirm",
            key=f"confirm_{user_id}",
            placeholder=row["email"],
        )
        acknowledged = st.checkbox(
            "I have taken an export or a backup", key=f"ack_{user_id}"
        )

        ready = typed.strip().lower() == row["email"].lower() and acknowledged

        if st.button(
            "Delete permanently",
            key=f"delete_{user_id}",
            type="primary",
            disabled=not ready,
        ):
            result = admin.delete_account(actor.email, user_id)
            st.success(
                f"Deleted {result['email']} — {result['jobs']} application(s) "
                f"and {_bytes(result['bytes'])} removed."
            )
            for key in (f"confirm_{user_id}", f"ack_{user_id}", "admin_selected_account"):
                st.session_state.pop(key, None)
            st.rerun()


# =====================================================================
# USAGE
# =====================================================================
def _render_usage() -> None:
    window = st.radio(
        "Window", list(WINDOWS), horizontal=True, key="admin_usage_window"
    )
    days = WINDOWS[window]

    totals = usage.totals(days=days)

    columns = st.columns(len(usage.REPORT_COLUMNS))
    for column, (label, event) in zip(columns, usage.REPORT_COLUMNS):
        column.metric(label, totals.get(event, 0))

    rows = admin.accounts(days=days)

    table = pd.DataFrame(
        [
            {
                "ID": row["user_id"],
                "Email": row["email"],
                "Paid units": row["paid_units"],
                **{
                    label: row["events"].get(event, 0)
                    for label, event in usage.REPORT_COLUMNS
                },
                "Last seen": _when(row["last_login_at"]),
            }
            for row in rows
        ]
    )

    st.dataframe(table, use_container_width=True, hide_index=True)

    if not table.empty:
        st.download_button(
            "Download as CSV",
            data=table.to_csv(index=False).encode("utf-8"),
            file_name=f"talent-pilot-usage-{datetime.now(timezone.utc):%Y%m%d}.csv",
            mime="text/csv",
        )

        # The distribution is the pricing question, not the total: if one
        # account is most of the spend, a flat monthly price is a bet on that
        # account staying.
        top = max(rows, key=lambda row: row["paid_units"])
        total = sum(row["paid_units"] for row in rows)

        if total and top["paid_units"]:
            share = round(top["paid_units"] / total * 100)
            st.caption(
                f"Heaviest account is **{top['email']}** at {share}% of all "
                "paid units in this window."
            )

    st.divider()
    st.subheader("Latest actions")
    st.caption("What people are doing right now, newest first.")

    recent = usage.recent(limit=60)

    if not recent:
        st.info("Nothing recorded yet.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "When": _when(entry["occurred_at"]),
                    "Account": entry["email"],
                    "Action": entry["event"],
                    "From": entry["source"],
                    "Units": entry["quantity"],
                }
                for entry in recent
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )


# =====================================================================
# SERVER
# =====================================================================
def _render_server(actor: auth.User) -> None:
    able = sysops.capabilities()

    if not able["sudo"]:
        st.info(
            f"**Read-only on this machine.** {able['sudo_detail']}\n\n"
            "Health, settings and logs below are still live. To enable "
            "restarts, settings changes and backups, install the sudoers rule "
            "once — see *Enabling server actions in the panel* in "
            "deploy/OPERATIONS.md."
        )

    _render_machine()

    st.divider()
    _render_services(actor, able)

    st.divider()
    _render_settings(actor, able)

    st.divider()
    _render_backups(actor, able)

    st.divider()
    _render_logs()


def _render_machine() -> None:
    st.subheader("Machine")

    facts = sysops.machine()
    columns = st.columns(4)

    disk = facts["disk"]
    if disk:
        columns[0].metric(
            "Disk used", f"{disk['percent']}%", help=f"{_bytes(disk['free'])} free"
        )
    else:
        columns[0].metric("Disk used", "—")

    memory = facts["memory"]
    if memory:
        columns[1].metric(
            "Memory used",
            f"{memory['percent']}%",
            help=f"{_bytes(memory['available'])} available of {_bytes(memory['total'])}",
        )
    else:
        columns[1].metric("Memory used", "—", help="Not reported on this platform.")

    load = facts["load"]
    columns[2].metric("Load (1 min)", f"{load[0]:.2f}" if load else "—")

    uptime = facts["uptime_seconds"]
    columns[3].metric("Uptime", _duration(uptime) if uptime else "—")

    if memory and memory["percent"] > 90:
        st.warning(
            "Memory is nearly full. On a 1 GB machine this is what precedes a "
            "service being killed — check the Audit log and errors.log."
        )


def _render_services(actor: auth.User, able: dict) -> None:
    st.subheader("Services")

    statuses = sysops.service_status()

    if not statuses:
        st.caption("systemd is not present on this machine.")
        return

    st.caption(
        "Both application services run the same code, so a deploy needs both "
        "restarted. Restarting the dashboard restarts the process serving this "
        "page — the browser reconnects after a few seconds."
    )

    for entry in statuses:
        row = st.columns([3, 1])
        icon = "🟢" if entry["running"] else "🔴"
        row[0].write(f"{icon} `{entry['name']}` — {entry['state']}")

        if row[1].button(
            "Restart",
            key=f"restart_{entry['name']}",
            disabled=not able["sudo"],
            use_container_width=True,
        ):
            try:
                st.success(sysops.restart_service(actor.email, entry["name"]))
            except sysops.OpsError as exc:
                st.error(str(exc))


def _render_settings(actor: auth.User, able: dict) -> None:
    st.subheader("Settings")

    if not able["env_file"]:
        st.caption(
            f"No environment file at `{able['env_path']}` — this is a local "
            "instance, which reads its settings from `.env` instead."
        )
        return

    try:
        current = sysops.read_settings()
    except sysops.OpsError as exc:
        st.error(str(exc))
        return

    st.caption(f"Held in `{able['env_path']}`. Nothing applies until a restart.")

    with st.form("admin_settings"):
        proposed = {}

        for entry in sysops.SETTINGS:
            key = entry["key"]
            value = current.get(key, "")

            if entry["kind"] == "bool":
                proposed[key] = str(
                    st.checkbox(
                        entry["label"], value=value.lower() == "true", help=entry["help"]
                    )
                ).lower()
            elif entry["kind"] == "choice":
                options = list(entry["options"])
                index = options.index(value) if value in options else 0
                proposed[key] = st.selectbox(
                    entry["label"], options, index=index, help=entry["help"]
                )
            elif entry["kind"] == "secret":
                proposed[key] = st.text_input(
                    entry["label"],
                    value="",
                    type="password",
                    placeholder="Set — leave blank to keep it" if value else "Not set",
                    help=entry["help"],
                )
            else:
                proposed[key] = st.text_input(
                    entry["label"], value=value, help=entry["help"]
                )

        saved = st.form_submit_button("Save changes", disabled=not able["sudo"])

    if not saved:
        return

    changes = []

    for key, value in proposed.items():
        # A blank secret means "leave it alone", not "erase the API key".
        if sysops.SETTING_KEYS[key]["kind"] == "secret" and not value.strip():
            continue
        if value != current.get(key, ""):
            changes.append((key, value))

    if not changes:
        st.info("Nothing changed.")
        return

    for key, value in changes:
        try:
            sysops.write_setting(actor.email, key, value)
        except sysops.OpsError as exc:
            st.error(f"{key}: {exc}")
        else:
            st.success(f"{key} saved.")

    st.warning("Restart both services above for these to take effect.")


def _render_backups(actor: auth.User, able: dict) -> None:
    st.subheader("Backups")

    if st.button("Take one now", disabled=not able["sudo"]):
        try:
            st.success(sysops.run_backup(actor.email))
        except sysops.OpsError as exc:
            st.error(str(exc))

    try:
        archives = sysops.backups()
    except sysops.OpsError as exc:
        st.error(str(exc))
        return

    if not archives:
        if able["backup_dir"]:
            st.caption("No archives yet. The nightly job keeps 14 days of them.")
        else:
            st.caption(
                "No backup directory on this machine — backups exist only on "
                "the server."
            )
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Archive": item["name"],
                    "Taken": _when(item["taken_at"]),
                    "Size": _bytes(item["bytes"]),
                }
                for item in archives
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "Restoring is the one operation still deliberately kept to a shell: it "
        "stops both services and unpacks over live data. See deploy/OPERATIONS.md."
    )


def _render_logs() -> None:
    st.subheader("Logs")

    controls = st.columns([2, 1, 2])
    name = controls[0].selectbox("File", list(sysops.LOG_FILES))
    count = controls[1].number_input("Lines", min_value=20, max_value=2000, value=200, step=20)
    contains = controls[2].text_input("Containing", placeholder="SKIP, ERROR, a request id…")

    lines = sysops.tail(name, lines=int(count), contains=contains.strip())

    if not lines:
        st.info("Nothing matching in that file.")
        return

    st.code("\n".join(lines), language="log")


# =====================================================================
# AUDIT LOG
# =====================================================================
def _render_audit() -> None:
    st.caption(
        "Every change made through this panel. Written when the action "
        "succeeds, kept when the account it refers to is deleted — that row is "
        "the only remaining evidence the account ever existed."
    )

    entries = admin.audit_trail(limit=200)

    if not entries:
        st.info("Nothing has been changed through the panel yet.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "When": _when(entry["occurred_at"]),
                    "Administrator": entry["actor"],
                    "Action": entry["action"],
                    "Target": entry["target"],
                    "Detail": entry["detail"],
                }
                for entry in entries
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )


# =====================================================================
# FORMATTING
# =====================================================================
def _bytes(count) -> str:
    """Byte counts as something readable at a glance."""
    size = float(count or 0)

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024

    return f"{size:,.1f} GB"


def _when(value: str, *, date_only: bool = False) -> str:
    """An ISO timestamp as a person reads it, or "never" when there is none."""
    if not value:
        return "never"

    text = str(value)
    return text[:10] if date_only else text[:16].replace("T", " ")


def _duration(seconds: float) -> str:
    """Seconds as days and hours."""
    days, rest = divmod(int(seconds), 86400)
    hours = rest // 3600

    if days:
        return f"{days}d {hours}h"

    minutes = (rest % 3600) // 60
    return f"{hours}h {minutes}m"
