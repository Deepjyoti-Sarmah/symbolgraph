"""Shared fixtures: tmp_db with WAL cleanup, tmp_repo."""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_codex_home():
    """Keep the suite out of the developer's real ~/.codex/config.toml.

    Codex's MCP config is global, so any test reaching `sg init --agent all`
    writes outside its tmpdir. Redirecting CODEX_HOME works identically on
    POSIX and Windows, unlike patching HOME (which Windows ignores in favour
    of USERPROFILE).
    """
    with tempfile.TemporaryDirectory() as codex_home:
        import os

        previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = codex_home
        try:
            yield codex_home
        finally:
            if previous is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous


@pytest.fixture
def tmp_db(tmp_path):
    """Temp SQLite path with automatic WAL/SHM cleanup."""
    db = tmp_path / "index.sqlite"
    yield str(db)
    # Cleanup WAL sidecars that sqlite WAL mode leaves behind
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(db) + suffix) if suffix else db
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


@pytest.fixture
def tmp_repo(tmp_path):
    """Empty repo directory for indexing tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo
