"""Every API route must be reachable through the reverse proxy.

Caddy serves one hostname from two processes: an explicit list of paths goes to
FastAPI, and everything else falls through to Streamlit. A route missing from
that list does not 404. It reaches the dashboard, which answers 200 with an
HTML page — so the extension sees a success it cannot parse, and a POST comes
back 405 from a server that was never asked.

That is what happened to /refine-answer. It was added, tested against
127.0.0.1:8000 on the VM, and shipped; through the public hostname it had never
worked at all, and the 405 that said so was read as a quirk of the endpoint.
Testing an API route against localhost skips the one component that decides
whether the route exists in production.
"""

import re
from pathlib import Path

import pytest

from api.server import app

CADDYFILE = Path(__file__).resolve().parent.parent / "deploy" / "Caddyfile"

# Served by Streamlit on purpose: the dashboard owns the root, and FastAPI's
# generated docs are the only other thing under it.
NOT_PROXIED_TO_THE_API = {"/", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def api_paths() -> set[str]:
    """Every path FastAPI serves, as a top-level segment.

    Compared at the first segment because the Caddy list matches prefixes:
    /jobs* covers /jobs/{job_id}/status, and enumerating each parameterised
    path would make this test noise rather than signal.
    """
    paths = set()

    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or path in NOT_PROXIED_TO_THE_API:
            continue
        paths.add("/" + path.lstrip("/").split("/")[0])

    return paths


def proxied_prefixes() -> list[str]:
    """The paths the Caddyfile hands to FastAPI."""
    text = CADDYFILE.read_text(encoding="utf-8")

    # The live block only; the file keeps a commented-out copy for the
    # certificate-less variant, and matching that one would pass forever.
    line = next(
        l for l in text.splitlines()
        if l.strip().startswith("@api path")
    )

    # "/auth/*" covers the /auth segment, so the trailing slash goes too —
    # otherwise the prefix is "/auth/" and the segment "/auth" never matches it.
    return [token.rstrip("*").rstrip("/") for token in line.split()[2:]]


def test_every_api_route_is_proxied_to_the_api():
    proxied = proxied_prefixes()
    missing = sorted(
        path for path in api_paths()
        if not any(path.startswith(prefix) for prefix in proxied)
    )

    assert not missing, (
        "These routes exist in FastAPI but fall through to the dashboard: "
        f"{missing}. Add them to the @api path list in deploy/Caddyfile, or "
        "they will answer 200 with an HTML page in production."
    )


def test_the_pages_the_store_listing_links_to_are_proxied():
    """A reviewer opens these in a browser, which is where this last broke."""
    proxied = proxied_prefixes()

    for public_page in ("/privacy", "/support"):
        assert any(public_page.startswith(p) for p in proxied), (
            f"{public_page} is linked from the Chrome Web Store listing and is "
            "not routed to the API, so it serves the dashboard instead."
        )


def test_the_commented_variant_is_not_what_is_being_checked():
    """Guards the parsing above rather than the config.

    The Caddyfile carries a commented-out second copy of the same block. If
    this test ever read that one, every assertion here would pass whatever the
    live block said.
    """
    line = next(
        l for l in CADDYFILE.read_text(encoding="utf-8").splitlines()
        if l.strip().startswith("@api path")
    )

    assert not line.lstrip().startswith("#")
