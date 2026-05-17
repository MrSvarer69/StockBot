"""Bar validation and normalization.

Canonical shapes are documented in `trading_bot.contracts`.
"""

from __future__ import annotations

import pandas as pd

REQUIRED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


class BarValidationError(ValueError):
    """Raised when a bar DataFrame violates the canonical contract."""


def _check_timestamp_index(index: pd.Index, label: str) -> None:
    if not isinstance(index, pd.DatetimeIndex):
        raise BarValidationError(f"{label} must be a DatetimeIndex, got {type(index).__name__}")
    if index.tz is None:
        raise BarValidationError(f"{label} is timezone-naive; must be tz-aware UTC")
    if str(index.tz) != "UTC":
        raise BarValidationError(f"{label} tz must be UTC, got {index.tz}")
    if index.has_duplicates:
        raise BarValidationError(f"{label} contains duplicate timestamps")
    if not index.is_monotonic_increasing:
        raise BarValidationError(f"{label} is not monotonically increasing")


def validate_bars(df: pd.DataFrame, *, single_symbol: bool) -> None:
    """Raise BarValidationError if `df` violates the canonical bar contract.

    For single_symbol=True: index must be a UTC DatetimeIndex named "timestamp".
    For single_symbol=False: index must be a MultiIndex on ("symbol", "timestamp"),
    with the timestamp level being UTC.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise BarValidationError(f"missing required columns: {missing}")

    if df.empty:
        return

    if single_symbol:
        _check_timestamp_index(df.index, "index")
        if df.index.name != "timestamp":
            raise BarValidationError(
                f'index name must be "timestamp", got {df.index.name!r}'
            )
    else:
        if not isinstance(df.index, pd.MultiIndex):
            raise BarValidationError("multi-symbol frame must have a MultiIndex")
        if list(df.index.names) != ["symbol", "timestamp"]:
            raise BarValidationError(
                f'MultiIndex names must be ("symbol", "timestamp"), got {df.index.names}'
            )
        ts_level = df.index.get_level_values("timestamp")
        _check_timestamp_index(pd.DatetimeIndex(ts_level), "timestamp level")

    for col in ("open", "high", "low", "close"):
        series = df[col]
        if series.isna().any():
            raise BarValidationError(f"column {col!r} contains NaN")
        if (series <= 0).any():
            raise BarValidationError(f"column {col!r} contains non-positive values")

    if (df["volume"] < 0).any():
        raise BarValidationError("column 'volume' contains negative values")

    max_oc = df[["open", "close"]].max(axis=1)
    min_oc = df[["open", "close"]].min(axis=1)
    if (df["high"] < max_oc).any():
        raise BarValidationError("found rows where high < max(open, close)")
    if (df["low"] > min_oc).any():
        raise BarValidationError("found rows where low > min(open, close)")


def normalize_alpaca_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Return a canonical multi-symbol BarsFrame from raw alpaca-py output.

    Alpaca returns a MultiIndex on (symbol, timestamp) with columns
    open, high, low, close, volume, trade_count, vwap. Drop the latter two
    if present and ensure timestamps are tz-aware UTC.
    """
    if df.empty:
        return df.copy()

    out = df.copy()
    drop_cols = [c for c in ("trade_count", "vwap") if c in out.columns]
    if drop_cols:
        out = out.drop(columns=drop_cols)

    if not isinstance(out.index, pd.MultiIndex):
        raise BarValidationError("alpaca frame must have a MultiIndex (symbol, timestamp)")

    if list(out.index.names) != ["symbol", "timestamp"]:
        out.index = out.index.set_names(["symbol", "timestamp"])

    ts_level = out.index.get_level_values("timestamp")
    if ts_level.tz is None:
        ts_level = pd.DatetimeIndex(ts_level).tz_localize("UTC")
    elif str(ts_level.tz) != "UTC":
        ts_level = pd.DatetimeIndex(ts_level).tz_convert("UTC")
    else:
        ts_level = pd.DatetimeIndex(ts_level)
    sym_level = out.index.get_level_values("symbol")
    out.index = pd.MultiIndex.from_arrays([sym_level, ts_level], names=("symbol", "timestamp"))
    out = out.sort_index()
    out["volume"] = out["volume"].astype("int64")
    return out
