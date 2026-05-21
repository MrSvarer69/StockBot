"""VWAP reclaim / rejection strategy (v2). Pure function over BarsFrames.

Mechanism
---------
Volume-weighted average price (VWAP) is the most-watched institutional
intraday benchmark; it is also the level execution algos anchor to. When
price extends away from VWAP and then *closes back across it*, that is read
as a failed extension -- participants who chased the move are now offside,
and a reversion toward VWAP becomes the path of least resistance.

Two confirmations are layered to avoid trading micro-noise:

  1. Price must have been on the *opposite* side of VWAP for at least
     ``min_extension_bars`` bars (the "extension" precondition).
  2. ``confirm_bars`` consecutive bar closes must then sit on the reclaim
     side (filters single-bar wicks).

Long entry: extension below VWAP, then ``confirm_bars`` consecutive closes
back above VWAP, *with* the daily trend up.

Short entry: extension above VWAP, then ``confirm_bars`` consecutive closes
back below VWAP, *with* the daily trend down. Gated by ``allow_short``.

Trend regime filter
-------------------
Per session, the prior session's regular-session close is compared to the
SMA of the prior ``trend_filter_period_sessions`` closes (using only
sessions strictly before the current one -- never the current session's
bars). ``up`` if prior close > SMA, ``down`` if prior close < SMA.

  - Trend ``up``  : long reclaims allowed, short rejections suppressed.
  - Trend ``down``: short rejections allowed, long reclaims suppressed.
  - Trend undefined (insufficient prior sessions): both suppressed by
    default (safe-fail). Override with ``allow_against_trend: true`` to
    revert to the v1 unfiltered behavior.

Stop / take (asymmetric by construction)
----------------------------------------
Sized off a bar-scale ATR (true-range ATR with period
``atr_period_bars``) -- distinct from ORB's session-scale ATR because this
signal lives at the bar timescale.

  stop = entry_close +/- atr_stop_multiplier * atr_bar
  take = entry_close +/- take_r_multiple * |entry_close - stop|

Default ``atr_stop_multiplier=1.0`` and ``take_r_multiple=1.5`` give a 1.5R
reward / 1R risk ratio. Break-even win rate is 1 / (1 + 1.5) = 40%; the v1
design's ~1.0 ratio required ~50%+ win rate to clear slippage, which the
reclaim/rejection trigger does not deliver.

Both stop and take are emitted on every entry row so the bracket-order
wiring in ``execution/alpaca_paper.py`` can attach stop and take children
together.

Score
-----
The current ``SignalsFrame`` schema still carries ORB's ``or_atr_ratio``
column. For VWAP v2 we emit a composite

    score = extension_depth * (1 + clip(volume_decay, 0, 1))

where

    extension_depth = max over the extension window of |close - vwap| / atr_bar
    volume_decay    = (mean_vol_first_half - mean_vol_second_half)
                      / mean_vol_first_half   (clipped >= 0 in the multiplier)

Thesis: deep extensions trap more late chasers (more fuel for the reclaim);
declining volume during the extension means the extending move ran out of
participation (exhaustion). A reclaim of a deep-and-exhausted extension is
mechanically a higher-quality setup than a reclaim of a shallow-and-still-
energetic extension. Score sits in the renamed-later ``or_atr_ratio`` slot.

Constraints
-----------
- Pure function: no I/O, no broker calls, no network, no global state.
- UTC-aware bar index required; ET only used for session-time filtering.
- Daytrade-only: a ``flat`` signal is emitted at ``flat_by_et`` so any open
  position is closed same session.
- At most one entry per ``cooldown_minutes`` window per symbol per session.
"""

from __future__ import annotations

import logging
from collections import deque
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
class VWAPConfig:
    session_start_et: str
    session_end_et: str
    flat_by_et: str
    latest_entry_et: str | None
    confirm_bars: int
    min_extension_bars: int
    atr_period_bars: int
    atr_stop_multiplier: float
    take_r_multiple: float
    trend_filter_period_sessions: int
    allow_against_trend: bool
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


def load_config(path: Path | None = None) -> VWAPConfig:
    p = path or _DEFAULT_CONFIG_PATH
    with p.open("r") as f:
        raw = yaml.safe_load(f)
    return VWAPConfig(**raw)


def _typical_price(session: pd.DataFrame) -> pd.Series:
    return (session["high"] + session["low"] + session["close"]) / 3.0


def _session_vwap(session: pd.DataFrame) -> pd.Series:
    """Cumulative typical-price * volume / cumulative volume, reset at session open."""
    tp = _typical_price(session)
    vol = session["volume"].astype("float64")
    cum_pv = (tp * vol).cumsum()
    cum_v = vol.cumsum()
    vwap = cum_pv / cum_v.where(cum_v > 0, np.nan)
    return vwap.fillna(tp)


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


def _trend_regime(
    prior_closes: deque[float],
    period: int,
) -> str:
    """Classify the daily trend from the rolling window of prior-session closes.

    ``prior_closes`` is in chronological order (oldest first); ``period`` is
    the SMA window. The most recent value in ``prior_closes`` is yesterday's
    close. Returns ``"up"``, ``"down"`` or ``"unknown"`` if there is not yet
    a full window of prior sessions.
    """
    if len(prior_closes) < period:
        return "unknown"
    window = list(prior_closes)[-period:]
    sma = sum(window) / period
    last = window[-1]
    if last > sma:
        return "up"
    if last < sma:
        return "down"
    return "unknown"


class VWAPStrategy:
    """VWAP reclaim (long) / rejection (short), v2. See module docstring."""

    def __init__(self, config: VWAPConfig):
        self.config = config
        self._logger = logger

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return empty_signals_frame()
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
            raise ValueError("bars must have a tz-aware DatetimeIndex (UTC expected)")

        symbol = self._infer_symbol(bars)
        cfg = self.config

        # Build a per-row ET-date column once.
        et_index = bars.index.tz_convert(ET)
        et_dates = pd.Series(et_index.date, index=bars.index)

        # Iterate sessions in chronological order so the rolling prior-close
        # deque is maintained correctly for the trend filter.
        session_dates = sorted(et_dates.unique())
        prior_closes: deque[float] = deque(
            maxlen=max(cfg.trend_filter_period_sessions, 1)
        )

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

            # Snapshot the regime for this session BEFORE we update the deque
            # with today's close. This guarantees we never use intraday info
            # from today to decide today's entries.
            regime = _trend_regime(prior_closes, cfg.trend_filter_period_sessions)

            if not session.empty:
                vwap = _session_vwap(session)
                atr = _bar_atr(session, cfg.atr_period_bars)
                rows.extend(
                    self._scan_session(session, vwap, atr, symbol, regime)
                )
                # Record today's regular-session close for tomorrow's trend
                # filter. Using regular-session bars only (no pre/post).
                prior_closes.append(float(session["close"].iloc[-1]))
            else:
                # Session day with no in-session bars: still try to record a
                # close from whatever bars we have, to keep the trend deque
                # populated. Falls back silently if none.
                if not session_full.empty:
                    prior_closes.append(float(session_full["close"].iloc[-1]))

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
        vwap: pd.Series,
        atr: pd.Series,
        symbol: str,
        regime: str,
    ) -> list[dict]:
        cfg = self.config
        rows: list[dict] = []

        closes = session["close"].to_numpy(dtype="float64")
        volumes = session["volume"].to_numpy(dtype="float64")
        vwap_arr = vwap.to_numpy(dtype="float64")
        atr_arr = atr.to_numpy(dtype="float64")
        idx = session.index
        et_times = idx.tz_convert(ET).time

        latest_entry = cfg.latest_entry_time
        flat_time = cfg.flat_time

        # Trend-gating per direction. ``allow_against_trend`` disables the
        # gate entirely (legacy v1 behavior). Otherwise:
        #   - regime "up"      : longs allowed,  shorts blocked
        #   - regime "down"    : longs blocked,  shorts allowed
        #   - regime "unknown" : both blocked (safe-fail; not enough prior data)
        if cfg.allow_against_trend:
            allow_long, allow_short_dir = True, True
        else:
            allow_long = (regime == "up")
            allow_short_dir = (regime == "down")

        above_run = 0
        below_run = 0
        last_entry_minute_idx: int | None = None
        cooldown_bars = cfg.cooldown_minutes  # 1-min bars by contract

        n = len(session)
        for i in range(n):
            close = closes[i]
            v = vwap_arr[i]
            a = atr_arr[i]

            if close > v:
                above_run += 1
                below_run = 0
            elif close < v:
                below_run += 1
                above_run = 0
            else:
                above_run = 0
                below_run = 0

            bar_time = et_times[i]
            entry_allowed_by_time = (
                latest_entry is None or bar_time < latest_entry
            )
            in_cooldown = (
                last_entry_minute_idx is not None
                and (i - last_entry_minute_idx) < cooldown_bars
            )

            if not entry_allowed_by_time or in_cooldown:
                continue
            if not np.isfinite(a) or a <= 0:
                continue

            # ---------------- Long reclaim ----------------
            if allow_long and above_run >= cfg.confirm_bars:
                ext_end_exclusive = i - cfg.confirm_bars + 1
                ext_len = self._prior_run_length(
                    closes, vwap_arr,
                    end_exclusive=ext_end_exclusive, side="below",
                )
                if ext_len >= cfg.min_extension_bars:
                    ext_start = ext_end_exclusive - ext_len
                    score = self._score(
                        closes, volumes, vwap_arr, atr_arr,
                        ext_start=ext_start, ext_end_exclusive=ext_end_exclusive,
                    )
                    stop = close - cfg.atr_stop_multiplier * a
                    take = close + cfg.take_r_multiple * (close - stop)
                    rows.append(
                        self._row(
                            idx[i], symbol, "long",
                            stop=stop, take=take, score=score,
                        )
                    )
                    last_entry_minute_idx = i
                    self._logger.debug(
                        "VWAP long reclaim",
                        extra={
                            "symbol": symbol, "ts": str(idx[i]),
                            "close": close, "vwap": v, "atr": a,
                            "extension_bars": ext_len, "score": score,
                            "regime": regime,
                        },
                    )
                    continue

            # ---------------- Short rejection ----------------
            if (
                cfg.allow_short
                and allow_short_dir
                and below_run >= cfg.confirm_bars
            ):
                ext_end_exclusive = i - cfg.confirm_bars + 1
                ext_len = self._prior_run_length(
                    closes, vwap_arr,
                    end_exclusive=ext_end_exclusive, side="above",
                )
                if ext_len >= cfg.min_extension_bars:
                    ext_start = ext_end_exclusive - ext_len
                    score = self._score(
                        closes, volumes, vwap_arr, atr_arr,
                        ext_start=ext_start, ext_end_exclusive=ext_end_exclusive,
                    )
                    stop = close + cfg.atr_stop_multiplier * a
                    take = close - cfg.take_r_multiple * (stop - close)
                    rows.append(
                        self._row(
                            idx[i], symbol, "short",
                            stop=stop, take=take, score=score,
                        )
                    )
                    last_entry_minute_idx = i
                    self._logger.debug(
                        "VWAP short rejection",
                        extra={
                            "symbol": symbol, "ts": str(idx[i]),
                            "close": close, "vwap": v, "atr": a,
                            "extension_bars": ext_len, "score": score,
                            "regime": regime,
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

    # ------------------------------------------------------------------ score
    @staticmethod
    def _score(
        closes: np.ndarray,
        volumes: np.ndarray,
        vwap_arr: np.ndarray,
        atr_arr: np.ndarray,
        *,
        ext_start: int,
        ext_end_exclusive: int,
    ) -> float:
        """Composite reclaim-quality score.

            score = extension_depth * (1 + clip(volume_decay, 0, 1))

            extension_depth = max over [ext_start, ext_end_exclusive)
                              of |close - vwap| / atr_bar
            volume_decay    = (mean_vol_first_half - mean_vol_second_half)
                              / mean_vol_first_half

        The depth term is volatility-normalized (divided by per-bar ATR) so
        scores are comparable across symbols. The decay term in [0, 1]
        rewards exhaustion patterns (declining volume on the extension);
        the (1 + ...) shape ensures the score is never zero on a finite-
        depth extension and at most 2x the raw depth.
        """
        if ext_end_exclusive <= ext_start:
            return float("nan")

        # Volatility-normalized depths along the extension.
        depths = []
        for j in range(ext_start, ext_end_exclusive):
            a = atr_arr[j]
            if not np.isfinite(a) or a <= 0:
                continue
            depths.append(abs(closes[j] - vwap_arr[j]) / a)
        if not depths:
            return float("nan")
        extension_depth = max(depths)

        # Volume decay across the extension window.
        ext_vols = volumes[ext_start:ext_end_exclusive]
        if ext_vols.size >= 2:
            mid = ext_vols.size // 2
            first_half = ext_vols[:mid] if mid > 0 else ext_vols[:1]
            second_half = ext_vols[mid:]
            mv1 = float(first_half.mean()) if first_half.size else 0.0
            mv2 = float(second_half.mean()) if second_half.size else 0.0
            if mv1 > 0:
                decay = (mv1 - mv2) / mv1
            else:
                decay = 0.0
        else:
            decay = 0.0

        # Multiplier in [1, 2]: clip decay to non-negative and cap at 1.
        mult = 1.0 + float(np.clip(decay, 0.0, 1.0))
        return float(extension_depth * mult)

    # ------------------------------------------------------------------ misc
    @staticmethod
    def _prior_run_length(
        closes: np.ndarray,
        vwap_arr: np.ndarray,
        *,
        end_exclusive: int,
        side: str,
    ) -> int:
        """Count consecutive bars strictly above/below VWAP immediately
        before ``end_exclusive`` (walking backward)."""
        run = 0
        i = end_exclusive - 1
        while i >= 0:
            c = closes[i]
            v = vwap_arr[i]
            if side == "below":
                if c < v:
                    run += 1
                    i -= 1
                else:
                    break
            else:  # "above"
                if c > v:
                    run += 1
                    i -= 1
                else:
                    break
        return run

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
            # Unified cross-strategy conviction column. For VWAP v2 it
            # carries extension_depth * (1 + clip(volume_decay, 0, 1)).
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
