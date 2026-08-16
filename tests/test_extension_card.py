"""Runs the extension's in-page card tests as part of the Python suite.

Same arrangement as the other two jsdom suites: the logic only makes sense
against a DOM, so the assertions live in ``test_extension_card.js`` and this
wrapper exists so ``pytest`` covers them rather than leaving the one part of the
extension that draws into somebody else's page outside the command anyone
actually runs.

Skips cleanly when node or jsdom is unavailable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_suffix(".js")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _node_with_jsdom() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        ["node", "-e", "require('jsdom')"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(
    not _node_with_jsdom(), reason="node with jsdom is not available"
)
def test_the_in_page_card_is_drawn_and_stays_out_of_the_page():
    """Placement, restraint, and that the score never enters the page's DOM."""
    result = subprocess.run(
        ["node", str(_SCRIPT)],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        "the in-page match card regressed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
