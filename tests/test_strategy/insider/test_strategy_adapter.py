"""Unit tests for the InsiderStrategy bar-aligned adapter.

These tests build a parquet fixture from synthetic Form 4 rows, then run
the adapter over synthetic minute bars to verify:

  - entry fires on the correct next-trading-day after max_filing_date
  - stop/take derived from a synthetic bar-ATR
  - the forced flat row lands at the right horizon
  - multiple tickers in the same bars frame do not interfere
  - filings older than the bars window are still considered
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_bot.data.insider.schema import Form4Filing, filings_to_arrow_table
from trading_bot.strategy.insider.strategy import (
    InsiderConfig,
    InsiderStrategy,
    load_config,
)

ET = ZoneInfo("America/New_York")


def _make_filing(**overrides: Any) -> Form4Filing:
    base: dict[str, Any] = {
        "accession_no": "acc-0001",
        "filing_date": date(2026, 3, 2),
        "transaction_date": date(2026, 3, 1),
        "ticker": "ACME",
        "issuer_cik": "0000000001",
        "issuer_name": "Acme Corp",
        "insider_cik": "0000000100",
        "insider_name": "A Person",
        "insider_title": "Director",
        "is_officer": False,
        "is_director": True,
        "is_ten_percent_owner": False,
        "transaction_code": "P",
        "shares": Decimal("1500"),
        "price_per_share": Decimal("50.0000"),
        "value_usd": Decimal("75000.0000"),
        "is_10b5_1": False,
        "shares_owned_after": Decimal("10000.0000"),
    }
    base.update(overrides)
    # Re-decimalize numeric overrides if they came in as int/str.
    for f in ("shares", "price_per_share", "value_usd", "shares_owned_after"):
        base[f] = Decimal(str(base[f]))
    return Form4Filing(**base)


def _make_cluster_for_ticker(
    ticker: str,
    *,
    base_txn_date: date = date(2026, 3, 1),
    max_filing_date: date | None = None,
) -> list[Form4Filing]:
    """Three distinct insiders buying in a 3-day window — passes the
    cluster_buy default threshold (3 insiders, 10-day window)."""
    if max_filing_date is None:
        max_filing_date = base_txn_date + timedelta(days=2)
    rows = []
    for i, cik in enumerate(("100", "101", "102")):
        txn = base_txn_date + timedelta(days=i)
        # Stagger filing dates so the latest filing lands on max_filing_date.
        filing = (
            max_filing_date
            if i == 2
            else txn + timedelta(days=1)
        )
        rows.append(
            _make_filing(
                accession_no=f"{ticker}-acc-{i}",
                ticker=ticker,
                insider_cik=cik,
                insider_name=f"Insider {i}",
                transaction_date=txn,
                filing_date=filing,
            )
        )
    return rows


def _write_parquet_fixture(
    root: Path, filings: list[Form4Filing]
) -> None:
    """Write a single parquet file under ``root`` containing the filings.

    Uses the canonical FORM4_PARQUET_SCHEMA so the adapter sees the same
    on-disk shape it would see from the real backfill cache.
    """
    import pyarrow.parquet as pq

    root.mkdir(parents=True, exist_ok=True)
    table = filings_to_arrow_table(filings)
    pq.write_table(
        table,
        root / "fixture.parquet",
        compression="snappy",
        use_dictionary=False,
    )


def _make_bars(
    *,
    symbols: list[str],
    session_dates: list[date],
    base_price: float = 100.0,
    minutes: int = 390,
) -> pd.DataFrame:
    """Build a multi-symbol 1-min bars DataFrame with a constant price drift
    and constant volume — enough to exercise the ATR calc + the entry
    bar selection. UTC index, ET-aligned 09:30 session start."""
    frames = []
    for sym_i, symbol in enumerate(symbols):
        for sess_i, sess_date in enumerate(session_dates):
            start = pd.Timestamp.combine(
                sess_date, dtime(9, 30)
            ).tz_localize(ET).tz_convert("UTC")
            idx = pd.date_range(
                start, periods=minutes, freq="1min", tz="UTC"
            )
            # Small symbol-specific offset so ATR is non-zero.
            base = base_price + sym_i * 10.0
            closes = [base + 0.1 * (i % 5) for i in range(minutes)]
            opens = [closes[0]] + closes[:-1]
            highs = [max(o, c) + 0.2 for o, c in zip(opens, closes)]
            lows = [min(o, c) - 0.2 for o, c in zip(opens, closes)]
            frames.append(
                pd.DataFrame(
                    {
                        "open": opens,
                        "high": highs,
                        "low": lows,
                        "close": closes,
                        "volume": [1000] * minutes,
                        "symbol": [symbol] * minutes,
                    },
                    index=idx,
                )
            )
    return pd.concat(frames, axis=0).sort_index()


def _build_config(parquet_root: Path, **overrides: Any) -> InsiderConfig:
    base = load_config()
    return replace(base, parquet_root=str(parquet_root), **overrides)


# ---------------------------------------------------------------------------
# Entry firing
# ---------------------------------------------------------------------------


def test_entry_fires_on_next_trading_day_after_max_filing(tmp_path: Path) -> None:
    # Cluster max_filing_date = Mon 2026-03-09; entry_date = Tue 2026-03-10.
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),  # Wed
        max_filing_date=date(2026, 3, 9),  # Mon
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    bars = _make_bars(
        symbols=["ACME"],
        session_dates=[
            date(2026, 3, 9),
            date(2026, 3, 10),  # expected entry day
            date(2026, 3, 11),
        ],
    )

    signals = strat.generate_signals(bars)
    entries = signals[signals["side"] == "long"]
    assert len(entries) == 1, signals
    entry = entries.iloc[0]
    entry_et = entry["timestamp"].tz_convert(ET)
    assert entry_et.date() == date(2026, 3, 10)
    assert entry_et.time() >= cfg.entry_time
    assert entry["symbol"] == "ACME"


def test_entry_skips_weekend_to_monday(tmp_path: Path) -> None:
    # max_filing_date = Fri 2026-03-06 -> entry on Mon 2026-03-09.
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 2),
        max_filing_date=date(2026, 3, 6),
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    bars = _make_bars(
        symbols=["ACME"],
        session_dates=[
            date(2026, 3, 6),
            date(2026, 3, 9),
            date(2026, 3, 10),
        ],
    )

    signals = strat.generate_signals(bars)
    entries = signals[signals["side"] == "long"]
    assert len(entries) == 1
    assert entries.iloc[0]["timestamp"].tz_convert(ET).date() == date(2026, 3, 9)


# ---------------------------------------------------------------------------
# Stop / take
# ---------------------------------------------------------------------------


def test_stop_and_take_derived_from_bar_atr(tmp_path: Path) -> None:
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    bars = _make_bars(
        symbols=["ACME"],
        session_dates=[date(2026, 3, 10), date(2026, 3, 11)],
    )

    signals = strat.generate_signals(bars)
    entry = signals[signals["side"] == "long"].iloc[0]
    entry_close = float(bars.loc[entry["timestamp"], "close"])
    # Stop below entry, take above entry, with R-multiple geometry.
    assert entry["stop_price"] < entry_close
    assert entry["take_price"] > entry_close
    risk = entry_close - entry["stop_price"]
    reward = entry["take_price"] - entry_close
    assert reward == pytest.approx(cfg.atr_take_r_multiple * risk, rel=1e-6)


# ---------------------------------------------------------------------------
# Flat
# ---------------------------------------------------------------------------


def test_flat_signal_emitted_at_holding_horizon(tmp_path: Path) -> None:
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)

    # Force a short holding horizon so the flat lands inside the loaded
    # bars window. holding_days=1 -> flat lands on the same session as entry.
    cfg = _build_config(tmp_path, holding_days=1)
    strat = InsiderStrategy(cfg)
    bars = _make_bars(
        symbols=["ACME"],
        session_dates=[
            date(2026, 3, 10),
            date(2026, 3, 11),
            date(2026, 3, 12),
        ],
    )

    signals = strat.generate_signals(bars)
    flats = signals[signals["side"] == "flat"]
    assert len(flats) == 1
    flat_et = flats.iloc[0]["timestamp"].tz_convert(ET)
    # holding_days=1 means the second weekday in the bars window after
    # entry (entry on 2026-03-10 -> flat on 2026-03-11).
    assert flat_et.date() == date(2026, 3, 11)


# ---------------------------------------------------------------------------
# Multi-symbol isolation
# ---------------------------------------------------------------------------


def test_multiple_tickers_do_not_interfere(tmp_path: Path) -> None:
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    ) + _make_cluster_for_ticker(
        "BETA",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    bars = _make_bars(
        symbols=["ACME", "BETA"],
        session_dates=[date(2026, 3, 10), date(2026, 3, 11)],
    )

    signals = strat.generate_signals(bars)
    entries = signals[signals["side"] == "long"]
    assert len(entries) == 2
    assert set(entries["symbol"]) == {"ACME", "BETA"}


# ---------------------------------------------------------------------------
# Lookahead / off-window
# ---------------------------------------------------------------------------


def test_filings_older_than_bars_window_still_considered(tmp_path: Path) -> None:
    """A cluster that filed weeks before the bars window should not fire an
    entry today — the effective_entry_date is in the past. But the loader
    must still ingest those filings (no silent drop)."""
    # Cluster ending Feb 2026 — entry date = late-Feb, well before our bars.
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 2, 2),
        max_filing_date=date(2026, 2, 6),
    )
    # Add a fresh cluster whose entry IS inside the bars window.
    filings += _make_cluster_for_ticker(
        "BETA",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    bars = _make_bars(
        symbols=["ACME", "BETA"],
        session_dates=[date(2026, 3, 10), date(2026, 3, 11)],
    )

    signals = strat.generate_signals(bars)
    entries = signals[signals["side"] == "long"]
    # Only BETA fires; ACME's entry was in February and is not in the bars window.
    assert set(entries["symbol"]) == {"BETA"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_parquet_root_produces_no_signals(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)  # tmp_path exists but is empty
    strat = InsiderStrategy(cfg)
    bars = _make_bars(symbols=["ACME"], session_dates=[date(2026, 3, 10)])
    signals = strat.generate_signals(bars)
    assert signals.empty


def test_nonexistent_parquet_root_produces_no_signals(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path / "does_not_exist")
    strat = InsiderStrategy(cfg)
    bars = _make_bars(symbols=["ACME"], session_dates=[date(2026, 3, 10)])
    signals = strat.generate_signals(bars)
    assert signals.empty


def test_empty_bars_produces_empty_signals(tmp_path: Path) -> None:
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    signals = strat.generate_signals(pd.DataFrame())
    assert signals.empty


def test_entry_anchors_to_latest_bar_for_freshness(tmp_path: Path) -> None:
    # Why: the session emits a max_signal_age_minutes guard (~5 min by
    # default) that drops stale signals. If the adapter anchored to the
    # FIRST bar at entry_et every poll, every poll after the first 5
    # minutes would see the signal as stale. Confirm the adapter uses
    # the LATEST bar in the loaded window so it stays fresh on every
    # iteration of the session loop.
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    # Build a bars frame whose 2026-03-10 session has bars all the way
    # to 15:30 ET. The adapter should anchor entry to the 15:30 bar, not
    # the 10:00 bar.
    bars = _make_bars(
        symbols=["ACME"],
        session_dates=[date(2026, 3, 10)],
        minutes=390,  # full RTH session 09:30 -> 16:00 ET
    )
    signals = strat.generate_signals(bars)
    entries = signals[signals["side"] == "long"]
    assert len(entries) == 1
    entry_et = entries.iloc[0]["timestamp"].tz_convert(ET)
    # 09:30 + 389 minutes = 15:59 ET — the last full minute bar before
    # 16:00 close. Anchoring to FIRST entry-eligible bar would give 10:00.
    assert entry_et.time() >= dtime(15, 0), (
        f"expected entry at the latest bar (>=15:00 ET) for freshness, "
        f"got {entry_et.time()}"
    )


def test_signal_score_in_unit_interval(tmp_path: Path) -> None:
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    bars = _make_bars(
        symbols=["ACME"],
        session_dates=[date(2026, 3, 10), date(2026, 3, 11)],
    )

    signals = strat.generate_signals(bars)
    entries = signals[signals["side"] == "long"]
    score = float(entries.iloc[0]["score"])
    assert 0.0 <= score <= 1.0
    # score and or_atr_ratio mirror by design
    assert entries.iloc[0]["or_atr_ratio"] == score


# ---------------------------------------------------------------------------
# Incremental reload — added 2026-05-20 alongside refresh_cache_to_today
# ---------------------------------------------------------------------------


def test_reload_picks_up_new_filings_written_to_parquet(tmp_path: Path) -> None:
    """A second cluster added to the parquet root after construction should
    become visible in generate_signals after reload(), without restarting
    the process."""
    # Initial cache: ACME cluster only.
    initial = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, initial)

    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    initial_total = sum(len(v) for v in strat._signals_by_entry_date.values())
    assert initial_total >= 1

    # Simulate a refresh_cache_to_today run that appended a new cluster for
    # WIDGET. Write it to a NEW file so pyarrow.dataset picks it up; the
    # backfill cache also produces one file per write call.
    import pyarrow.parquet as pq

    added = _make_cluster_for_ticker(
        "WIDGET",
        base_txn_date=date(2026, 3, 11),
        max_filing_date=date(2026, 3, 16),
    )
    pq.write_table(
        filings_to_arrow_table(added),
        tmp_path / "added.parquet",
        compression="snappy",
        use_dictionary=False,
    )

    new_total = strat.reload()
    assert new_total > initial_total, (
        f"reload should pick up the appended cluster: "
        f"initial={initial_total}, new={new_total}"
    )

    # WIDGET's max_filing_date is Mon 2026-03-16 -> entry on Tue 2026-03-17.
    bars = _make_bars(symbols=["WIDGET"], session_dates=[date(2026, 3, 17)])
    signals = strat.generate_signals(bars)
    entries = signals[signals["side"] == "long"]
    assert len(entries) == 1, f"WIDGET entry should fire post-reload: {signals}"
    assert entries.iloc[0]["symbol"] == "WIDGET"


def test_reload_with_empty_parquet_root_clears_index(tmp_path: Path) -> None:
    """If the parquet cache somehow becomes empty between construction and
    reload, the in-memory index is cleared rather than left stale."""
    filings = _make_cluster_for_ticker(
        "ACME",
        base_txn_date=date(2026, 3, 4),
        max_filing_date=date(2026, 3, 9),
    )
    _write_parquet_fixture(tmp_path, filings)
    cfg = _build_config(tmp_path)
    strat = InsiderStrategy(cfg)
    assert len(strat._signals_by_entry_date) >= 1

    # Remove every parquet file under the root.
    for p in tmp_path.rglob("*.parquet"):
        p.unlink()

    new_total = strat.reload()
    assert new_total == 0
    assert strat._signals_by_entry_date == {}
