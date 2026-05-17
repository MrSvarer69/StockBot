"""Test isolation for the execution package.

`run_session` persists `session_start_equity` to disk on first successful
poll. The default path is under the repo's `data/` directory; redirect it
to a tmp_path so unit tests don't write into the working tree and don't
contaminate each other through a shared per-ET-date file.
"""

from __future__ import annotations

import pytest

import trading_bot.execution.session as session_module


@pytest.fixture(autouse=True)
def _isolated_ops_dirs(tmp_path, monkeypatch):
    """Redirect both session_state and unreconciled-sentinel directories to
    a per-test tmp path so unit tests don't write into the working tree and
    don't contaminate each other through shared per-ET-date or sentinel files.
    """
    monkeypatch.setattr(
        session_module,
        "_DEFAULT_SESSION_STATE_DIR",
        tmp_path / "session_state",
    )
    monkeypatch.setattr(
        session_module,
        "_DEFAULT_UNRECONCILED_DIR",
        tmp_path / "unreconciled",
    )
    yield {
        "session_state": tmp_path / "session_state",
        "unreconciled": tmp_path / "unreconciled",
    }
