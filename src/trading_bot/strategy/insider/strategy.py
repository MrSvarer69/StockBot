"""InsiderStrategy: bar-aligned adapter for the date-level insider detectors.

The underlying ``cluster_buy_signals`` and ``csuite_conviction_signals``
filters operate on Form 4 filings and return one row per ``(ticker,
signal_date)`` — date-level events with no notion of the intraday bar
timeline. This adapter bridges them to the SignalsFrame contract the
session loop consumes:

  - load all Form 4 parquet at construction (one-shot I/O at the
    data-layer boundary, comparable to the YAML config load other
    strategies do at construction)
  - re-run both detectors over the in-memory filings DataFrame
  - on each ``generate_signals(bars)`` call, look up which signals are
    tradable on the dates spanned by ``bars`` and emit per-bar rows
    (entry at ``entry_et`` ET, plus a forced ``flat`` later)

Anti-lookahead rule
-------------------
A signal is tradable on the *next trading day after* its
``max_filing_date`` (the latest date on which any filing in the signal
was publicly visible on EDGAR). The detectors carry ``max_filing_date``
on each row's ``metadata`` dict — see ``cluster_buy.py`` and
``csuite_conviction.py``. Using ``transaction_date`` would be a
contract violation because Form 4 allows up to two business days between
the transaction and the filing.

Construction-time I/O
---------------------
``generate_signals`` is a pure function of (bars, in-memory filings).
The construction-time parquet read is the only side effect and matches
the pattern other strategies use for ``config.yaml`` loads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

from ..base import empty_signals_frame
from .cluster_buy import cluster_buy_signals
from .csuite_conviction import csuite_conviction_signals

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class InsiderConfig:
    parquet_root: str
    use_cluster_buy: bool
    use_csuite: bool
    cluster_min_insiders: int
    cluster_window_days: int
    csuite_min_value_usd: str  # parsed to Decimal via the property below
    csuite_cooldown_days: int
    holding_days: int
    target_size_pct: float
    atr_period_bars: int
    atr_stop_multiplier: float
    atr_take_r_multiple: float
    entry_et: str
    # Per-position trailing stop policy declared to the session loop. The
    # strategy itself does not implement the ratchet; this flag tells the
    # session layer whether to apply one. Insider defaults False: the
    # multi-day swing horizon and the wide 2.5x ATR stop are sized to
    # absorb normal multi-day drawdowns, which a ratchet would tighten
    # away.
    enable_trailing_stop: bool = False

    @property
    def entry_time(self) -> dtime:
        h, m = self.entry_et.split(":")
        return dtime(int(h), int(m))

    @property
    def csuite_min_value(self) -> Decimal:
        return Decimal(str(self.csuite_min_value_usd))


def load_config(path: Path | None = None) -> InsiderConfig:
    p = path or _DEFAULT_CONFIG_PATH
    with p.open("r") as f:
        raw = yaml.safe_load(f)
    # Coerce csuite_min_value_usd to a string so the Decimal property is
    # deterministic regardless of whether YAML parsed it as int/float.
    raw["csuite_min_value_usd"] = str(raw["csuite_min_value_usd"])
    return InsiderConfig(**raw)


def _bar_atr(session: pd.DataFrame, period: int) -> pd.Series:
    """Per-bar SMA-of-true-range ATR over ``period`` bars."""
    high = session["high"]
    low = session["low"]
    prev_close = session["close"].shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def _load_filings_from_parquet(root: Path) -> pd.DataFrame:
    """Read every parquet file beneath ``root`` into a single DataFrame.

    Returns an empty DataFrame (no schema enforcement) if ``root`` does
    not exist or contains no parquet files — empty input is a valid
    construction state (the strategy simply produces no signals).
    """
    if not root.exists():
        logger.warning(
            "insider parquet root does not exist; adapter will produce no signals",
            extra={"parquet_root": str(root)},
        )
        return pd.DataFrame()
    # pandas+pyarrow can read a partitioned directory directly.
    try:
        df = pd.read_parquet(root, engine="pyarrow")
    except (FileNotFoundError, ValueError) as exc:
        logger.warning(
            "insider parquet read failed; adapter will produce no signals",
            extra={"parquet_root": str(root), "error": str(exc)},
        )
        return pd.DataFrame()
    return df


class InsiderStrategy:
    """Bar-aligned adapter for the date-level insider detectors.

    Construction loads the Form 4 parquet cache and runs the configured
    detectors once. ``generate_signals(bars)`` is then a pure function:
    for each ET date spanned by ``bars``, it looks up insider signals
    whose ``effective_entry_date == that date`` and emits one entry row
    plus a forced flat exit ``holding_days`` later (or at the last bar
    of the entry session if the bars frame stops sooner).
    """

    def __init__(self, config: InsiderConfig):
        self.config = config
        self._logger = logger
        self._signals_by_entry_date: dict[date, list[dict]] = {}

        filings = _load_filings_from_parquet(Path(config.parquet_root))
        if filings.empty:
            self._logger.info(
                "InsiderStrategy constructed with no filings; "
                "generate_signals will return empty",
                extra={"parquet_root": str(config.parquet_root)},
            )
            return

        index = self._build_signal_index(filings)
        self._signals_by_entry_date = index
        self._logger.info(
            "InsiderStrategy constructed",
            extra={
                "n_filings": len(filings),
                "n_signal_dates": len(index),
                "n_signals_total": sum(len(v) for v in index.values()),
            },
        )

    def reload(self) -> int:
        """Re-read the parquet cache and rebuild the in-memory signal index.

        Called by the session loop after an incremental backfill so newly
        appended filings become visible without restarting the process.
        The new index is built fully before being swapped in, so a concurrent
        ``generate_signals`` call always sees a consistent snapshot.

        Returns the new total signal count across all entry dates.
        """
        filings = _load_filings_from_parquet(Path(self.config.parquet_root))
        if filings.empty:
            self._signals_by_entry_date = {}
            self._logger.warning(
                "InsiderStrategy.reload found no filings; index cleared",
                extra={"parquet_root": str(self.config.parquet_root)},
            )
            return 0
        new_index = self._build_signal_index(filings)
        prev_total = sum(len(v) for v in self._signals_by_entry_date.values())
        new_total = sum(len(v) for v in new_index.values())
        self._signals_by_entry_date = new_index
        self._logger.info(
            "InsiderStrategy reloaded",
            extra={
                "n_filings": len(filings),
                "n_signal_dates": len(new_index),
                "n_signals_total": new_total,
                "n_signals_delta": new_total - prev_total,
            },
        )
        return new_total

    def _build_signal_index(
        self, filings: pd.DataFrame
    ) -> dict[date, list[dict]]:
        """Run the detectors, derive the effective entry date per signal,
        and index signals by entry date for O(1) per-date lookup."""
        signal_rows: list[dict] = []
        cfg = self.config

        if cfg.use_cluster_buy:
            cluster = cluster_buy_signals(
                filings,
                min_insiders=cfg.cluster_min_insiders,
                window_days=cfg.cluster_window_days,
            )
            for _, row in cluster.iterrows():
                signal_rows.append(self._normalize_signal(row))

        if cfg.use_csuite:
            csuite = csuite_conviction_signals(
                filings,
                min_value_usd=cfg.csuite_min_value,
                cooldown_days=cfg.csuite_cooldown_days,
            )
            for _, row in csuite.iterrows():
                signal_rows.append(self._normalize_signal(row))

        index: dict[date, list[dict]] = {}
        for sig in signal_rows:
            entry_date = sig["effective_entry_date"]
            index.setdefault(entry_date, []).append(sig)
        return index

    @staticmethod
    def _normalize_signal(row: pd.Series) -> dict:
        """Turn a detector output row into a flat dict + derive the
        effective entry date (one trading day past max_filing_date)."""
        meta = row["metadata"] or {}
        max_filing = meta.get("max_filing_date")
        if max_filing is None:
            # Conservative fallback: if a detector omits max_filing_date,
            # use signal_date as a stand-in. This loses the anti-lookahead
            # guarantee but is safer than dropping the signal silently.
            max_filing = row["signal_date"]
            logger.warning(
                "insider signal missing max_filing_date; falling back to signal_date",
                extra={"ticker": row["ticker"], "signal_date": str(row["signal_date"])},
            )
        max_filing_date = _coerce_date(max_filing)
        effective_entry = _next_trading_day(max_filing_date)
        return {
            "ticker": str(row["ticker"]),
            "signal_date": _coerce_date(row["signal_date"]),
            "strategy": str(row["strategy"]),
            "strength": float(row["strength"]),
            "max_filing_date": max_filing_date,
            "effective_entry_date": effective_entry,
            "metadata": meta,
        }

    # ------------------------------------------------------------------ main
    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return empty_signals_frame()
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
            raise ValueError("bars must have a tz-aware DatetimeIndex (UTC expected)")
        if not self._signals_by_entry_date:
            return empty_signals_frame()

        cfg = self.config
        et_index = bars.index.tz_convert(ET)
        et_dates_series = pd.Series(et_index.date, index=bars.index)
        unique_et_dates = sorted(set(et_dates_series.unique()))

        rows: list[dict] = []
        for session_date in unique_et_dates:
            todays_signals = self._signals_by_entry_date.get(session_date)
            if not todays_signals:
                continue

            session_mask = et_dates_series.values == session_date
            session_bars = bars[session_mask]
            if session_bars.empty:
                continue

            # Find the LATEST bar at-or-after entry_et on this date.
            # Why: the session emits a freshness guard (max_signal_age_minutes)
            # that drops any signal older than ~5 minutes. If the adapter
            # always anchored to the FIRST bar at entry_et (10:00 ET), a
            # bot launched after ~10:05 ET would see every insider signal
            # for today's date as stale. Anchoring to the latest available
            # bar keeps signals fresh on every poll iteration — once the
            # session opens a position from one of these emissions, its
            # open_entries dedup prevents duplicate entries on subsequent
            # polls.
            session_et_times = session_bars.index.tz_convert(ET).time
            entry_mask = np.array(
                [t >= cfg.entry_time for t in session_et_times]
            )
            if not entry_mask.any():
                self._logger.debug(
                    "no bar at-or-after entry_et this session",
                    extra={
                        "session_date": str(session_date),
                        "entry_et": cfg.entry_et,
                    },
                )
                continue
            entry_pos = int(np.where(entry_mask)[0][-1])

            symbols_in_session = (
                set(session_bars["symbol"].unique())
                if "symbol" in session_bars.columns
                else set()
            )

            atr_arr = _bar_atr(session_bars, cfg.atr_period_bars).to_numpy(
                dtype="float64"
            )

            for sig in todays_signals:
                ticker = sig["ticker"]
                if symbols_in_session and ticker not in symbols_in_session:
                    # Bars for this symbol are not in the window — skip
                    # silently, the session will load them tomorrow.
                    continue

                # For multi-symbol bars, the entry bar is the LATEST bar
                # for *this ticker* at-or-after entry_et (same freshness
                # rationale as the session-level entry_pos above). For
                # single-symbol bars, entry_pos is the index into
                # session_bars.
                if "symbol" in session_bars.columns:
                    sym_mask = (
                        session_bars["symbol"].values == ticker
                    ) & entry_mask
                    if not sym_mask.any():
                        continue
                    sym_pos = int(np.where(sym_mask)[0][-1])
                else:
                    sym_pos = entry_pos

                entry_row = session_bars.iloc[sym_pos]
                entry_close = float(entry_row["close"])
                atr_bar = float(atr_arr[sym_pos])
                if not np.isfinite(atr_bar) or atr_bar <= 0:
                    self._logger.debug(
                        "insider entry skipped: ATR not yet warm",
                        extra={
                            "ticker": ticker,
                            "session_date": str(session_date),
                        },
                    )
                    continue

                stop = entry_close - cfg.atr_stop_multiplier * atr_bar
                take = entry_close + cfg.atr_take_r_multiple * (
                    entry_close - stop
                )

                entry_ts = session_bars.index[sym_pos]
                rows.append(
                    {
                        "timestamp": entry_ts,
                        "symbol": ticker,
                        "side": "long",
                        "strategy": "insider",
                        "target_size_pct": float(cfg.target_size_pct),
                        "stop_price": stop,
                        "take_price": take,
                        # Strength in [0, 1] from the underlying detector.
                        "score": float(sig["strength"]),
                        "or_atr_ratio": float(sig["strength"]),
                    }
                )

                # Insider holds across sessions; this flat only fires once
                # we've actually accumulated ``holding_days`` trading days
                # of bars after entry. When the loaded bars don't yet
                # reach that horizon, no flat is emitted and the position
                # carries into the next session (the broker-side bracket
                # remains the overnight safety net for stop/take).
                flat_ts = self._resolve_flat_timestamp(
                    bars=bars,
                    ticker=ticker,
                    entry_ts=entry_ts,
                    holding_days=cfg.holding_days,
                )
                if flat_ts is not None and flat_ts != entry_ts:
                    rows.append(
                        {
                            "timestamp": flat_ts,
                            "symbol": ticker,
                            "side": "flat",
                            "strategy": "insider",
                            "target_size_pct": float(cfg.target_size_pct),
                            "stop_price": float("nan"),
                            "take_price": float("nan"),
                            "score": float("nan"),
                            "or_atr_ratio": float("nan"),
                        }
                    )

        if not rows:
            return empty_signals_frame()
        out = pd.DataFrame(rows)
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        return out[
            [
                "timestamp",
                "symbol",
                "side",
                "strategy",
                "target_size_pct",
                "stop_price",
                "take_price",
                "score",
                "or_atr_ratio",
            ]
        ]

    @staticmethod
    def _resolve_flat_timestamp(
        bars: pd.DataFrame,
        ticker: str,
        entry_ts: pd.Timestamp,
        holding_days: int,
    ) -> pd.Timestamp | None:
        """Find the latest bar for ``ticker`` on the trading day exactly
        ``holding_days`` sessions after ``entry_ts``.

        Returns None when bars don't extend far enough to hit
        ``entry_ts + holding_days`` — the position is intentionally
        carried into the next session in that case. The broker-side
        bracket order remains the overnight stop/take safety net, and
        the session loop's ``flatten_on_exit=False`` default leaves the
        position open across the session boundary so a subsequent
        ``generate_signals`` call (with a wider bars window) can emit
        the flat once the holding horizon is actually reached.
        """
        if "symbol" in bars.columns:
            sym_bars = bars[bars["symbol"] == ticker]
        else:
            sym_bars = bars
        if sym_bars.empty:
            return None

        et_index = sym_bars.index.tz_convert(ET)
        entry_et_date = entry_ts.tz_convert(ET).date()

        target_dates = sorted(
            d for d in {x.date() for x in et_index} if d >= entry_et_date
        )
        if not target_dates:
            return None
        # If bars don't cover enough trading days to reach the actual
        # holding horizon, carry the position rather than collapsing the
        # flat onto the last loaded bar (that bug caused same-day flats
        # in live sessions where bars only span the current session).
        if holding_days >= len(target_dates):
            return None
        idx = holding_days
        target_date = target_dates[idx]

        date_mask = pd.Series(
            [d.date() == target_date for d in et_index], index=sym_bars.index
        )
        target_bars = sym_bars[date_mask]
        if target_bars.empty:
            return None
        return target_bars.index[-1]


def _coerce_date(value) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _next_trading_day(d: date) -> date:
    """Return the next weekday after ``d``. Weekend-only — does not consult
    a holiday calendar; the session-loop's own market-open check filters
    market holidays at runtime."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # 5 == Sat, 6 == Sun
        nxt += timedelta(days=1)
    return nxt
