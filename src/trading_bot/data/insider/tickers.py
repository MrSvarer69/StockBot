"""CIK -> ticker resolution backed by SEC's company_tickers.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import EdgarClient

logger = logging.getLogger(__name__)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class TickerMap:
    """Maps a 10-digit padded CIK to its primary ticker.

    SEC publishes `company_tickers.json` with one entry per company; the
    file lists a CIK and the company's primary listing. We don't attempt
    to resolve multi-class issuers to a specific class — strategist agent
    handles that ambiguity if it matters for a signal.
    """

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    @classmethod
    def from_client(cls, client: "EdgarClient") -> "TickerMap":
        body = client.get_text(
            COMPANY_TICKERS_URL,
            cache_key="files/company_tickers.json",
        )
        return cls._parse(body)

    @classmethod
    def from_file(cls, path: Path) -> "TickerMap":
        return cls._parse(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def _parse(cls, body: str) -> "TickerMap":
        raw = json.loads(body)
        # SEC publishes the file as {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        mapping: dict[str, str] = {}
        if isinstance(raw, dict):
            iterable = raw.values()
        else:
            iterable = raw
        for entry in iterable:
            cik_raw = entry.get("cik_str") if isinstance(entry, dict) else None
            ticker = entry.get("ticker") if isinstance(entry, dict) else None
            if cik_raw is None or not ticker:
                continue
            cik = str(cik_raw).zfill(10)
            # First write wins. SEC's file is ordered by primary listing;
            # subsequent records for the same CIK (rare) are alternates.
            mapping.setdefault(cik, str(ticker).upper())
        logger.info("loaded %d CIK->ticker mappings", len(mapping))
        return cls(mapping)

    def lookup(self, cik: str) -> str | None:
        return self._mapping.get(str(cik).zfill(10))

    def __len__(self) -> int:
        return len(self._mapping)
