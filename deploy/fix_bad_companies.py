#!/usr/bin/env python3
"""Repair applications whose `company` holds a job title.

The scraper used to derive the employer by splitting `document.title` and
taking the first segment, which assumed every board writes "Company - Role".
Workable, Ashby and most career pages write the reverse, so rows were saved
with a company of "AI Engineer".

That breaks matching: the tracker's identity is (company, role), so every
later recruiter email about the application fails to find it and opens a
duplicate row instead. Fixing the extractor stops new bad rows; this script
repairs the ones already stored.

The saved posting URL is the evidence used. It is independent of the page
markup that was misread, and it survives the board redesigning its HTML.

  # See what it would do (default — writes nothing):
  python deploy/fix_bad_companies.py

  # Apply, having taken a backup first:
  python deploy/fix_bad_companies.py --apply

  # Supply companies the URL could not reveal (e.g. LinkedIn postings):
  python deploy/fix_bad_companies.py --set 12="Nexus Labs" --set 15="Acme" --apply

On the server, run it as the service account so file ownership is preserved:

  sudo /usr/local/bin/talent-pilot-backup
  cd /opt/talent-pilot
  sudo -u talentpilot .venv/bin/python deploy/fix_bad_companies.py
  sudo -u talentpilot .venv/bin/python deploy/fix_bad_companies.py --apply
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from config import STATUS_RANK, WORKSPACES_DIR  # noqa: E402
from job_fields import (  # noqa: E402
    InvalidJobField,
    company_from_url,
    is_placeholder,
    looks_like_role,
    normalize,
    validate_company,
)


# =====================================================================
# DIAGNOSIS
# =====================================================================
def diagnose(job: dict) -> str | None:
    """Why this row's company is unusable, or None if it is fine."""
    company = normalize(job["company"])
    role = normalize(job["role"])

    if not company:
        return "company is empty"
    if is_placeholder(company):
        return f"company {company!r} is a placeholder"
    if role and company.casefold() == role.casefold():
        return f"company and role are both {company!r}"
    if looks_like_role(company):
        return f"company {company!r} is a job title"

    return None


def suggest(job: dict, overrides: dict[int, str]) -> tuple[str, str]:
    """Return ``(company, source)`` for a broken row, or ``("", reason)``."""
    if job["id"] in overrides:
        return overrides[job["id"]], "--set"

    from_url = company_from_url(job.get("link") or "")

    if from_url:
        try:
            # The suggestion has to survive the same validation as any other
            # company, or the repair would just store a different bad value.
            return validate_company(from_url, job["role"]), "posting URL"
        except InvalidJobField:
            pass

    if not (job.get("link") or "").strip():
        return "", "no posting URL saved"

    return "", "posting URL does not identify the employer"


# =====================================================================
# REPAIR
# =====================================================================
def better_status(left: str, right: str) -> str:
    """The further-along of two statuses, so a merge never loses progress."""
    return left if STATUS_RANK.get(left, 0) >= STATUS_RANK.get(right, 0) else right


def merge_into(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Fold one duplicate row into another, then delete it.

    History is repointed *before* the delete: status_history cascades on
    delete, so removing the row first would take its timeline with it.
    """
    keep = conn.execute("SELECT * FROM jobs WHERE id = ?", (keep_id,)).fetchone()
    drop = conn.execute("SELECT * FROM jobs WHERE id = ?", (drop_id,)).fetchone()

    notes = "\n\n".join(filter(None, [keep["notes"], drop["notes"]])).strip()
    dates = [d for d in (keep["date_applied"], drop["date_applied"]) if d]

    conn.execute(
        """
        UPDATE jobs SET
            status       = ?,
            notes        = ?,
            date_applied = ?,
            link         = COALESCE(NULLIF(?, ''), link),
            jd           = COALESCE(NULLIF(?, ''), jd),
            resume_used  = COALESCE(resume_used, ?),
            updated_at   = ?
        WHERE id = ?
        """,
        (
            better_status(keep["status"], drop["status"]),
            notes,
            min(dates) if dates else keep["date_applied"],
            keep["link"] or drop["link"] or "",
            keep["jd"] or drop["jd"] or "",
            drop["resume_used"],
            db._utcnow(),
            keep_id,
        ),
    )

    conn.execute(
        "UPDATE status_history SET job_id = ? WHERE job_id = ?", (keep_id, drop_id)
    )
    conn.execute("DELETE FROM jobs WHERE id = ?", (drop_id,))


def repair_workspace(
    db_path: Path, overrides: dict[int, str], apply: bool
) -> dict[str, int]:
    """Diagnose and optionally repair one workspace database."""
    counts = {"scanned": 0, "broken": 0, "fixed": 0, "merged": 0, "unresolved": 0}

    jobs = db.get_all_jobs(db_path=db_path)
    counts["scanned"] = len(jobs)

    broken = [(job, reason) for job in jobs if (reason := diagnose(job))]
    counts["broken"] = len(broken)

    if not broken:
        print("  No broken rows.")
        return counts

    for job, reason in broken:
        company, source = suggest(job, overrides)

        print(f"\n  #{job['id']}  {job['company']!r} / {job['role']!r}")
        print(f"      problem: {reason}")
        print(f"      link:    {job.get('link') or '(none)'}")

        if not company:
            print(f"      ACTION:  cannot repair automatically — {source}")
            print(f"               fix by hand: --set {job['id']}=\"Company Name\"")
            counts["unresolved"] += 1
            continue

        with db.connect(db_path) as conn:
            clash = conn.execute(
                """
                SELECT id FROM jobs
                WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)
                  AND id != ?
                """,
                (company, job["role"], job["id"]),
            ).fetchone()

            if clash:
                # The duplicate this bad row caused: an email created the
                # correct entry while the scraped one sat under a job title.
                keep_id, drop_id = sorted((clash["id"], job["id"]))
                print(
                    f"      ACTION:  merge into #{keep_id} as {company!r} "
                    f"(via {source}); #{drop_id} removed"
                )
                if apply:
                    merge_into(conn, keep_id, drop_id)
                    # The surviving row may be the broken one (it can hold the
                    # lower id), so the corrected company is written either way.
                    conn.execute(
                        "UPDATE jobs SET company = ?, updated_at = ? WHERE id = ?",
                        (company, db._utcnow(), keep_id),
                    )
                    db._record_transition(
                        conn, keep_id, None, conn.execute(
                            "SELECT status FROM jobs WHERE id = ?", (keep_id,)
                        ).fetchone()["status"],
                        applied=True, source="Cleanup",
                        reason=f"Merged duplicate #{drop_id}; company set to {company!r}",
                    )
                counts["merged"] += 1
                continue

            print(f"      ACTION:  set company to {company!r} (via {source})")

            if apply:
                conn.execute(
                    "UPDATE jobs SET company = ?, updated_at = ? WHERE id = ?",
                    (company, db._utcnow(), job["id"]),
                )
                # Recorded so the change is visible in the Activity tab and
                # the stage timeline, rather than a value silently differing
                # from what the user last saw.
                db._record_transition(
                    conn,
                    job["id"],
                    job["status"],
                    job["status"],
                    applied=True,
                    source="Cleanup",
                    reason=f"Company corrected from {job['company']!r} to {company!r}",
                )
            counts["fixed"] += 1

    return counts


# =====================================================================
# ENTRY POINT
# =====================================================================
def parse_overrides(values: list[str]) -> dict[int, str]:
    overrides: dict[int, str] = {}

    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"--set expects ID=Company, got {item!r}")
        raw_id, _, name = item.partition("=")
        try:
            overrides[int(raw_id.strip())] = name.strip()
        except ValueError:
            raise SystemExit(f"--set expects a numeric job id, got {raw_id!r}") from None

    return overrides


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair applications whose company holds a job title."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this, nothing is modified.",
    )
    parser.add_argument(
        "--user",
        type=int,
        help="Only this account id. Default: every workspace.",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="ID=COMPANY",
        help="Supply a company the URL could not reveal. Repeatable.",
    )
    args = parser.parse_args()

    overrides = parse_overrides(args.set)

    if args.user:
        workspaces = [WORKSPACES_DIR / str(args.user)]
    else:
        workspaces = sorted(
            (p for p in WORKSPACES_DIR.glob("*") if p.is_dir()),
            key=lambda p: int(p.name) if p.name.isdigit() else 0,
        )

    if not workspaces:
        print(f"No workspaces found under {WORKSPACES_DIR}")
        return 0

    print("DRY RUN — nothing will be modified. Re-run with --apply to write.\n"
          if not args.apply else
          "APPLYING CHANGES.\n")

    totals = {"scanned": 0, "broken": 0, "fixed": 0, "merged": 0, "unresolved": 0}

    for workspace in workspaces:
        db_path = workspace / "jobs.db"
        if not db_path.exists():
            continue

        print(f"Workspace {workspace.name}  ({db_path})")
        # Brings a v1 database up to date so status_history exists before any
        # repair tries to write to it.
        db.create_table(db_path)

        for key, value in repair_workspace(db_path, overrides, args.apply).items():
            totals[key] += value
        print()

    print("=" * 66)
    print(
        f"  scanned {totals['scanned']}   broken {totals['broken']}   "
        f"repaired {totals['fixed']}   merged {totals['merged']}   "
        f"needs input {totals['unresolved']}"
    )
    print("=" * 66)

    if not args.apply and totals["broken"]:
        print("\nNothing was changed. Back up, then re-run with --apply:")
        print("  sudo /usr/local/bin/talent-pilot-backup")
        print("  sudo -u talentpilot .venv/bin/python deploy/fix_bad_companies.py --apply")

    if totals["unresolved"]:
        print(
            f"\n{totals['unresolved']} row(s) need a company supplied by hand — "
            "see the --set lines above, or edit them in the dashboard."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
