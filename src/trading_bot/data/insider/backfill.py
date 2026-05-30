"""High-level Form 4 backfill orchestrator.

Pipeline:
    1. Build / refresh CIK -> ticker map from SEC.
    2. For each quarter in the requested window, fetch form.idx.
    3. Filter to Form 4 / 4/A entries in the requested date range.
    4. Skip any accession_no already present in the cache (resumability).
    5. For each new entry: fetch index.json, resolve XML name, fetch XML,
       parse, accumulate.
    6. Write rows out in batches partitioned by filing_date year/month.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date

from .cache import Form4Cache
from .client import EdgarClient, EdgarHTTPError
from .index import (
    FormIndexEntry,
    fetch_quarter_index,
    filter_by_date_range,
    quarters_in_range,
)
from .parser import Form4ParseError, parse_form4_xml
from .schema import Form4Filing
from .tickers import TickerMap

logger = logging.getLogger(__name__)


@dataclass
class BackfillStats:
    quarters_scanned: int = 0
    index_entries_seen: int = 0
    filings_fetched: int = 0
    filings_skipped_cached: int = 0
    filings_failed: int = 0
    rows_written: int = 0
    files_written: int = 0


def _flush(cache: Form4Cache, batch: list[Form4Filing], stats: BackfillStats) -> None:
    if not batch:
        return
    paths = cache.write(batch)
    stats.rows_written += len(batch)
    stats.files_written += len(paths)
    batch.clear()


def run_backfill(
    *,
    client: EdgarClient,
    cache: Form4Cache,
    ticker_map: TickerMap,
    start_date: date,
    end_date: date,
    batch_size: int = 500,
    max_filings: int | None = None,
) -> BackfillStats:
    """Run the backfill end-to-end and return stats.

    Parameters
    ----------
    batch_size : number of rows to accumulate before flushing to Parquet.
    max_filings : safety cap on number of filings to fetch this run; None
        means no cap. Useful when smoke-testing the pipeline before kicking
        off a full 12-month run.
    """
    stats = BackfillStats()
    already_have = cache.existing_accessions()
    logger.info("cache already contains %d accessions", len(already_have))

    quarters = quarters_in_range(start_date, end_date)
    logger.info(
        "backfill window %s -> %s spans %d quarter(s)",
        start_date,
        end_date,
        len(quarters),
    )

    batch: list[Form4Filing] = []
    for year, quarter in quarters:
        stats.quarters_scanned += 1
        try:
            entries = fetch_quarter_index(client, year, quarter)
        except EdgarHTTPError as exc:
            logger.error("failed to fetch index %d-Q%d: %s", year, quarter, exc)
            continue

        in_range = filter_by_date_range(entries, start_date, end_date)
        stats.index_entries_seen += len(in_range)
        logger.info(
            "%d-Q%d: %d entries, %d in window",
            year,
            quarter,
            len(entries),
            len(in_range),
        )

        for entry in in_range:
            if max_filings is not None and stats.filings_fetched >= max_filings:
                logger.info("hit max_filings=%d, stopping early", max_filings)
                _flush(cache, batch, stats)
                return stats

            if entry.accession_no in already_have:
                stats.filings_skipped_cached += 1
                continue

            rows = _process_entry(client, entry, ticker_map)
            if rows is None:
                stats.filings_failed += 1
                continue
            stats.filings_fetched += 1
            batch.extend(rows)
            already_have.add(entry.accession_no)

            if len(batch) >= batch_size:
                _flush(cache, batch, stats)

    _flush(cache, batch, stats)
    return stats


def _select_form4_xml_name(items: list[dict], accession_no: str) -> str | None:
    # Form 4 directories typically contain: <accession>-index-headers.html,
    # <accession>-index.html, <accession>.txt, and exactly one form XML.
    # The XML name varies (ownership.xml, form4_*.xml, wk-form4_*.xml, etc.)
    # so we pick the first .xml that isn't an index artifact.
    candidates = []
    for item in items:
        name = item.get("name", "")
        if not name.endswith(".xml"):
            continue
        if "-index." in name:
            continue
        if name.startswith(accession_no):
            continue
        candidates.append(name)
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.debug(
            "filing %s has %d xml candidates; using first: %s",
            accession_no, len(candidates), candidates[0],
        )
    return candidates[0]


def _process_entry(
    client: EdgarClient,
    entry: FormIndexEntry,
    ticker_map: TickerMap,
) -> list[Form4Filing] | None:
    """Fetch and parse one filing. Returns None on a recoverable failure."""
    index_url = f"{entry.directory_url}/index.json"
    try:
        index_body = client.get_bytes(
            index_url, cache_key=index_url.split("://", 1)[-1]
        )
    except EdgarHTTPError as exc:
        logger.warning("index fetch failed for %s: %s", entry.accession_no, exc)
        return None

    try:
        index_data = json.loads(index_body)
        items = index_data["directory"]["item"]
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("index parse failed for %s: %s", entry.accession_no, exc)
        return None

    xml_name = _select_form4_xml_name(items, entry.accession_no)
    if not xml_name:
        logger.warning("no form xml found in %s", entry.accession_no)
        return None

    xml_url = f"{entry.directory_url}/{xml_name}"
    try:
        body = client.get_bytes(xml_url, cache_key=xml_url.split("://", 1)[-1])
    except EdgarHTTPError as exc:
        logger.warning("xml fetch failed for %s: %s", entry.accession_no, exc)
        return None

    ticker = ticker_map.lookup(entry.cik)
    try:
        rows = parse_form4_xml(
            body,
            accession_no=entry.accession_no,
            filing_date=entry.filing_date,
            ticker_override=ticker,
        )
    except Form4ParseError as exc:
        logger.warning("parse failed for %s: %s", entry.accession_no, exc)
        return None
    return rows
