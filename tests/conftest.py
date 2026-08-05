"""Shared pytest fixtures.

Every test runs against a temporary DATA_DIR so nothing touches the real
workspace on disk. The environment is set before importing config, because
config resolves its paths at import time.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point the app at a throwaway data directory before config is imported.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="talentpilot-tests-"))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["LOG_DIR"] = str(_TEST_DATA_DIR / "logs")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")


@pytest.fixture
def users_db(tmp_path):
    """An isolated accounts database."""
    return tmp_path / "users.db"


@pytest.fixture
def jobs_db(tmp_path):
    """An isolated, initialised jobs database."""
    import db

    path = tmp_path / "jobs.db"
    db.create_table(path)
    return path
