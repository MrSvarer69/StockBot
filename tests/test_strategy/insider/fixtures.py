"""Synthetic Form-4 fixtures matching the data-engineer's schema contract."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd


def _default_row() -> dict[str, Any]:
    return {
        "accession_no": "0000000000-00-000000",
        "filing_date": date(2026, 3, 2),
        "transaction_date": date(2026, 3, 1),
        "ticker": "ACME",
        "issuer_cik": "0000000001",
        "issuer_name": "Acme Corp",
        "insider_cik": "0000000100",
        "insider_name": "A Person",
        "insider_title": "Chief Executive Officer",
        "is_officer": True,
        "is_director": False,
        "is_ten_percent_owner": False,
        "transaction_code": "P",
        "shares": Decimal("1000"),
        "price_per_share": Decimal("50"),
        "value_usd": Decimal("50000"),
        "is_10b5_1": False,
        "shares_owned_after": Decimal("10000"),
    }


def make_filing(**overrides: Any) -> dict[str, Any]:
    row = _default_row()
    row.update(overrides)
    if "value_usd" not in overrides and ("shares" in overrides or "price_per_share" in overrides):
        row["value_usd"] = Decimal(str(row["shares"])) * Decimal(str(row["price_per_share"]))
    return row


def filings_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list(_default_row().keys()))
    return pd.DataFrame(rows)


def make_cluster(
    *,
    ticker: str = "ACME",
    base_date: date = date(2026, 3, 1),
    insider_ciks: list[str] | None = None,
    spacing_days: int = 1,
    filing_lag_days: int = 1,
    value_each: Decimal = Decimal("75000"),
) -> list[dict[str, Any]]:
    if insider_ciks is None:
        insider_ciks = ["0000000100", "0000000101", "0000000102"]
    rows = []
    for i, cik in enumerate(insider_ciks):
        txn = base_date + timedelta(days=i * spacing_days)
        filing = txn + timedelta(days=filing_lag_days)
        shares = Decimal("1500")
        price = (value_each / shares).quantize(Decimal("0.01"))
        rows.append(
            make_filing(
                accession_no=f"acc-{ticker}-{i}",
                ticker=ticker,
                insider_cik=cik,
                insider_name=f"Insider {i}",
                insider_title="Director" if i > 0 else "Chief Executive Officer",
                is_officer=(i == 0),
                is_director=(i > 0),
                transaction_date=txn,
                filing_date=filing,
                shares=shares,
                price_per_share=price,
                value_usd=value_each,
            )
        )
    return rows
