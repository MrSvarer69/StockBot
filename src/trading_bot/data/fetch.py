"""Bar fetchers. A Protocol seam plus an Alpaca implementation and a cache-backed one."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import pandas as pd

from .bars import normalize_alpaca_bars, validate_bars
from .cache import BarCache


class CredentialsMissingError(RuntimeError):
    """Raised when broker credentials are required but absent."""


class BarFetcher(Protocol):
    """Returns a canonical single-symbol BarsFrame for [start, end] in UTC."""

    def fetch(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Min"
    ) -> pd.DataFrame: ...


class AlpacaBarFetcher:
    """Pulls historical bars from Alpaca. Paper or live keys both work — read-only."""

    def __init__(self, api_key: str, api_secret: str):
        if not api_key or not api_secret:
            raise CredentialsMissingError(
                "ALPACA_API_KEY and ALPACA_API_SECRET must be set"
            )
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._client = StockHistoricalDataClient(self._api_key, self._api_secret)
        return self._client

    def fetch(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Min"
    ) -> pd.DataFrame:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if timeframe != "1Min":
            raise NotImplementedError(f"timeframe {timeframe} not yet supported")

        client = self._ensure_client()
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        raw = client.get_stock_bars(req).df
        if raw.empty:
            from .cache import _empty_single_symbol_frame

            return _empty_single_symbol_frame()

        canonical = normalize_alpaca_bars(raw)
        if symbol.upper() in canonical.index.get_level_values("symbol"):
            single = canonical.xs(symbol.upper(), level="symbol")
        else:
            sym = canonical.index.get_level_values("symbol")[0]
            single = canonical.xs(sym, level="symbol")
        single.index.name = "timestamp"
        validate_bars(single, single_symbol=True)
        return single


class ParquetBarFetcher:
    """A BarFetcher backed by a local Parquet cache. Useful for backtests + tests."""

    def __init__(self, cache: BarCache):
        self._cache = cache

    def fetch(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Min"
    ) -> pd.DataFrame:
        if timeframe != "1Min":
            raise NotImplementedError(f"timeframe {timeframe} not yet supported")
        return self._cache.read(symbol, start, end)
