"""Pullback-to-EMA midday strategy. Pure function over BarsFrames.

Mechanism
---------
A trending intraday move pulls back to the fast EMA and resumes. The setup
codifies that pattern with three layered checks so any one of them firing
alone does not trigger an entry:

  1. Trend regime. EMA_fast > EMA_slow and EMA_fast is rising over the last
     ``trend_slope_lookback`` bars (long side); mirrored for shorts.

  2. Pullback excursion. Within the last ``pullback_lookback_bars`` (the
     current bar excluded), at least ``min_pullback_bars`` bars closed on
     the wrong side of EMA_fast AND the maximum excursion (distance of the
     furthest close from EMA_fast) exceeded ``pullback_min_atr`` x bar-ATR.
     A purely-cosmetic single-bar dip does not qualify.

  3. Reclaim. The current bar (and ``reclaim_confirm_bars - 1`` prior bars)
     closes back on the trend side of EMA_fast.

Time window
-----------
Entries are gated to the midday window ``[earliest_entry_et,
latest_entry_et)``. This strategy is the "no morning gap" cover for ORB; it
deliberately does not compete with ORB at the open and does not chase
late-day reversals.

Stop / take
-----------
Bar-scale ATR driven, symmetric construction.

    stop = entry_close -/+ atr_stop_multiplier * atr_bar
    take = entry_close +/- take_r_multiple * |entry_close - stop|

The bracket order in ``execution/alpaca_paper.py`` requires both to be
finite. The strategy enforces this by construction.

Score
-----
For picker comparability with ORB's OR/session-ATR ratio, this strategy
emits

    score = (EMA_fast - EMA_slow) / atr_bar * pullback_depth

with both factors volatility-normalised. Higher score = stronger trend AND
deeper pullback, both of which empirically correlate with reversal-back-to-
trend quality.

Constraints
-----------
- Pure function: no I/O, no broker calls, no network.
- UTC-aware bar index required; ET only used for session-time gating.
- Daytrade-only: a ``flat`` signal at ``flat_by_et`` closes any open
  position by end of session.
- ``cooldown_minutes`` throttles repeat entries on the same symbol.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

from ..base import empty_signals_frame, infer_symbol

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class PullbackConfig:
    session_start_et: str
    session_end_et: str
    flat_by_et: str
    earliest_entry_et: str
    latest_entry_et: str
    ema_fast_period: int
    ema_slow_period: int
    trend_slope_lookback: int
    # Minimum (EMA_fast - EMA_slow) / ATR_bar required at the entry bar.
    # On 1-min bars, EMA crossings happen constantly in chop -- a positive
    # gap is necessary but not sufficient for "real" trend.
    min_trend_strength: float
    pullback_lookback_bars: int
    min_pullback_bars: int
    pullback_min_atr: float
    reclaim_confirm_bars: int
    atr_period_bars: int
    atr_stop_multiplier: float
    take_r_multiple: float
    target_size_pct: float
    cooldown_minutes: int
    allow_short: bool
    # When True, prior-session bars injected via ``set_prior_session_bars``
    # are prepended to today's frame for indicator warmup only.
    warmup_from_prior_session: bool = True
    # Per-position trailing stop policy declared to the session loop. The
    # strategy itself does not implement the ratchet; this flag tells the
    # session layer whether to apply one. Pullback defaults True (intraday
    # daytrade -- protect afternoon gains the same way ORB does).
    enable_trailing_stop: bool = True

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
    def earliest_entry_time(self) -> dtime:
        return self._parse_time(self.earliest_entry_et)

    @property
    def latest_entry_time(self) -> dtime:
        return self._parse_time(self.latest_entry_et)


def load_config(path: Path | None = None) -> PullbackConfig:
    p = path or _DEFAULT_CONFIG_PATH
    with p.open("r") as f:
        raw = yaml.safe_load(f)
    return PullbackConfig(**raw)


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average with adjust=False (so the
    recursion matches the "online" form a live system would compute)."""
    return series.ewm(span=period, adjust=False, min_periods=1).mean()


def _normalize_score(trend_strength: float, depth: float) -> float:
    """Compress (trend_strength x depth) into ORB's typical [0.5, 2.5] range.

    The picker ranks candidates from ALL strategies by ``score``. ORB's
    OR/ATR ratio sits roughly in [0.5, 2.5]; the insider strength score
    sits roughly in [0, 1]. Without compression, the raw pullback metric
    (trend_strength x depth, easily 3-30) would dominate every iteration
    and starve the other strategies.
    """
    raw = max(0.0, float(trend_strength)) * max(0.0, float(depth))
    if raw <= 0:
        return 0.0
    # sqrt-compression keeps relative ordering but flattens the tail.
    # 0.5 floor matches the minimum a passing pullback can express.
    val = 0.5 + (raw ** 0.5) / 2.0
    return min(2.5, val)


def _bar_atr(session: pd.DataFrame, period: int) -> pd.Series:
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


class PullbackStrategy:
    """Pullback-to-EMA midday entries. See module docstring."""

    def __init__(self, config: PullbackConfig):
        self.config = config
        self._logger = logger
        # Symbol -> prior-session 1-min bars, injected by the runner for
        # indicator warmup. Empty when no setter call has been made.
        self._prior_session_bars: dict[str, pd.DataFrame] = {}

    def set_prior_session_bars(
        self, by_symbol: Mapping[str, pd.DataFrame]
    ) -> None:
        """Inject prior-session bars per symbol for indicator warmup; no-op when disabled."""
        if not self.config.warmup_from_prior_session:
            self._prior_session_bars = {}
            return
        self._prior_session_bars = dict(by_symbol)

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return empty_signals_frame()
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
            raise ValueError(
                "bars must have a tz-aware DatetimeIndex (UTC expected)"
            )

        symbol = infer_symbol(bars)
        cfg = self.config

        et_index = bars.index.tz_convert(ET)
        et_dates = pd.Series(et_index.date, index=bars.index)
        session_dates = sorted(et_dates.unique())

        # Prior-session bars are indicator-only context; never entries.
        prior = self._prior_session_bars.get(symbol) if cfg.warmup_from_prior_session else None

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

            # Prepend prior-session bars (older first) for EMA / ATR warmup.
            # The date gate inside _scan_session ensures prior bars cannot
            # produce entry rows.
            warmup_bars = self._prepend_prior(session, prior, session_date)

            rows.extend(
                self._scan_session(warmup_bars, symbol, entry_date=session_date)
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

    # --------------------------------------------------------------- scanning
    def _scan_session(
        self, session: pd.DataFrame, symbol: str, *, entry_date=None
    ) -> list[dict]:
        cfg = self.config

        closes = session["close"].astype("float64")
        ema_fast = _ema(closes, cfg.ema_fast_period).to_numpy()
        ema_slow = _ema(closes, cfg.ema_slow_period).to_numpy()
        atr_arr = _bar_atr(session, cfg.atr_period_bars).to_numpy()
        closes_arr = closes.to_numpy()

        idx = session.index
        et_idx = idx.tz_convert(ET)
        et_times = et_idx.time
        et_bar_dates = et_idx.date

        earliest_entry = cfg.earliest_entry_time
        latest_entry = cfg.latest_entry_time
        flat_time = cfg.flat_time

        rows: list[dict] = []
        last_entry_idx: int | None = None
        cooldown_bars = cfg.cooldown_minutes  # 1-min bars by contract

        n = len(session)
        # Need at least enough warm-up for the slow EMA + slope lookback +
        # pullback lookback. Anything earlier is operating on under-baked
        # indicators.
        warmup = max(
            cfg.ema_slow_period + cfg.trend_slope_lookback,
            cfg.pullback_lookback_bars + cfg.reclaim_confirm_bars,
        )

        for i in range(n):
            # Date gate: prior-session bars are warmup-only, never entries.
            if entry_date is not None and et_bar_dates[i] != entry_date:
                continue
            bar_time = et_times[i]
            if bar_time < earliest_entry or bar_time >= latest_entry:
                continue
            if i < warmup:
                continue
            a = atr_arr[i]
            if not np.isfinite(a) or a <= 0:
                continue
            if last_entry_idx is not None and (i - last_entry_idx) < cooldown_bars:
                continue

            ef = ema_fast[i]
            es = ema_slow[i]
            ef_back = ema_fast[i - cfg.trend_slope_lookback]
            close = closes_arr[i]

            # Long: uptrend regime + downside pullback + upside reclaim.
            trend_strength = (ef - es) / a
            up_regime = (
                ef > es
                and ef > ef_back
                and trend_strength >= cfg.min_trend_strength
            )
            if up_regime:
                pullback_ok, depth = self._pullback_check(
                    closes_arr, ema_fast, atr_arr, i, side="long",
                )
                reclaim_ok = self._reclaim_check(
                    closes_arr, ema_fast, i, side="long",
                )
                if pullback_ok and reclaim_ok:
                    stop = close - cfg.atr_stop_multiplier * a
                    take = close + cfg.take_r_multiple * (close - stop)
                    score = _normalize_score(trend_strength, depth)
                    rows.append(
                        self._row(
                            idx[i], symbol, "long",
                            stop=stop, take=take, score=score,
                        )
                    )
                    last_entry_idx = i
                    self._logger.debug(
                        "pullback long",
                        extra={
                            "symbol": symbol, "ts": str(idx[i]),
                            "close": float(close), "ema_fast": float(ef),
                            "ema_slow": float(es), "atr": float(a),
                            "depth": float(depth), "score": float(score),
                        },
                    )
                    continue

            # Short: mirrored.
            if cfg.allow_short:
                down_regime = (
                    ef < es
                    and ef < ef_back
                    and (-trend_strength) >= cfg.min_trend_strength
                )
                if down_regime:
                    pullback_ok, depth = self._pullback_check(
                        closes_arr, ema_fast, atr_arr, i, side="short",
                    )
                    reclaim_ok = self._reclaim_check(
                        closes_arr, ema_fast, i, side="short",
                    )
                    if pullback_ok and reclaim_ok:
                        stop = close + cfg.atr_stop_multiplier * a
                        take = close - cfg.take_r_multiple * (stop - close)
                        score = _normalize_score(-trend_strength, depth)
                        rows.append(
                            self._row(
                                idx[i], symbol, "short",
                                stop=stop, take=take, score=score,
                            )
                        )
                        last_entry_idx = i
                        self._logger.debug(
                            "pullback short",
                            extra={
                                "symbol": symbol, "ts": str(idx[i]),
                                "close": float(close), "ema_fast": float(ef),
                                "ema_slow": float(es), "atr": float(a),
                                "depth": float(depth), "score": float(score),
                            },
                        )

        # Per-session flat at first bar at/after flat_by_et so any open
        # entry is closed before the close. Restrict to entry_date so a
        # prepended prior session does not fire the flat twice.
        if entry_date is not None:
            flat_mask = np.array(
                [t >= flat_time and d == entry_date
                 for t, d in zip(et_times, et_bar_dates)]
            )
        else:
            flat_mask = np.array([t >= flat_time for t in et_times])
        if flat_mask.any():
            first_flat = int(np.argmax(flat_mask))
            rows.append(
                self._row(
                    idx[first_flat], symbol, "flat",
                    stop=float("nan"), take=float("nan"),
                    score=float("nan"),
                )
            )
        return rows

    # ------------------------------------------------------------ pullback
    def _pullback_check(
        self,
        closes: np.ndarray,
        ema_fast: np.ndarray,
        atr_arr: np.ndarray,
        i: int,
        *,
        side: str,
    ) -> tuple[bool, float]:
        """Did the prior window contain a real excursion through EMA_fast?

        Returns (ok, depth) where ``depth`` is the maximum normalised
        excursion (>=0) over the lookback window. ``ok`` is True only when
        the count of wrong-side bars meets ``min_pullback_bars`` AND the
        depth exceeds ``pullback_min_atr``.
        """
        cfg = self.config
        # Lookback excludes the current bar (the reclaim bar).
        lo = max(0, i - cfg.pullback_lookback_bars)
        hi = i  # exclusive
        if hi <= lo:
            return False, 0.0
        window_closes = closes[lo:hi]
        window_ema = ema_fast[lo:hi]
        window_atr = atr_arr[lo:hi]

        if side == "long":
            wrong_side = window_closes < window_ema
            diffs = (window_ema - window_closes)  # positive where below
        else:
            wrong_side = window_closes > window_ema
            diffs = (window_closes - window_ema)

        n_wrong = int(wrong_side.sum())
        if n_wrong < cfg.min_pullback_bars:
            return False, 0.0

        # Volatility-normalised max excursion.
        with np.errstate(divide="ignore", invalid="ignore"):
            norm = np.where(window_atr > 0, diffs / window_atr, 0.0)
        # Only consider bars on the wrong side for "depth"; reclaim bars
        # inside the lookback contribute 0.
        norm = np.where(wrong_side, norm, 0.0)
        depth = float(norm.max()) if norm.size else 0.0
        if depth < cfg.pullback_min_atr:
            return False, 0.0
        return True, depth

    # ------------------------------------------------------------ reclaim
    def _reclaim_check(
        self,
        closes: np.ndarray,
        ema_fast: np.ndarray,
        i: int,
        *,
        side: str,
    ) -> bool:
        """The most-recent ``reclaim_confirm_bars`` bars closed back on the
        trend side."""
        k = self.config.reclaim_confirm_bars
        lo = i - k + 1
        if lo < 0:
            return False
        window_closes = closes[lo : i + 1]
        window_ema = ema_fast[lo : i + 1]
        if side == "long":
            return bool(np.all(window_closes > window_ema))
        return bool(np.all(window_closes < window_ema))

    # ------------------------------------------------------------ helpers
    def _prepend_prior(
        self,
        session: pd.DataFrame,
        prior: pd.DataFrame | None,
        session_date,
    ) -> pd.DataFrame:
        """Concat prior-session bars before today's session bars for warmup; returns session unchanged when no prior is available."""
        if prior is None or prior.empty:
            return session
        if not isinstance(prior.index, pd.DatetimeIndex) or prior.index.tz is None:
            return session
        # Only keep prior bars that are strictly earlier than the entry
        # session's ET date -- prevents accidental same-date overlap.
        prior_et_dates = prior.index.tz_convert(ET).date
        keep = prior_et_dates < session_date
        prior_filtered = prior[keep]
        if prior_filtered.empty:
            return session
        # Align columns with the session frame; missing columns become NaN.
        aligned = prior_filtered.reindex(columns=session.columns)
        combined = pd.concat([aligned, session], axis=0)
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()
        return combined

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
            "strategy": "pullback",
            "target_size_pct": float(self.config.target_size_pct),
            "stop_price": stop,
            "take_price": take,
            "score": score,
            # Mirror into ``or_atr_ratio`` until the legacy picker migrates.
            "or_atr_ratio": score,
        }
