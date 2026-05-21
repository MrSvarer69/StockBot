"""C-suite conviction filter: large CEO/CFO open-market buy with a cooldown.

Mechanism: CEOs and CFOs are the insiders with the broadest informational
advantage and the strongest career-risk disincentive to buy publicly. A
sizable open-market buy by one of them — outside any prior recent cluster of
their own buying — is a high-conviction signal. The cooldown filters out
repeat buys by the same officer within a window where the prior signal is
still in force.
"""

from __future__ import annotations

import logging
import math
import re
from decimal import Decimal

import pandas as pd

from .gates import apply_universal_gates, empty_insider_signals_frame

logger = logging.getLogger(__name__)

STRATEGY_NAME = "insider.csuite_conviction"

# Why: regex anchors handle "CEO" as a standalone token while also accepting
# the spelled-out title. The (?:\b|^) / (?:\b|$) guards avoid matching "VCEO"
# or similar substrings, and "Chief Executive" without "Officer" still hits
# the common shorthand in SEC filings.
_CSUITE_TITLE_PATTERN = re.compile(
    r"(?:\bceo\b|\bcfo\b|chief\s+executive(?:\s+officer)?|chief\s+financial(?:\s+officer)?)",
    re.IGNORECASE,
)


def csuite_conviction_signals(
    filings: pd.DataFrame,
    *,
    min_value_usd: Decimal = Decimal("100000"),
    cooldown_days: int = 180,
) -> pd.DataFrame:
    if filings.empty:
        return empty_insider_signals_frame()

    df = apply_universal_gates(filings)
    if df.empty:
        return empty_insider_signals_frame()

    df = df.copy()
    df["transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.normalize()

    df = df[df["is_officer"].astype(bool)]
    df = df[df["insider_title"].notna()]
    df = df[df["insider_title"].astype(str).map(_is_csuite_title)]

    if df.empty:
        return empty_insider_signals_frame()

    df = df[df["value_usd"].apply(lambda x: Decimal(str(x)) >= min_value_usd)]
    if df.empty:
        return empty_insider_signals_frame()

    df = df.sort_values(["ticker", "insider_cik", "transaction_date"]).reset_index(drop=True)

    rows: list[dict] = []
    last_emit: dict[tuple[str, str], pd.Timestamp] = {}
    for _, row in df.iterrows():
        ticker = str(row["ticker"])
        insider_cik = str(row["insider_cik"])
        txn_date = row["transaction_date"]
        key = (ticker, insider_cik)
        prior = last_emit.get(key)
        if prior is not None:
            elapsed = (txn_date - prior).days
            # Why: strict inequality — `elapsed > cooldown_days` is the only case
            # that resets. A txn exactly cooldown_days after the prior is still
            # inside the cooldown window per the spec ("prior cooldown_days days").
            if elapsed <= cooldown_days:
                logger.debug(
                    "csuite cooldown skip",
                    extra={
                        "ticker": ticker,
                        "insider_cik": insider_cik,
                        "elapsed_days": elapsed,
                    },
                )
                continue

        value_usd = Decimal(str(row["value_usd"]))
        strength = _csuite_strength(value_usd, min_value_usd)
        # Why: ``max_filing_date`` is the date on which this filing became
        # publicly visible on EDGAR. The InsiderStrategy adapter uses it
        # (not signal_date / transaction_date) to derive the earliest
        # tradable entry date, preventing lookahead from a filing whose
        # transaction predated its public availability.
        filing_dt = pd.to_datetime(row["filing_date"]).normalize()
        rows.append(
            {
                "ticker": ticker,
                "signal_date": txn_date.date(),
                "strategy": STRATEGY_NAME,
                "strength": strength,
                "metadata": {
                    "insider_cik": insider_cik,
                    "insider_name": row.get("insider_name"),
                    "insider_title": row.get("insider_title"),
                    "value_usd": value_usd,
                    "max_filing_date": filing_dt.date(),
                },
            }
        )
        last_emit[key] = txn_date

    if not rows:
        return empty_insider_signals_frame()

    out = pd.DataFrame(rows, columns=["ticker", "signal_date", "strategy", "strength", "metadata"])
    out["ticker"] = out["ticker"].astype("string")
    out["strategy"] = out["strategy"].astype("string")
    out["strength"] = out["strength"].astype("float64")
    return out


def _is_csuite_title(raw: str) -> bool:
    return _CSUITE_TITLE_PATTERN.search(raw) is not None


def _csuite_strength(value_usd: Decimal, min_value_usd: Decimal) -> float:
    # Why: log-bucket ramp from the gating threshold to 100x that threshold.
    # The choice of 100x as saturation is a documented convention, not a fit —
    # it gives a $10M buy a higher rank than a $1M buy at min=$100k while
    # bounding strength in [0, 1].
    if value_usd <= min_value_usd:
        return 0.5
    cap = min_value_usd * Decimal("100")
    if value_usd >= cap:
        return 1.0

    ratio = float(value_usd / min_value_usd)
    cap_ratio = float(cap / min_value_usd)
    return 0.5 + 0.5 * (math.log(ratio) / math.log(cap_ratio))
