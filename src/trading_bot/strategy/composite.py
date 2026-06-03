"""Composite strategy: run several inner strategies, merge their signals.

Why this exists
---------------
The session loop expects a single object that satisfies the ``Strategy``
protocol (``generate_signals(bars) -> SignalsFrame``). Running ORB and the
insider-signal adapter in the same session would otherwise require either
two parallel loops or a session-layer change. ``CompositeStrategy`` keeps
the session blind to multi-strategy operation: it concatenates the rows
emitted by each inner strategy and returns one SignalsFrame.

Pure function
-------------
``generate_signals`` does no I/O of its own — inner strategies that need
to load data (e.g. ``InsiderStrategy`` loading parquet) must do that at
construction time, not on each call. This preserves the strategy-layer
contract.

Conflict resolution
-------------------
Two strategies may emit a row for the same ``(timestamp, symbol)`` with
side ``long`` or ``short``. To prevent the session loop from issuing two
opposing entries for the same bar, the composite deduplicates such rows:

  - For each ``(timestamp, symbol)`` key with one or more entry rows
    (``side in {"long", "short"}``), the row with the highest ``score``
    wins. Ties on ``score`` are broken by the ``names`` priority order
    given at construction (the first listed strategy wins).
  - ``flat`` rows are NOT subject to this dedup — every ``flat`` row
    passes through. The session needs to see all of them so any open
    position is closed when ANY strategy says so.

This is deliberately stricter than "merge everything": acting on two
opposing entries for the same symbol at the same bar would be a
wash-trade risk and a sign of a real strategy disagreement that wants
operator review, not automatic execution.

Attribution
-----------
Each inner strategy populates a ``strategy`` column on its emitted rows
("orb", "pullback", "insider"). The composite preserves that column on
both entry and flat rows so downstream attribution can split P&L by
source. The composite emits the exact ``empty_signals_frame()`` shape.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pandas as pd

from .base import Strategy, empty_signals_frame

logger = logging.getLogger(__name__)


class CompositeStrategy:
    """Fan-out wrapper around multiple ``Strategy`` instances.

    Args:
        strategies: ordered list of strategies to call. The order is also
            the tie-break priority when two strategies emit the same
            ``(timestamp, symbol)`` entry with equal ``score``.
        names: optional human-readable names for each strategy. Defaults
            to ``["strategy_0", "strategy_1", ...]``. Used only for
            logging today; the actual priority comes from positional
            order in ``strategies``.
    """

    def __init__(
        self,
        strategies: list[Strategy],
        names: Sequence[str] | None = None,
    ) -> None:
        if not strategies:
            raise ValueError(
                "CompositeStrategy requires at least one inner strategy"
            )
        if names is not None and len(names) != len(strategies):
            raise ValueError(
                "names length must match strategies length: "
                f"got {len(names)} names for {len(strategies)} strategies"
            )
        self._strategies: list[Strategy] = list(strategies)
        self._names: list[str] = (
            list(names)
            if names is not None
            else [f"strategy_{i}" for i in range(len(strategies))]
        )

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Run every inner strategy and merge their signals into one frame.

        Empty inner outputs are skipped. The result columns are exactly
        those of ``empty_signals_frame()``.
        """
        frames: list[pd.DataFrame] = []
        # Tag each row with the inner-strategy index so the entry-conflict
        # resolution can apply name-priority deterministically without an
        # explicit ``strategy_id`` column on the public SignalsFrame.
        for idx, strat in enumerate(self._strategies):
            sig = strat.generate_signals(bars)
            if sig is None or sig.empty:
                continue
            tagged = sig.copy()
            tagged["_strategy_idx"] = idx
            frames.append(tagged)

        if not frames:
            return empty_signals_frame()

        merged = pd.concat(frames, axis=0, ignore_index=True)

        entries = merged[merged["side"].isin(["long", "short"])]
        flats = merged[merged["side"] == "flat"]

        if not entries.empty:
            entries = self._resolve_entry_conflicts(entries)

        out = pd.concat([entries, flats], axis=0, ignore_index=True)
        # Sort by timestamp so the session's per-symbol "latest signal" pick
        # behaves the same as if a single strategy had produced these rows.
        if not out.empty:
            out = out.sort_values("timestamp", kind="mergesort").reset_index(
                drop=True
            )

        # Drop the internal column and project to the canonical schema.
        out = out.drop(columns=["_strategy_idx"], errors="ignore")
        return out[
            [
                "timestamp",
                "symbol",
                "side",
                "strategy",
                "target_size_pct",
                "stop_price",
                "take_price",
                "entry_price",
                "score",
                "or_atr_ratio",
            ]
        ]

    def _resolve_entry_conflicts(self, entries: pd.DataFrame) -> pd.DataFrame:
        """For each ``(timestamp, symbol)`` keep the highest-score entry.

        Ties broken by smallest ``_strategy_idx`` (first-listed strategy
        wins). NaN scores rank last and are logged at WARNING because a
        strategy emitting an entry with no score is a contract violation.
        """
        df = entries.copy()
        nan_mask = df["score"].isna()
        if nan_mask.any():
            for _, row in df[nan_mask].iterrows():
                logger.warning(
                    "composite received entry with NaN score; ranking as 0",
                    extra={
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "strategy_idx": int(row.get("_strategy_idx", -1)),
                    },
                )
        # Replace NaN scores with -inf so they always lose the priority sort.
        df["_score_for_sort"] = df["score"].where(~nan_mask, float("-inf"))

        # Sort: highest score first, then lowest strategy index (first listed
        # strategy wins ties). Then keep the first row per (ts, symbol).
        df = df.sort_values(
            ["timestamp", "symbol", "_score_for_sort", "_strategy_idx"],
            ascending=[True, True, False, True],
            kind="mergesort",
        )
        deduped = df.drop_duplicates(
            subset=["timestamp", "symbol"], keep="first"
        )
        return deduped.drop(columns=["_score_for_sort"])

    @property
    def names(self) -> list[str]:
        """Read-only list of inner-strategy names."""
        return list(self._names)
