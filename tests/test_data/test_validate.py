from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.data.bars import BarValidationError, validate_bars


def _good_single_symbol(n: int = 3) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        pd.date_range("2026-01-02 14:30", periods=n, freq="1min", tz="UTC"),
        name="timestamp",
    )
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000] * n,
        },
        index=idx,
    )


def test_happy_path_single_symbol() -> None:
    validate_bars(_good_single_symbol(), single_symbol=True)


def test_empty_frame_with_columns_passes() -> None:
    df = _good_single_symbol(0)
    validate_bars(df, single_symbol=True)


def test_missing_columns_rejected() -> None:
    df = _good_single_symbol().drop(columns=["volume"])
    with pytest.raises(BarValidationError, match="missing required columns"):
        validate_bars(df, single_symbol=True)


def test_naive_index_rejected() -> None:
    df = _good_single_symbol()
    df.index = df.index.tz_localize(None)
    with pytest.raises(BarValidationError, match="tz-aware"):
        validate_bars(df, single_symbol=True)


def test_non_utc_index_rejected() -> None:
    df = _good_single_symbol()
    df.index = df.index.tz_convert("America/New_York")
    with pytest.raises(BarValidationError, match="UTC"):
        validate_bars(df, single_symbol=True)


def test_duplicate_timestamps_rejected() -> None:
    df = _good_single_symbol()
    dup_idx = pd.DatetimeIndex([df.index[0]] * len(df), tz="UTC", name="timestamp")
    df.index = dup_idx
    with pytest.raises(BarValidationError, match="duplicate"):
        validate_bars(df, single_symbol=True)


def test_non_monotonic_rejected() -> None:
    df = _good_single_symbol()
    df = df.iloc[::-1]
    with pytest.raises(BarValidationError, match="monotonic"):
        validate_bars(df, single_symbol=True)


def test_non_positive_close_rejected() -> None:
    df = _good_single_symbol()
    df.loc[df.index[0], "close"] = 0.0
    with pytest.raises(BarValidationError, match="non-positive"):
        validate_bars(df, single_symbol=True)


def test_nan_open_rejected() -> None:
    df = _good_single_symbol()
    df.loc[df.index[0], "open"] = float("nan")
    with pytest.raises(BarValidationError, match="NaN"):
        validate_bars(df, single_symbol=True)


def test_negative_volume_rejected() -> None:
    df = _good_single_symbol()
    df.loc[df.index[0], "volume"] = -1
    with pytest.raises(BarValidationError, match="volume"):
        validate_bars(df, single_symbol=True)


def test_high_below_max_oc_rejected() -> None:
    df = _good_single_symbol()
    df.loc[df.index[0], "high"] = 50.0
    with pytest.raises(BarValidationError, match="high"):
        validate_bars(df, single_symbol=True)


def test_low_above_min_oc_rejected() -> None:
    df = _good_single_symbol()
    df.loc[df.index[0], "low"] = 200.0
    with pytest.raises(BarValidationError, match="low"):
        validate_bars(df, single_symbol=True)


def test_wrong_index_name_rejected() -> None:
    df = _good_single_symbol()
    df.index.name = "ts"
    with pytest.raises(BarValidationError, match="timestamp"):
        validate_bars(df, single_symbol=True)


def test_multi_symbol_happy_path() -> None:
    single = _good_single_symbol()
    arrays = [
        ["SPY"] * len(single),
        list(single.index),
    ]
    mi = pd.MultiIndex.from_arrays(arrays, names=("symbol", "timestamp"))
    df = single.copy()
    df.index = mi
    validate_bars(df, single_symbol=False)


def test_multi_symbol_requires_multiindex() -> None:
    df = _good_single_symbol()
    with pytest.raises(BarValidationError, match="MultiIndex"):
        validate_bars(df, single_symbol=False)
