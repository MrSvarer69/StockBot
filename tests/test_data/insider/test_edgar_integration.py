"""Integration tests that hit SEC EDGAR. Skipped by default.

Run with:
    uv run pytest -m network tests/test_data/insider/test_edgar_integration.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.data.insider import EdgarClient, TickerMap
from trading_bot.data.insider.index import fetch_quarter_index

pytestmark = pytest.mark.network


def test_ticker_map_live_fetch(tmp_path: Path) -> None:
    client = EdgarClient(cache_dir=tmp_path)
    tmap = TickerMap.from_client(client)
    # Apple's CIK is one of the most stable test values in EDGAR.
    assert tmap.lookup("0000320193") == "AAPL"


def test_fetch_recent_quarter_index(tmp_path: Path) -> None:
    client = EdgarClient(cache_dir=tmp_path)
    # Fetch any historical quarter that definitely exists.
    entries = fetch_quarter_index(client, year=2025, quarter=1)
    assert len(entries) > 0
    sample = entries[0]
    assert sample.form_type in {"4", "4/A"}
    assert len(sample.cik) == 10
