"""Cross-symbol candidate picker.

The ORB strategy emits one entry signal per symbol per session when its
opening range breaks. With a wide universe, an iteration may surface more
candidate entries than the risk-cap permits in flight (`max_position_count`
in RiskParams). This module ranks the candidates and selects the top-N so
the bot's "decision" about which symbols to trade is principled rather than
first-come-first-served by clock order.

Stateless: each iteration ranks fresh independently. Already-open positions
are NOT preempted by a higher-ratio candidate arriving later — capital
already deployed stays deployed. This avoids wash-trade rejections and
round-trip costs at the cost of being suboptimal when a much stronger
setup arrives 1-2 iterations late.

Pure functions only — no I/O, no broker calls. Strategy-package contract.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)


def rank_entry_signals(
    entry_signals_by_symbol: Mapping[str, dict],
    *,
    max_picks: int,
    min_ratio: float | None = None,
) -> dict[str, dict]:
    """Pick the top `max_picks` entry signals by OR/ATR ratio.

    Args:
        entry_signals_by_symbol: one entry signal per symbol (a dict shaped
            like a SignalsFrame row, must include "or_atr_ratio" and "side").
            "flat" signals should not be passed here — they are not subject
            to ranking and the caller is responsible for handling them.
        max_picks: maximum number of entries to keep. If non-positive, an
            empty dict is returned (no entries fire this iteration).
        min_ratio: optional pool-entry floor. Candidates below this OR/ATR
            ratio are excluded before ranking — guards against weak days
            where all candidates marginally pass the strategy's filter but
            none are convincingly above noise. None disables the floor.

    Returns:
        A subset of `entry_signals_by_symbol` containing only the chosen
        symbols. Ranking is descending by `or_atr_ratio`; ties are broken
        alphabetically by symbol for determinism. Signals missing or with
        NaN `or_atr_ratio` are sorted last (treated as 0 conviction) AND
        logged at WARNING — a missing ratio in entry_candidates is a
        contract violation upstream.
    """
    if max_picks <= 0 or not entry_signals_by_symbol:
        return {}

    eligible: dict[str, dict] = {}
    skipped_below_floor: list[tuple[str, float]] = []
    for sym, sig in entry_signals_by_symbol.items():
        ratio = sig.get("or_atr_ratio")
        if ratio is None or _is_nan(ratio):
            logger.warning(
                "picker received entry signal with missing/NaN or_atr_ratio; "
                "ranking as 0 conviction",
                extra={"symbol": sym, "side": sig.get("side")},
            )
            ratio = 0.0
        if min_ratio is not None and float(ratio) < float(min_ratio):
            skipped_below_floor.append((sym, float(ratio)))
            continue
        eligible[sym] = sig

    if skipped_below_floor:
        logger.info(
            "picker dropped candidates below min_ratio",
            extra={
                "min_ratio": float(min_ratio),
                "dropped": {sym: ratio for sym, ratio in skipped_below_floor},
            },
        )

    if not eligible:
        return {}

    def _score(item: tuple[str, dict]) -> tuple[float, str]:
        sym, sig = item
        ratio = sig.get("or_atr_ratio")
        if ratio is None or _is_nan(ratio):
            ratio = 0.0
        return (-float(ratio), sym)

    ordered = sorted(eligible.items(), key=_score)
    chosen = ordered[:max_picks]
    return {sym: sig for sym, sig in chosen}


def _is_nan(x) -> bool:
    """Cheap NaN check without requiring pandas/numpy in this module."""
    try:
        return x != x  # only NaN is not equal to itself
    except Exception:
        return False
