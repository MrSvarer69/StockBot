"""Cluster-buy filter: multiple distinct insiders buying the same ticker in a short window.

Mechanism: independent decisions by several insiders within a tight window are
harder to explain by personal liquidity events than a lone buy. The cluster
itself is the signal — strength scales with the number of distinct insiders
and (mildly) with aggregate dollar value.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pandas as pd

from .gates import apply_universal_gates, empty_insider_signals_frame

logger = logging.getLogger(__name__)

STRATEGY_NAME = "insider.cluster_buy"


def cluster_buy_signals(
    filings: pd.DataFrame,
    *,
    min_insiders: int = 3,
    window_days: int = 10,
) -> pd.DataFrame:
    if filings.empty:
        return empty_insider_signals_frame()

    df = apply_universal_gates(filings)
    if df.empty:
        return empty_insider_signals_frame()

    df = df.copy()
    df["transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.normalize()

    rows: list[dict] = []
    for ticker, group in df.groupby("ticker", sort=True):
        rows.extend(_cluster_rows_for_ticker(str(ticker), group, min_insiders, window_days))

    if not rows:
        return empty_insider_signals_frame()

    out = pd.DataFrame(rows, columns=["ticker", "signal_date", "strategy", "strength", "metadata"])
    out["ticker"] = out["ticker"].astype("string")
    out["strategy"] = out["strategy"].astype("string")
    out["strength"] = out["strength"].astype("float64")
    return out


def _cluster_rows_for_ticker(
    ticker: str,
    group: pd.DataFrame,
    min_insiders: int,
    window_days: int,
) -> list[dict]:
    group = group.sort_values("transaction_date").reset_index(drop=True)
    if len(group) < min_insiders:
        return []

    txn_dates = group["transaction_date"].to_list()
    insiders = group["insider_cik"].to_list()
    values = group["value_usd"].to_list()
    filing_dates = pd.to_datetime(group["filing_date"]).dt.normalize().to_list()

    out: list[dict] = []
    # Why: window is anchored on each transaction as the *end* of a look-back of
    # window_days. Using transaction-end as the dedup key gives a stable signal
    # date even if more buys arrive later within the same cluster.
    last_emitted_end: pd.Timestamp | None = None
    last_emitted_members: frozenset[str] | None = None

    for end_idx in range(len(group)):
        end_date = txn_dates[end_idx]
        start_date = end_date - pd.Timedelta(days=window_days - 1)

        window_members: dict[str, Decimal] = {}
        window_filing_dates: list[pd.Timestamp] = []
        for i in range(end_idx + 1):
            if txn_dates[i] < start_date:
                continue
            cik = insiders[i]
            val = Decimal(str(values[i]))
            window_members[cik] = window_members.get(cik, Decimal("0")) + val
            window_filing_dates.append(filing_dates[i])

        if len(window_members) < min_insiders:
            continue

        members_set = frozenset(window_members.keys())
        # Why: dedup rule — if the membership set is unchanged from the most
        # recent emission, suppress the new row. A cluster that gains a member
        # still re-emits because it is materially a stronger signal.
        if last_emitted_members == members_set:
            continue

        total_value = sum(window_members.values(), Decimal("0"))
        strength = _cluster_strength(len(window_members), total_value, min_insiders)
        # Why: ``max_filing_date`` is the latest date on which any filing in
        # the cluster became publicly visible on EDGAR. The InsiderStrategy
        # adapter uses this (not signal_date / transaction_date) to derive
        # the earliest tradable entry date, guaranteeing no lookahead from a
        # signal whose newest filing was still embargoed.
        max_filing_date = max(window_filing_dates) if window_filing_dates else end_date
        out.append(
            {
                "ticker": ticker,
                "signal_date": end_date.date(),
                "strategy": STRATEGY_NAME,
                "strength": strength,
                "metadata": {
                    "insider_ciks": sorted(window_members.keys()),
                    "distinct_insider_count": len(window_members),
                    "total_value_usd": total_value,
                    "window_start": start_date.date(),
                    "window_end": end_date.date(),
                    "max_filing_date": max_filing_date.date(),
                },
            }
        )
        last_emitted_end = end_date
        last_emitted_members = members_set

    if last_emitted_end is not None:
        logger.debug(
            "cluster_buy emitted",
            extra={"ticker": ticker, "rows": len(out)},
        )
    return out


def _cluster_strength(
    distinct_insider_count: int,
    total_value_usd: Decimal,
    min_insiders: int,
) -> float:
    # Why: two bounded components combined multiplicatively so neither dominates.
    # Count component saturates at min_insiders + 4 distinct insiders (logistic-ish
    # via min()), value component saturates at $5M aggregate.
    count_excess = max(0, distinct_insider_count - min_insiders)
    count_component = min(1.0, 0.5 + 0.125 * count_excess)

    value_cap = Decimal("5000000")
    capped_value = total_value_usd if total_value_usd < value_cap else value_cap
    value_component = float(capped_value / value_cap)

    return float(count_component * (0.5 + 0.5 * value_component))
