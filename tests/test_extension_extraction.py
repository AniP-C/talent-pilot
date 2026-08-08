"""Runs the extension's extraction tests as part of the Python suite.

The logic under test is JavaScript that only makes sense against a DOM, so the
assertions live in ``test_extension_extraction.js`` and run under jsdom. This
wrapper exists so ``pytest`` covers them too rather than leaving a whole class
of regressions outside the one command anyone actually runs.

Skips cleanly when node or jsdom is unavailable — the Python tests must stay
runnable on a machine without a JavaScript toolchain.
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
def test_extension_extracts_the_company_not_the_role():
    """Every board's title format, plus the case where no company exists."""
    result = subprocess.run(
        ["node", str(_SCRIPT)],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        "extension extraction regressed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
