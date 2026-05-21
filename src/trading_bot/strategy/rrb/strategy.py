"""Rolling Range Breakout (RRB) strategy. Pure function over BarsFrames.

Mechanism
---------
Donchian-style breakout adapted to the intraday timeframe. For each bar,
the rolling-window high and low over the prior ``lookback_minutes`` bars
define a reference channel. A close *strictly above* the rolling high
fires a long; a close *strictly below* the rolling low fires a short.

This is the same trigger family as ORB (a close-confirmed break of a
recent reference range), differing only in how the reference is built:
ORB anchors to a fixed opening window, RRB uses a sliding window. The
goal is to recover ORB's empirically-validated momentum edge across the
full session, not only the first ~30 minutes.

Why a sliding window matters (microstructure)
---------------------------------------------
The rolling-window high is not arbitrary: it tracks the most recent
liquidity-rejection level on the upside. Limit sell orders and
stops-from-long cluster near recent swing highs (documented in Osler,
"Currency Orders and Exchange-Rate Dynamics", 2003, and in
Easley/Lopez de Prado/O'Hara on order-flow toxicity at level breaks).
A close that pushes *through* such a level has, by definition, consumed
the overhead supply that was hosted there — the path of least resistance
afterward is upward, until the next cluster.

Stop / take (asymmetric by construction)
----------------------------------------
Sized off a bar-scale ATR (true-range ATR with period
``atr_period_bars``). Break-even win rate at the default 2.0R take is
1 / (1 + 2.0) = 33.3%, well below the 47-62% win rate ORB printed on
the same universe.

  stop = entry_close +/- atr_stop_multiplier * atr_bar
  take = entry_close +/- take_r_multiple * |entry_close - stop|

Both stop and take are emitted on every entry row so the bracket-order
wiring in ``execution/alpaca_paper.py`` can attach stop and take
children together.

Score (predictive thesis)
-------------------------
The score column carries ``breakout_strength``:

    breakout_strength = (close - rolling_high) / atr_bar       (longs)
    breakout_strength = (rolling_low - close) / atr_bar        (shorts)

Mechanism by which it should correlate with PnL: a *deeper* close
through the reference level means more of the resting overhead supply
(stops, limit-sells for longs; the mirror for shorts) has been
consumed. Less remaining overhead supply -> a cleaner path of least
resistance afterward -> more room for the take to be reached before
mean-reverting noise dominates. Normalizing by per-bar ATR makes the
score volatility-comparable across symbols with different bar-scale
volatility profiles.

This score is monotonically testable on synthetic data: holding the
reference level fixed and varying only the depth of the breakout close,
score must increase monotonically. That test exists in
``tests/test_strategy/rrb/test_rrb.py`` -- a hard requirement of this
design.

Constraints
-----------
- Pure function: no I/O, no broker calls, no network, no global state.
- UTC-aware bar index required; ET only used for session-time filtering.
- Daytrade-only: a ``flat`` signal is emitted at ``flat_by_et`` so any
  open position is closed same session.
- At most one entry per ``cooldown_minutes`` window per symbol per
  session, separately for long and short directions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

from ..base import empty_signals_frame

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class RRBConfig:
    session_start_et: str
    session_end_et: str
    flat_by_et: str
    latest_entry_et: str | None
    lookback_minutes: int
    require_full_window: bool
    atr_period_bars: int
    atr_stop_multiplier: float
    take_r_multiple: float
    min_breakout_strength: float
    target_size_pct: float
    cooldown_minutes: int
    allow_short: bool

    @staticmethod
    def _parse_time(s: str) -> dtime:
        h, m = s.split(":")
        return dtime(int(h), int(m))

    @property
    def session_start(self) -> dtime:
        return self._parse_time(self.session_start_et)

    @property
    def session_end(self) -> dtime:
        return self._parse_time(self.session_end_et)

    @property
    def flat_time(self) -> dtime:
        return self._parse_time(self.flat_by_et)

    @property
    def latest_entry_time(self) -> dtime | None:
        if self.latest_entry_et is None:
            return None
        return self._parse_time(self.latest_entry_et)


def load_config(path: Path | None = None) -> RRBConfig:
    p = path or _DEFAULT_CONFIG_PATH
    with p.open("r") as f:
        raw = yaml.safe_load(f)
    return RRBConfig(**raw)


def _bar_atr(session: pd.DataFrame, period: int) -> pd.Series:
    """Per-bar SMA-of-true-range ATR over ``period`` bars.

    Identical formulation to VWAP's ``_bar_atr`` -- kept independent (no
    cross-strategy import) to preserve strategy isolation.
    """
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


class RRBStrategy:
    """Rolling-window Donchian-style breakout. See module docstring."""

    def __init__(self, config: RRBConfig):
        self.config = config
        self._logger = logger

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return empty_signals_frame()
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
            raise ValueError("bars must have a tz-aware DatetimeIndex (UTC expected)")

        symbol = self._infer_symbol(bars)
        cfg = self.config

        et_index = bars.index.tz_convert(ET)
        et_dates = pd.Series(et_index.date, index=bars.index)
        session_dates = sorted(et_dates.unique())

        rows: list[dict] = []
        for session_date in session_dates:
            mask = et_dates.values == session_date
            session_full = bars[mask]
            if session_full.empty:
                continue

            session_et = session_full.index.tz_convert(ET)
            in_session_mask = (
                (session_et.time >= cfg.session_start)
                & (session_et.time < cfg.session_end)
            )
            session = session_full[in_session_mask]
            if session.empty:
                continue

            rows.extend(self._scan_session(session, symbol))

        if not rows:
            return empty_signals_frame()
        out = pd.DataFrame(rows)
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        return out[
            [
                "timestamp",
                "symbol",
                "side",
                "target_size_pct",
                "stop_price",
                "take_price",
                "score",
                "or_atr_ratio",
            ]
        ]

    # ------------------------------------------------------------------ scan
    def _scan_session(
        self,
        session: pd.DataFrame,
        symbol: str,
    ) -> list[dict]:
        cfg = self.config
        rows: list[dict] = []

        closes = session["close"].to_numpy(dtype="float64")
        highs = session["high"].to_numpy(dtype="float64")
        lows = session["low"].to_numpy(dtype="float64")
        atr_arr = _bar_atr(session, cfg.atr_period_bars).to_numpy(dtype="float64")
        idx = session.index
        et_times = idx.tz_convert(ET).time

        # Rolling reference windows: the bar at position ``i`` is compared
        # against the high/low of bars [i-lookback, i-1] -- strictly prior
        # bars, so "close > rolling_high" cannot be self-referential.
        lookback = int(cfg.lookback_minutes)
        if lookback < 1:
            raise ValueError("lookback_minutes must be >= 1")

        # Shift-by-one so the rolling window at position i covers prior bars
        # only. min_periods=lookback gates the warm-up.
        high_ser = pd.Series(highs)
        low_ser = pd.Series(lows)
        min_periods = lookback if cfg.require_full_window else 1
        rolling_high = (
            high_ser.shift(1)
            .rolling(window=lookback, min_periods=min_periods)
            .max()
            .to_numpy(dtype="float64")
        )
        rolling_low = (
            low_ser.shift(1)
            .rolling(window=lookback, min_periods=min_periods)
            .min()
            .to_numpy(dtype="float64")
        )

        latest_entry = cfg.latest_entry_time
        flat_time = cfg.flat_time
        cooldown_bars = int(cfg.cooldown_minutes)  # 1-min bars by contract
        last_long_idx: int | None = None
        last_short_idx: int | None = None

        n = len(session)
        for i in range(n):
            close = closes[i]
            a = atr_arr[i]
            r_high = rolling_high[i]
            r_low = rolling_low[i]

            bar_time = et_times[i]
            entry_allowed_by_time = (
                latest_entry is None or bar_time < latest_entry
            )
            if not entry_allowed_by_time:
                continue
            if not np.isfinite(a) or a <= 0:
                continue
            if not (np.isfinite(r_high) and np.isfinite(r_low)):
                # Inside the warm-up window when require_full_window=True.
                continue

            # ---------------- Long breakout ----------------
            long_in_cooldown = (
                last_long_idx is not None
                and (i - last_long_idx) < cooldown_bars
            )
            if not long_in_cooldown and close > r_high:
                strength = (close - r_high) / a
                if strength >= cfg.min_breakout_strength:
                    stop = close - cfg.atr_stop_multiplier * a
                    take = close + cfg.take_r_multiple * (close - stop)
                    rows.append(
                        self._row(
                            idx[i], symbol, "long",
                            stop=stop, take=take, score=strength,
                        )
                    )
                    last_long_idx = i
                    self._logger.debug(
                        "RRB long breakout",
                        extra={
                            "symbol": symbol, "ts": str(idx[i]),
                            "close": close, "rolling_high": r_high,
                            "atr": a, "strength": strength,
                        },
                    )
                    continue  # do not also evaluate short on same bar

            # ---------------- Short breakdown ----------------
            if not cfg.allow_short:
                continue
            short_in_cooldown = (
                last_short_idx is not None
                and (i - last_short_idx) < cooldown_bars
            )
            if not short_in_cooldown and close < r_low:
                strength = (r_low - close) / a
                if strength >= cfg.min_breakout_strength:
                    stop = close + cfg.atr_stop_multiplier * a
                    take = close - cfg.take_r_multiple * (stop - close)
                    rows.append(
                        self._row(
                            idx[i], symbol, "short",
                            stop=stop, take=take, score=strength,
                        )
                    )
                    last_short_idx = i
                    self._logger.debug(
                        "RRB short breakdown",
                        extra={
                            "symbol": symbol, "ts": str(idx[i]),
                            "close": close, "rolling_low": r_low,
                            "atr": a, "strength": strength,
                        },
                    )

        # Per-session flat at first bar at/after flat_by_et.
        flat_mask = np.array([t >= flat_time for t in et_times])
        if flat_mask.any():
            first_flat = int(np.argmax(flat_mask))
            rows.append(
                self._row(
                    idx[first_flat], symbol, "flat",
                    stop=float("nan"), take=float("nan"), score=float("nan"),
                )
            )
        return rows

    def _row(
        self,
        ts,
        symbol: str,
        side: str,
        *,
        stop: float,
        take: float,
        score: float,
    ) -> dict:
        return {
            "timestamp": ts,
            "symbol": symbol,
            "side": side,
            "target_size_pct": float(self.config.target_size_pct),
            "stop_price": stop,
            "take_price": take,
            # Unified cross-strategy conviction column. For RRB it carries
            # breakout_strength = |close - reference_level| / atr_bar.
            "score": score,
            # Mirror into ``or_atr_ratio`` until the legacy picker migrates
            # off it.
            "or_atr_ratio": score,
        }

    @staticmethod
    def _infer_symbol(bars: pd.DataFrame) -> str:
        if "symbol" in bars.columns:
            return str(bars["symbol"].iloc[0])
        if isinstance(bars.index, pd.MultiIndex) and "symbol" in bars.index.names:
            return str(bars.index.get_level_values("symbol")[0])
        raise ValueError(
            "bars must contain a 'symbol' column or a 'symbol' index level"
        )
