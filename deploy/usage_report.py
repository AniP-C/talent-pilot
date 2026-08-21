"""Who is using Talent Pilot, and how much.

Run on the server, where the accounts database lives:

    cd /opt/talent-pilot
    sudo -u talentpilot DATA_DIR=/var/lib/talent-pilot LOG_DIR=/var/log/talent-pilot \\
      .venv/bin/python deploy/usage_report.py

    # only the last 30 days
    ... deploy/usage_report.py --days 30

    # machine-readable, for a spreadsheet
    ... deploy/usage_report.py --csv > usage.csv

The same numbers now appear in the dashboard's admin panel, which is the
easier way to read them. This stays because it needs nothing but a shell: it
still answers when the dashboard is the thing that is broken, and it is what
pipes into a spreadsheet or a cron job.

What has not changed is who may see it. The panel is gated on ADMIN_EMAILS,
which only someone who can edit the root-owned environment file may set — so
reaching either version of this report still costs server access, and no
ordinary signed-in user can find it by guessing a URL.
"""

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usage  # noqa: E402  - after the path insert


# Column order for the table, shared with the admin panel so the two cannot
# label the same number differently.
COLUMNS = usage.REPORT_COLUMNS


def render_table(accounts: list[dict], days) -> None:
    window = f"last {days} days" if days else "all time"
    print(f"\nUsage per account ({window})\n")

    headers = ["#", "Email", "Paid units"] + [label for label, _ in COLUMNS] + ["Last seen"]
    rows = [
        [
            str(account["user_id"]),
            account["email"],
            str(account["paid_units"]),
            *[str(account["events"][event]) for _, event in COLUMNS],
            (account["last_login_at"] or "never")[:16],
        ]
        for account in accounts
    ]

    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def line(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    print(line(headers))
    print("  ".join("-" * width for width in widths))

    for row in rows:
        print(line(row))

    print()
    # "Paid units" is the number that decides a price: every one of them is a
    # model call somebody was billed for by Google.
    print(f"{len(accounts)} account(s). 'Paid units' counts model calls: "
          f"{', '.join(event for event in usage.PAID_EVENTS)}.")


def render_csv(accounts: list[dict]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(
        ["user_id", "email", "created_at", "last_login_at", "paid_units"]
        + [event for _, event in COLUMNS]
    )
    for account in accounts:
        writer.writerow(
            [
                account["user_id"],
                account["email"],
                account["created_at"],
                account["last_login_at"] or "",
                account["paid_units"],
            ]
            + [account["events"][event] for _, event in COLUMNS]
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=None, help="Only count the last N days."
    )
    parser.add_argument("--csv", action="store_true", help="Emit CSV instead of a table.")
    parser.add_argument(
        "--recent", type=int, default=0, help="Also show the N most recent events."
    )
    args = parser.parse_args()

    accounts = usage.per_user(days=args.days)

    if args.csv:
        render_csv(accounts)
        return 0

    render_table(accounts, args.days)

    totals = usage.totals(days=args.days)
    print("\nTotals across all accounts")
    for label, event in COLUMNS:
        print(f"  {label:<16} {totals[event]}")

    print(
        f"\nActive in the last 30 days: {usage.active_accounts(30)} "
        f"of {len(accounts)} account(s)."
    )

    if args.recent:
        print(f"\nMost recent {args.recent} events")
        for entry in usage.recent(limit=args.recent):
            print(
                f"  {entry['occurred_at'][:19]}  {entry['email']:<30} "
                f"{entry['event']:<14} x{entry['quantity']}  via {entry['source']}"
            )

    return 0


if __name__ == "__main__":
    # Surfaced rather than raised as a traceback: forgetting DATA_DIR is the
    # normal way this is run wrong, and the fix is a one-line environment
    # variable rather than anything about the code.
    if not os.getenv("DATA_DIR"):
        print(
            "Warning: DATA_DIR is not set, so this reads the local ./data "
            "database rather than the deployment's.\n",
            file=sys.stderr,
        )
    raise SystemExit(main())
