"""Universal gates + shared helpers for insider-buy filters.

The gates encode the minimum bar a Form-4 row must clear before any signal
logic considers it. They are intentionally conservative: open-market purchase
code only, no pre-scheduled trades, no penny stocks, no stale filings.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

PENNY_STOCK_THRESHOLD_USD = Decimal("5")
STALE_FILING_BUSINESS_DAYS = 2


INSIDER_SIGNAL_COLUMNS = ["ticker", "signal_date", "strategy", "strength", "metadata"]


def empty_insider_signals_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(dtype="string"),
            "signal_date": pd.Series(dtype="object"),
            "strategy": pd.Series(dtype="string"),
            "strength": pd.Series(dtype="float64"),
            "metadata": pd.Series(dtype="object"),
        }
    )


def apply_universal_gates(filings: pd.DataFrame) -> pd.DataFrame:
    empty = filings.iloc[0:0].copy()
    if filings.empty:
        return empty

    df = filings.copy()

    df = df[df["transaction_code"] == "P"]
    if df.empty:
        return empty
    df = df[~df["is_10b5_1"].astype(bool)]
    if df.empty:
        return empty
    df = df[df["ticker"].notna()]
    if df.empty:
        return empty
    # Parser emits sentinel strings "NONE"/"N/A" when the source filing
    # had no usable ticker (filer is a fund / SPV / pre-IPO entity). These
    # are unusable for trading: drop them here defensively rather than
    # relying on the parser to ever emit a true null.
    ticker_str = df["ticker"].astype("string").str.strip().str.upper()
    df = df[~ticker_str.isin({"NONE", "N/A", "NA", "NULL", ""})]
    if df.empty:
        return empty

    # Why: Decimal comparison – the schema mandates Decimal here, so coerce any
    # incoming objects via Decimal(str(x)) to avoid implicit float promotion.
    # Guarded with an empty check because pandas .apply on an empty object Series
    # returns a columnless DataFrame, which then breaks downstream selection.
    price_ok = df["price_per_share"].apply(lambda x: Decimal(str(x)) >= PENNY_STOCK_THRESHOLD_USD)
    df = df[price_ok.astype(bool)]
    if df.empty:
        return empty

    filing_dt = pd.to_datetime(df["filing_date"])
    txn_dt = pd.to_datetime(df["transaction_date"])
    # Why: business-day delta (Mon-Fri only) is the meaningful staleness measure
    # because EDGAR filings cluster around business-day cutoffs.
    business_day_lag = _business_day_delta(txn_dt, filing_dt)
    df = df[business_day_lag <= STALE_FILING_BUSINESS_DAYS]
    if df.empty:
        return empty

    return df.reset_index(drop=True)


def _business_day_delta(start: pd.Series, end: pd.Series) -> pd.Series:
    if start.empty:
        return pd.Series([], dtype="int64")
    start_arr = start.dt.normalize().values.astype("datetime64[D]")
    end_arr = end.dt.normalize().values.astype("datetime64[D]")
    import numpy as np

    deltas = np.busday_count(start_arr, end_arr)
    return pd.Series(deltas, index=start.index)
