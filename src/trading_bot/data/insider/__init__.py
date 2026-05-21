"""SEC EDGAR Form 4 (insider transaction) ingestion.

Public surface:
    Form4Filing       canonical row schema (frozen dataclass)
    parse_form4_xml   parse a primary_doc.xml into one or more Form4Filing rows
    EdgarClient       rate-limited SEC EDGAR HTTP client
    TickerMap         CIK -> ticker resolver
    Form4Cache        Parquet partitioned cache (year/month)
    backfill          high-level backfill orchestrator
"""

from __future__ import annotations

from .backfill import BackfillStats, run_backfill
from .cache import Form4Cache
from .client import EdgarClient
from .parser import Form4ParseError, parse_form4_xml
from .refresh import refresh_cache_to_today
from .schema import (
    FORM4_PARQUET_SCHEMA,
    Form4Filing,
    filings_to_arrow_table,
)
from .tickers import TickerMap

__all__ = [
    "BackfillStats",
    "EdgarClient",
    "FORM4_PARQUET_SCHEMA",
    "Form4Cache",
    "Form4Filing",
    "Form4ParseError",
    "TickerMap",
    "filings_to_arrow_table",
    "parse_form4_xml",
    "refresh_cache_to_today",
    "run_backfill",
]
