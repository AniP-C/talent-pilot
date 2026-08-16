"""Presentation helpers for the Streamlit dashboard.

Kept separate from app.py so the page logic stays readable and the styling
lives in one place.
"""

import streamlit as st

from config import STATUS_LABELS

# Deliberately restrained: a few spacing and colour corrections on top of the
# default theme rather than a full re-skin, so Streamlit upgrades don't break it.
STYLES = """
<style>
    /* Tighten the default page padding */
    .block-container { padding-top: 2.5rem; padding-bottom: 3rem; max-width: 1200px; }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 10px;
        padding: 1rem 1.2rem;
    }
    div[data-testid="stMetricLabel"] p {
        font-size: 0.8rem;
        font-weight: 500;
        opacity: 0.75;
        text-transform: uppercase;
        letter-spacing: 0.03em;
    }
    div[data-testid="stMetricValue"] { font-size: 1.9rem; font-weight: 600; }

    /* Tab bar */
    button[data-baseweb="tab"] { font-size: 0.95rem; font-weight: 500; }
    div[data-baseweb="tab-list"] { gap: 0.4rem; }

    /* Buttons */
    .stButton button { border-radius: 8px; font-weight: 500; }

    /* Auth screen */
    .auth-header { text-align: center; margin-bottom: 0.4rem; }
    .auth-header h1 { font-size: 2.1rem; margin-bottom: 0.2rem; }
    .auth-header p { opacity: 0.7; font-size: 0.95rem; }

    /* Sidebar account chip */
    .account-chip {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 8px;
        padding: 0.6rem 0.8rem;
        font-size: 0.85rem;
        margin-bottom: 0.8rem;
        word-break: break-all;
    }
    .account-chip .label { opacity: 0.6; font-size: 0.72rem; text-transform: uppercase; }
</style>
"""


def inject_styles() -> None:
    st.markdown(STYLES, unsafe_allow_html=True)


# What an empty cell says out loud.
#
# A blank cell cannot be told apart from a broken one: "this posting did not
# state a salary" and "the salary never got captured" look identical, and the
# second is the one worth investigating. The stored column stays NULL either way,
# so this is a display decision only and nothing downstream has to know about it.
NOT_STATED = "NA"


def status_label(status: str) -> str:
    """Human-friendly label with a colour dot for a status code."""
    return STATUS_LABELS.get(status, status)


def render_metrics(stats: dict) -> None:
    """Top-of-dashboard summary row."""
    columns = st.columns(5)
    cells = [
        ("Total", stats["total"]),
        ("Active", stats["active"]),
        ("Interviews", stats["interviews"]),
        ("Offers", stats["offers"]),
        ("Response Rate", f"{stats['response_rate']}%"),
    ]

    for column, (label, value) in zip(columns, cells):
        column.metric(label, value)


def contact_line(job: dict) -> str:
    """One markdown line naming who to reply to, or "" when nobody is known.

    Shared by the follow-up list and the application editor so the same
    application never describes its contact two different ways. The address is
    a ``mailto:`` and the number a ``tel:`` — the point of capturing them is
    that replying is one click, not one copy-paste.
    """
    name = (job.get("contact_name") or "").strip()
    email = (job.get("contact_email") or "").strip()
    phone = (job.get("contact_phone") or "").strip()

    if not (name or email or phone):
        return ""

    parts = []

    if email:
        parts.append(f"[{name or email}](mailto:{email})")
    elif name:
        # A name with no address still answers "who is handling this?".
        parts.append(f"**{name}**")

    if phone:
        # Spaces and brackets are for reading; the dial string is digits.
        dialable = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        parts.append(f"[{phone}](tel:{dialable})")

    return "↩️ " + " · ".join(parts)


def account_chip(email: str) -> None:
    st.sidebar.markdown(
        f'<div class="account-chip"><div class="label">Signed in as</div>{email}</div>',
        unsafe_allow_html=True,
    )
