"""Unit tests for the Form 4 XML parser and surrounding pipeline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from trading_bot.data.insider import (
    FORM4_PARQUET_SCHEMA,
    Form4Cache,
    Form4Filing,
    Form4ParseError,
    filings_to_arrow_table,
    parse_form4_xml,
)
from trading_bot.data.insider.index import (
    months_in_range,
    parse_form_idx,
    quarters_in_range,
)
from trading_bot.data.insider.tickers import TickerMap

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---- parser --------------------------------------------------------------


def test_parse_purchase_single_transaction():
    rows = parse_form4_xml(
        _read("form4_purchase.xml"),
        accession_no="0000320193-26-000010",
        filing_date=date(2026, 3, 15),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.accession_no == "0000320193-26-000010"
    assert row.filing_date == date(2026, 3, 15)
    assert row.transaction_date == date(2026, 3, 14)
    assert row.ticker == "FXTR"
    assert row.issuer_cik == "0000320193"
    assert row.issuer_name == "FIXTURE COMPANY INC"
    assert row.insider_cik == "0001234567"
    assert row.insider_name == "SMITH JOHN"
    assert row.insider_title == "Chief Executive Officer"
    assert row.is_officer is True
    assert row.is_director is True
    assert row.is_ten_percent_owner is False
    assert row.transaction_code == "P"
    assert row.shares == Decimal("1500")
    assert row.price_per_share == Decimal("123.4500")
    assert row.value_usd == Decimal("185175.0000")
    assert row.is_10b5_1 is False
    assert row.shares_owned_after == Decimal("11500")


def test_parse_sale_with_two_transactions_and_10b5_1():
    rows = parse_form4_xml(
        _read("form4_sale_10b5_1.xml"),
        accession_no="0000789012-26-000077",
        filing_date=date(2026, 4, 1),
    )
    assert len(rows) == 2
    codes = [r.transaction_code for r in rows]
    assert codes == ["S", "S"]
    # All rows from the filing carry the 10b5-1 flag because the marker
    # lives in footnotes (shared across transactions).
    assert all(r.is_10b5_1 for r in rows)
    # Ticker normalized to uppercase.
    assert rows[0].ticker == "ANFX"
    # Value is shares * price as Decimal.
    assert rows[0].value_usd == (Decimal("2000") * Decimal("55.7800")).quantize(
        Decimal("0.0001")
    )


def test_parse_ticker_override_wins_over_xml():
    rows = parse_form4_xml(
        _read("form4_purchase.xml"),
        accession_no="acc-1",
        filing_date=date(2026, 3, 15),
        ticker_override="OVRD",
    )
    assert rows[0].ticker == "OVRD"


def test_parse_no_nonderivative_returns_empty_list():
    rows = parse_form4_xml(
        _read("form4_no_nonderivative.xml"),
        accession_no="acc-2",
        filing_date=date(2026, 2, 10),
    )
    assert rows == []


def test_parse_malformed_xml_raises():
    with pytest.raises(Form4ParseError):
        parse_form4_xml(b"<not-valid", accession_no="acc-3", filing_date=date(2026, 1, 1))


def test_parse_missing_issuer_raises():
    body = b"<?xml version='1.0'?><ownershipDocument></ownershipDocument>"
    with pytest.raises(Form4ParseError):
        parse_form4_xml(body, accession_no="acc-4", filing_date=date(2026, 1, 1))


# ---- schema / arrow round-trip -------------------------------------------


def test_arrow_table_uses_canonical_schema():
    rows = parse_form4_xml(
        _read("form4_purchase.xml"),
        accession_no="0000320193-26-000010",
        filing_date=date(2026, 3, 15),
    )
    table = filings_to_arrow_table(rows)
    assert table.schema == FORM4_PARQUET_SCHEMA
    # Decimal fields are decimal128(28,4).
    assert str(table.schema.field("shares").type) == "decimal128(28, 4)"
    assert str(table.schema.field("price_per_share").type) == "decimal128(28, 4)"
    assert str(table.schema.field("value_usd").type) == "decimal128(28, 4)"


def test_empty_table_still_has_canonical_schema():
    table = filings_to_arrow_table([])
    assert table.num_rows == 0
    assert table.schema == FORM4_PARQUET_SCHEMA


# ---- cache --------------------------------------------------------------


def _row(accession: str, filing_date: date) -> Form4Filing:
    return Form4Filing(
        accession_no=accession,
        filing_date=filing_date,
        transaction_date=filing_date,
        ticker="ZZZZ",
        issuer_cik="0000000001",
        issuer_name="X",
        insider_cik="0000000002",
        insider_name="Y",
        insider_title=None,
        is_officer=True,
        is_director=False,
        is_ten_percent_owner=False,
        transaction_code="P",
        shares=Decimal("100"),
        price_per_share=Decimal("10.0000"),
        value_usd=Decimal("1000.0000"),
        is_10b5_1=False,
        shares_owned_after=Decimal("100"),
    )


def test_cache_partitions_by_year_month(tmp_path: Path):
    cache = Form4Cache(tmp_path)
    paths = cache.write(
        [
            _row("a-1", date(2026, 3, 1)),
            _row("a-2", date(2026, 3, 31)),
            _row("a-3", date(2026, 4, 1)),
        ]
    )
    assert len(paths) == 2
    parts = {p.parent.relative_to(tmp_path).as_posix() for p in paths}
    assert parts == {"year=2026/month=03", "year=2026/month=04"}


def test_cache_existing_accessions_after_write(tmp_path: Path):
    cache = Form4Cache(tmp_path)
    cache.write([_row("acc-A", date(2026, 1, 5)), _row("acc-B", date(2026, 2, 5))])
    assert cache.existing_accessions() == {"acc-A", "acc-B"}


def test_cache_round_trip_preserves_decimals(tmp_path: Path):
    cache = Form4Cache(tmp_path)
    rows = parse_form4_xml(
        _read("form4_purchase.xml"),
        accession_no="acc-X",
        filing_date=date(2026, 3, 15),
    )
    cache.write(rows)
    parts = list(tmp_path.rglob("*.parquet"))
    assert len(parts) == 1
    # Read as a plain file (no partition discovery) — schema should be
    # bit-for-bit identical to the canonical schema we declared.
    pf = pq.ParquetFile(parts[0])
    assert pf.schema_arrow == FORM4_PARQUET_SCHEMA
    table = pf.read()
    py = table.to_pylist()[0]
    assert py["accession_no"] == "acc-X"
    assert py["shares"] == Decimal("1500.0000")
    assert py["price_per_share"] == Decimal("123.4500")


def test_cache_empty_write_returns_no_files(tmp_path: Path):
    cache = Form4Cache(tmp_path)
    assert cache.write([]) == []


# ---- index helpers ------------------------------------------------------


def test_quarters_in_range_spans_year_boundary():
    qs = quarters_in_range(date(2025, 11, 1), date(2026, 4, 1))
    assert qs == [(2025, 4), (2026, 1), (2026, 2)]


def test_months_in_range():
    ms = months_in_range(date(2025, 12, 5), date(2026, 2, 10))
    assert ms == [(2025, 12), (2026, 1), (2026, 2)]


def test_parse_form_idx_filters_form4_only():
    # Fixture mirrors the real EDGAR format: single unbroken dashed divider,
    # filenames ending in `.txt`. Column starts are derived from the header row.
    body = """\
Description:           Master Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    March 31, 2026




Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
4                ACME INC                                                      1234567     2026-03-15  edgar/data/1234567/0001234567-26-000010.txt
4/A              ACME INC                                                      1234567     2026-03-16  edgar/data/1234567/0001234567-26-000011.txt
424B2            UNRELATED CORP                                                9999999     2026-03-15  edgar/data/9999999/0009999999-26-000001.txt
10-K             ALSO UNRELATED                                                8888888     2026-03-15  edgar/data/8888888/0008888888-26-000002.txt
"""
    entries = parse_form_idx(body)
    assert len(entries) == 2
    assert {e.form_type for e in entries} == {"4", "4/A"}
    e0 = entries[0]
    assert e0.cik == "0001234567"
    assert e0.accession_no == "0001234567-26-000010"
    assert e0.filing_date == date(2026, 3, 15)
    assert e0.directory_url == (
        "https://www.sec.gov/Archives/edgar/data/1234567/"
        "000123456726000010"
    )


# ---- ticker map ---------------------------------------------------------


def test_ticker_map_parses_sec_company_tickers_format():
    body = """\
{
  "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
  "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"}
}
"""
    tmap = TickerMap._parse(body)
    assert len(tmap) == 2
    assert tmap.lookup("0000320193") == "AAPL"
    assert tmap.lookup("320193") == "AAPL"  # zfill applied
    assert tmap.lookup("0000000000") is None
