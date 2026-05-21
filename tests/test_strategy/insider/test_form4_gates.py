"""Tests for ``apply_universal_gates`` — specifically the sentinel-ticker filter.

The Form 4 parser was observed to emit sentinel-string tickers like ``"NONE"``
or ``"N/A"`` in ~23% of rows (filer is a fund / SPV / pre-IPO entity with no
usable ticker). These rows are unusable for trading; the gates layer drops
them defensively so a parser regression cannot leak through to the strategy.

This file pins that behavior. All other gate criteria
(transaction_code="P", not 10b5-1, price >= $5, filing within 2 business days)
are satisfied on every fixture row so the test isolates the
ticker-sentinel branch.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from trading_bot.strategy.insider.gates import apply_universal_gates


def _row(ticker: object) -> dict:
    """Build a single Form 4-shaped row that satisfies every non-ticker gate.

    Why these values:
      - transaction_code "P" passes the open-market-purchase gate.
      - is_10b5_1 False passes the pre-scheduled-trade gate.
      - price_per_share $50 (Decimal) passes the >=$5 penny-stock gate.
      - filing_date one business day after transaction_date passes the
        <=2-business-day staleness gate.
    """
    return {
        "accession_no": "acc-test",
        "filing_date": date(2026, 3, 2),  # Mon
        "transaction_date": date(2026, 3, 2),  # Mon — 0 business days lag
        "ticker": ticker,
        "issuer_cik": "0000000001",
        "issuer_name": "Test Corp",
        "insider_cik": "0000000100",
        "insider_name": "A Person",
        "insider_title": "Director",
        "is_officer": False,
        "is_director": True,
        "is_ten_percent_owner": False,
        "transaction_code": "P",
        "shares": Decimal("1000"),
        "price_per_share": Decimal("50"),
        "value_usd": Decimal("50000"),
        "is_10b5_1": False,
        "shares_owned_after": Decimal("10000"),
    }


def test_sentinel_string_tickers_are_filtered() -> None:
    """All sentinel-string variants must be dropped, only true tickers remain.

    The filter normalizes whitespace + case before matching, so lowercase
    ``"na"`` and whitespace-padded ``" NONE "`` must also be filtered.
    """
    rows = [
        _row("AAPL"),        # valid — keep
        _row("MSFT"),        # valid — keep
        _row("NONE"),        # sentinel — drop
        _row("N/A"),         # sentinel — drop
        _row("na"),          # sentinel (lowercase) — drop after .str.upper()
        _row(" NONE "),      # sentinel (whitespace) — drop after .str.strip()
        _row(""),            # empty string — drop
        _row(None),          # true null — drop via .notna()
    ]
    df = pd.DataFrame(rows)

    result = apply_universal_gates(df)

    # Only the two valid rows should survive. The sort guards against any
    # incidental row reordering inside apply_universal_gates.
    assert sorted(result["ticker"].tolist()) == ["AAPL", "MSFT"]
    assert len(result) == 2
