from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from trading_bot.data import CredentialsMissingError, bars_for_range

_START = datetime(2026, 1, 2, tzinfo=UTC)
_END = datetime(2026, 1, 9, tzinfo=UTC)


class _RecordingCache:
    """Duck-typed stand-in for BarCache that records which path was taken."""

    def __init__(self) -> None:
        self.read_calls: list[tuple] = []
        self.read_or_fetch_calls: list[tuple] = []

    def read(self, symbol, start, end) -> pd.DataFrame:
        self.read_calls.append((symbol, start, end))
        return pd.DataFrame({"close": [1.0]})

    def read_or_fetch(self, symbol, start, end, fetcher) -> pd.DataFrame:
        self.read_or_fetch_calls.append((symbol, start, end, fetcher))
        return pd.DataFrame({"close": [2.0]})


def test_use_cache_without_credentials_falls_back_to_cache_only(monkeypatch):
    """use_cache=True + no creds → best-effort cache-only read, no fetch."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    cache = _RecordingCache()

    out = bars_for_range("SPY", _START, _END, cache=cache, use_cache=True)

    assert cache.read_calls == [("SPY", _START, _END)]
    assert cache.read_or_fetch_calls == []
    assert not out.empty


def test_forced_fetch_without_credentials_raises(monkeypatch):
    """use_cache=False + no creds → fail fast (can't fetch without keys)."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    cache = _RecordingCache()

    with pytest.raises(CredentialsMissingError):
        bars_for_range("SPY", _START, _END, cache=cache, use_cache=False)


def test_use_cache_with_credentials_uses_read_or_fetch(monkeypatch):
    """use_cache=True + creds present → read_or_fetch (fills gaps from Alpaca)."""
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    cache = _RecordingCache()

    out = bars_for_range("SPY", _START, _END, cache=cache, use_cache=True)

    assert cache.read_or_fetch_calls and cache.read_calls == []
    # The fetcher handed to read_or_fetch carries the supplied credentials.
    _, _, _, fetcher = cache.read_or_fetch_calls[0]
    assert fetcher.__class__.__name__ == "AlpacaBarFetcher"
    assert not out.empty


def test_explicit_credentials_override_environment(monkeypatch):
    """Passed-in creds take precedence over the environment defaults."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    cache = _RecordingCache()

    bars_for_range(
        "SPY", _START, _END, cache=cache, use_cache=True, api_key="k", api_secret="s"
    )

    # Creds were supplied explicitly, so the gap-filling path is taken.
    assert cache.read_or_fetch_calls and cache.read_calls == []
