"""EDGAR full-index helpers.

EDGAR publishes quarterly `form.idx` files at:
    https://www.sec.gov/Archives/edgar/full-index/<YYYY>/QTR<n>/form.idx

`form.idx` is a pipe-padded fixed-column text file, sorted by form type:

    Form Type   Company Name                       CIK         Date Filed   File Name
    -----------------------------------------------------------------------------------
    4           ACME INC                           1234567     2026-05-01   edgar/data/1234567/0001234567-26-000001-index.htm
    4/A         ...

We filter to rows whose Form Type starts with "4" (catches 4 and 4/A). The
filename column points at the filing's *index* page; from the accession
number embedded there we build the URL for the actual primary_doc.xml.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from .client import EdgarClient

_ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
_IDX_HEADER_COLUMNS = ("Form Type", "Company Name", "CIK", "Date Filed", "File Name")

# Why: EDGAR pads form.idx data columns wider than the header row, so column
# offsets derived from the header don't slice rows correctly. Instead we
# anchor on structural invariants — CIK is digits, date is ISO, the filename
# starts with "edgar/" and has no whitespace — and let the lazy `.+?`
# backtrack to grab the company name in between.
_IDX_ROW_RE = re.compile(
    r"^(?P<form>\S+)"
    r"\s{2,}(?P<company>.+?)"
    r"\s{2,}(?P<cik>\d+)"
    r"\s+(?P<date>\d{4}-\d{2}-\d{2})"
    r"\s+(?P<file>edgar/\S+)"
    r"\s*$"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FormIndexEntry:
    form_type: str
    company_name: str
    cik: str  # zero-padded 10 digits
    filing_date: date
    accession_no: str  # canonical form: NNNNNNNNNN-NN-NNNNNN
    directory_url: str  # URL of the filing's directory; the XML name varies per filing


def _quarter_for(d: date) -> int:
    return (d.month - 1) // 3 + 1


def quarters_in_range(start: date, end: date) -> list[tuple[int, int]]:
    """All (year, quarter) tuples whose 3-month window touches [start, end]."""
    if start > end:
        raise ValueError("start must be <= end")
    out: list[tuple[int, int]] = []
    year = start.year
    q = _quarter_for(start)
    while (year, q) <= (end.year, _quarter_for(end)):
        out.append((year, q))
        q += 1
        if q > 4:
            q = 1
            year += 1
    return out


def months_in_range(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cur = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cur <= last:
        out.append((cur.year, cur.month))
        # advance one month
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out


def _accession_from_index_path(index_path: str) -> str:
    # Why: form.idx points at either `<accession>.txt` or `<accession>-index.htm`
    # depending on the form; regex finds the canonical 10-2-6 pattern regardless.
    m = _ACCESSION_RE.search(index_path)
    return m.group(0) if m else ""


def _filing_directory_url(cik: str, accession_no: str) -> str:
    # Why: Form 4 XML filenames vary per filer agent (e.g. ownership.xml,
    # form4_*.xml, wk-form4_*.xml) — we cannot hardcode the filename. The
    # directory URL is canonical and EDGAR accepts any party's CIK.
    cik_int = str(int(cik))  # strip leading zeros for the URL path
    accession_nodash = accession_no.replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{accession_nodash}"
    )


def parse_form_idx(body: str, *, form_filter: str = "4") -> list[FormIndexEntry]:
    """Parse a form.idx body, returning entries whose Form Type matches `form_filter`.

    `form_filter` matches "4" or "4/A" exactly (not prefix); other form types
    are excluded.
    """
    lines = body.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if all(name in line for name in _IDX_HEADER_COLUMNS):
            header_idx = i
            break
    if header_idx is None:
        logger.warning("form.idx: header row not found in %d lines", len(lines))
        return []

    out: list[FormIndexEntry] = []
    for line in lines[header_idx + 1 :]:
        if not line.strip():
            continue
        stripped = line.rstrip()
        if set(stripped) <= {"-", " "}:
            continue
        m = _IDX_ROW_RE.match(line)
        if not m:
            continue
        form_type = m.group("form")
        if form_type not in {"4", "4/A"}:
            continue
        try:
            filing_date = date.fromisoformat(m.group("date"))
        except ValueError:
            logger.debug("form.idx: bad date %r, skipping", m.group("date"))
            continue
        cik_padded = m.group("cik").zfill(10)
        accession = _accession_from_index_path(m.group("file"))
        if not accession:
            continue
        out.append(
            FormIndexEntry(
                form_type=form_type,
                company_name=m.group("company").strip(),
                cik=cik_padded,
                filing_date=filing_date,
                accession_no=accession,
                directory_url=_filing_directory_url(cik_padded, accession),
            )
        )
    return out


def fetch_quarter_index(client: EdgarClient, year: int, quarter: int) -> list[FormIndexEntry]:
    url = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"
    body = client.get_text(
        url,
        cache_key=f"full-index/{year}/QTR{quarter}/form.idx",
        encoding="latin-1",  # EDGAR ships latin-1 for these
    )
    entries = parse_form_idx(body)
    logger.info("fetched %d Form 4 entries from %d-Q%d", len(entries), year, quarter)
    return entries


def filter_by_date_range(
    entries: Iterable[FormIndexEntry], start: date, end: date
) -> list[FormIndexEntry]:
    return [e for e in entries if start <= e.filing_date <= end]


def date_range_for_months_back(months: int, *, today: date | None = None) -> tuple[date, date]:
    """Return (start, end) covering the last `months` months ending today.

    `months=12` and today=2026-05-17 -> (2025-05-17, 2026-05-17). Calendar
    quarters are derived from this range, so we slightly over-fetch index
    files at the boundary and filter to the exact date range later.
    """
    end_date = today or date.today()
    # Approximate "N months back" as N*30 days. Strategist filters at the
    # row level anyway, so day-level precision at the boundary is enough.
    start_date = end_date - timedelta(days=months * 30)
    return start_date, end_date
