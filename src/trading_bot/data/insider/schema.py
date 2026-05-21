"""Canonical Form 4 row schema.

The dtypes documented here are a contract shared with the strategist agent.
Do not change column names or types without coordinating across both modules.

Monetary fields are Decimal (per project rule); they survive a Parquet
round trip as Arrow decimal128(28,4). Dates are stored as Arrow date32 in
UTC; Form 4 XML carries dates only (no time-of-day), so the UTC conversion
is a labelling step performed at parse time.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from typing import Iterable

import pyarrow as pa

# Decimal scale/precision used for shares, prices, value. (28,4) gives 24
# digits of integer headroom — well above any plausible share count or
# notional dollar value — and 4 fractional digits, enough for cent prices
# and fractional shares as reported on Form 4.
_DECIMAL_PRECISION = 28
_DECIMAL_SCALE = 4

FORM4_PARQUET_SCHEMA: pa.Schema = pa.schema(
    [
        ("accession_no", pa.string()),
        ("filing_date", pa.date32()),
        ("transaction_date", pa.date32()),
        ("ticker", pa.string()),
        ("issuer_cik", pa.string()),
        ("issuer_name", pa.string()),
        ("insider_cik", pa.string()),
        ("insider_name", pa.string()),
        ("insider_title", pa.string()),
        ("is_officer", pa.bool_()),
        ("is_director", pa.bool_()),
        ("is_ten_percent_owner", pa.bool_()),
        ("transaction_code", pa.string()),
        ("shares", pa.decimal128(_DECIMAL_PRECISION, _DECIMAL_SCALE)),
        ("price_per_share", pa.decimal128(_DECIMAL_PRECISION, _DECIMAL_SCALE)),
        ("value_usd", pa.decimal128(_DECIMAL_PRECISION, _DECIMAL_SCALE)),
        ("is_10b5_1", pa.bool_()),
        (
            "shares_owned_after",
            pa.decimal128(_DECIMAL_PRECISION, _DECIMAL_SCALE),
        ),
    ]
)


@dataclass(frozen=True)
class Form4Filing:
    """One transaction row from a Form 4 filing.

    A single filing can contain multiple transactions; the parser emits one
    Form4Filing per non-derivative transaction. `accession_no` is shared
    across rows from the same filing.
    """

    accession_no: str
    filing_date: date
    transaction_date: date
    ticker: str | None
    issuer_cik: str
    issuer_name: str
    insider_cik: str
    insider_name: str
    insider_title: str | None
    is_officer: bool
    is_director: bool
    is_ten_percent_owner: bool
    transaction_code: str
    shares: Decimal
    price_per_share: Decimal
    value_usd: Decimal
    is_10b5_1: bool
    shares_owned_after: Decimal


def _column_names() -> list[str]:
    return [f.name for f in fields(Form4Filing)]


def filings_to_arrow_table(filings: Iterable[Form4Filing]) -> pa.Table:
    """Build an Arrow Table from Form4Filing rows using FORM4_PARQUET_SCHEMA.

    Decimal fields are passed through pyarrow's decimal128 conversion.
    Empty input still produces an empty table with the canonical schema, so
    downstream partition writes always carry the contract.
    """
    column_names = _column_names()
    columns: dict[str, list] = {name: [] for name in column_names}
    for row in filings:
        columns["accession_no"].append(row.accession_no)
        columns["filing_date"].append(row.filing_date)
        columns["transaction_date"].append(row.transaction_date)
        columns["ticker"].append(row.ticker)
        columns["issuer_cik"].append(row.issuer_cik)
        columns["issuer_name"].append(row.issuer_name)
        columns["insider_cik"].append(row.insider_cik)
        columns["insider_name"].append(row.insider_name)
        columns["insider_title"].append(row.insider_title)
        columns["is_officer"].append(row.is_officer)
        columns["is_director"].append(row.is_director)
        columns["is_ten_percent_owner"].append(row.is_ten_percent_owner)
        columns["transaction_code"].append(row.transaction_code)
        columns["shares"].append(row.shares)
        columns["price_per_share"].append(row.price_per_share)
        columns["value_usd"].append(row.value_usd)
        columns["is_10b5_1"].append(row.is_10b5_1)
        columns["shares_owned_after"].append(row.shares_owned_after)

    arrays: list[pa.Array] = []
    for field in FORM4_PARQUET_SCHEMA:
        arrays.append(pa.array(columns[field.name], type=field.type))
    return pa.Table.from_arrays(arrays, schema=FORM4_PARQUET_SCHEMA)
