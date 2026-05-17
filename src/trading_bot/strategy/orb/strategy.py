"""Opening-range breakout strategy. Pure function over BarsFrames."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from ..base import empty_signals_frame

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class ORBConfig:
    opening_range_minutes: int
    session_start_et: str
    session_end_et: str
    flat_by_et: str
    atr_period_sessions: int
    atr_stop_multiplier: float
    take_r_multiple: float
    target_size_pct: float
    min_range_atr_multiplier: float
    allow_short: bool
    # Latest ET time at which a NEW entry may be opened. Flat signals are not
    # gated. None disables the filter entirely.
    latest_entry_et: str | None = None

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


def load_config(path: Path | None = None) -> ORBConfig:
    p = path or _DEFAULT_CONFIG_PATH
    with p.open("r") as f:
        raw = yaml.safe_load(f)
    return ORBConfig(**raw)


def _session_range(session: pd.DataFrame) -> float:
    """High-low range of a single regular session."""
    if session.empty:
        return 0.0
    return float(session["high"].max() - session["low"].min())


def _session_atr(prior_ranges: "deque[float]") -> float:
    """Mean of the buffered prior-session high-low ranges (the 'daily ATR' proxy).

    Returns 0.0 when the buffer is empty; callers handle cold-start fallback.
    """
    if not prior_ranges:
        return 0.0
    return float(sum(prior_ranges) / len(prior_ranges))


class ORBStrategy:
    """Close-based opening-range breakout.

    Mechanism: the first `opening_range_minutes` of the regular session capture
    pre-market positioning and overnight news digestion. A bar that *closes*
    outside that range is treated as confirmation that participants have agreed
    on a new short-term level — a momentum continuation signal. Close-based (not
    high/low touch) on purpose: we want commitment, not a wick.

    Constraints, per session:
      - at most one long entry (first close > OR_high)
      - at most one short entry (first close < OR_low, only if allow_short)
      - ATR stop = entry_close ± atr_stop_multiplier * ATR
      - take = entry_close ± take_r_multiple * |entry - stop|
      - time-of-day flat at flat_by_et

    ATR source: rolling mean of the last `atr_period_sessions` prior-session
    high-low ranges — a session-level (a.k.a. "daily ATR") volatility proxy
    computed from intraday bars. The `min_range_atr_multiplier` filter and the
    `atr_stop_multiplier` stop are both denominated in this session-scale unit.

    For the first session in a sequence the buffer is empty; we fall back to
    the OR window's own range as the ATR estimate. This is a known cold-start
    compromise — the filter is effectively disarmed on day 1 and stop sizing
    is tighter than steady-state until the buffer fills.
    """

    def __init__(self, config: ORBConfig):
        self.config = config
        self._logger = logger

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return empty_signals_frame()
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
            raise ValueError("bars must have a tz-aware DatetimeIndex (UTC expected)")

        symbol = self._infer_symbol(bars)
        et_index = bars.index.tz_convert(ET)
        session_dates = pd.Series(et_index.date, index=bars.index).unique()

        rows: list[dict] = []
        cfg = self.config
        prior_session_ranges: deque[float] = deque(maxlen=cfg.atr_period_sessions)

        for session_date in session_dates:
            mask = pd.Series(et_index.date, index=bars.index).values == session_date
            session = bars[mask]
            if session.empty:
                continue

            session_et = session.index.tz_convert(ET)
            in_session_mask = (
                (session_et.time >= cfg.session_start)
                & (session_et.time < cfg.session_end)
            )
            session = session[in_session_mask]
            if session.empty:
                continue

            session_et = session.index.tz_convert(ET)
            or_end_mask = session_et.time < self._or_end_time()
            or_window = session[or_end_mask]
            after_or = session[~or_end_mask]

            if or_window.empty or after_or.empty:
                # Still feed the buffer so a partial-data day doesn't permanently
                # disarm the filter on the next session.
                self._logger.debug(
                    "ORB partial session",
                    extra={
                        "symbol": symbol,
                        "session_date": str(session_date),
                        "or_window_empty": bool(or_window.empty),
                        "after_or_empty": bool(after_or.empty),
                        "buffer_n": len(prior_session_ranges),
                    },
                )
                prior_session_ranges.append(_session_range(session))
                continue

            or_high = float(or_window["high"].max())
            or_low = float(or_window["low"].min())
            or_range = or_high - or_low

            # Cold-start semantically equals "no prior sessions buffered". Tying
            # this to the buffer length (not _session_atr's return value) keeps
            # the audit honest if _session_atr is later changed.
            cold_start = len(prior_session_ranges) == 0
            atr_raw = _session_atr(prior_session_ranges)
            if cold_start:
                # Use the OR-window range itself. This effectively disarms the
                # min-range filter on day 1 (or_range == atr → ratio 1.0).
                atr = max(or_range, 1e-9)
            else:
                atr = atr_raw
            ratio = or_range / atr if atr > 0 else float("inf")
            self._logger.debug(
                "ORB filter check",
                extra={
                    "symbol": symbol,
                    "session_date": str(session_date),
                    "or_high": or_high,
                    "or_low": or_low,
                    "or_range": or_range,
                    "atr": atr,
                    "ratio": ratio,
                    "threshold": cfg.min_range_atr_multiplier,
                    "buffer_n": len(prior_session_ranges),
                    "cold_start": cold_start,
                },
            )

            if or_range < cfg.min_range_atr_multiplier * atr:
                self._logger.debug(
                    "ORB filter rejected session",
                    extra={
                        "symbol": symbol,
                        "session_date": str(session_date),
                        "or_range": or_range,
                        "min_required": cfg.min_range_atr_multiplier * atr,
                        "ratio": ratio,
                        "cold_start": cold_start,
                    },
                )
                prior_session_ranges.append(_session_range(session))
                rows.extend(self._maybe_flat(after_or, symbol))
                continue

            long_done = False
            short_done = False
            latest_entry = cfg.latest_entry_time
            for ts, row in after_or.iterrows():
                # Late-day entries (ORB historically does poorly on late breakouts)
                # are suppressed; the existing flat signal still fires.
                if latest_entry is not None:
                    bar_et = ts.tz_convert(ET).time() if hasattr(ts, "tz_convert") else ts
                    if isinstance(bar_et, dtime) and bar_et >= latest_entry:
                        self._logger.debug(
                            "skip late entry candidate %s: bar %s >= cutoff %s",
                            symbol,
                            bar_et,
                            latest_entry,
                        )
                        break
                close = float(row["close"])
                if not long_done and close > or_high:
                    stop = close - cfg.atr_stop_multiplier * atr
                    take = close + cfg.take_r_multiple * (close - stop)
                    rows.append(
                        self._row(
                            ts, symbol, "long", stop=stop, take=take,
                            or_atr_ratio=ratio,
                        )
                    )
                    long_done = True
                elif cfg.allow_short and not short_done and close < or_low:
                    stop = close + cfg.atr_stop_multiplier * atr
                    take = close - cfg.take_r_multiple * (stop - close)
                    rows.append(
                        self._row(
                            ts, symbol, "short", stop=stop, take=take,
                            or_atr_ratio=ratio,
                        )
                    )
                    short_done = True

            rows.extend(self._maybe_flat(after_or, symbol))
            prior_session_ranges.append(_session_range(session))

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
                "or_atr_ratio",
            ]
        ]

    def _or_end_time(self) -> dtime:
        start = self.config.session_start
        total_minutes = start.hour * 60 + start.minute + self.config.opening_range_minutes
        return dtime(total_minutes // 60, total_minutes % 60)

    def _maybe_flat(self, after_or: pd.DataFrame, symbol: str) -> list[dict]:
        et_index = after_or.index.tz_convert(ET)
        flat_mask = et_index.time >= self.config.flat_time
        if not flat_mask.any():
            return []
        first_idx = after_or.index[flat_mask][0]
        return [
            self._row(
                first_idx,
                symbol,
                "flat",
                stop=float("nan"),
                take=float("nan"),
                or_atr_ratio=float("nan"),
            )
        ]

    def _row(
        self,
        ts,
        symbol: str,
        side: str,
        *,
        stop: float,
        take: float,
        or_atr_ratio: float,
    ) -> dict:
        return {
            "timestamp": ts,
            "symbol": symbol,
            "side": side,
            "target_size_pct": float(self.config.target_size_pct),
            "stop_price": stop,
            "take_price": take,
            # Quality score: OR range / session-scale ATR. Higher = larger
            "or_atr_ratio": or_atr_ratio,
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
