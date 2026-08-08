"""Runs the extension's in-page autofill tests as part of the Python suite.

The logic is JavaScript that only means anything against a DOM, so the
assertions live in ``test_extension_autofill.js`` and run under jsdom. This
wrapper keeps them inside the one command anyone actually runs.

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
        ["node", "-e", "require('jsdom')"], cwd=_PROJECT_ROOT, capture_output=True
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _node_with_jsdom(), reason="node with jsdom is not available")
def test_in_page_suggestions_behave():
    """Saved answers are matched and offered; nothing is filled without a click."""
    result = subprocess.run(
        ["node", str(_SCRIPT)],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        "extension autofill regressed:\n" f"{result.stdout}\n{result.stderr}"
    )
