"""Parquet cache for Form 4 rows, partitioned by filing_date year/month.

Layout:
    data/insider/year=YYYY/month=MM/part-<batch_id>.parquet

Each `write` call produces one partition file per (year, month) covered by
the batch. The strategist agent reads the dataset via pyarrow's dataset API
(or `pd.read_parquet(path, engine="pyarrow")` over the directory) and sees
the canonical schema regardless of how the partitions were written.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from .schema import FORM4_PARQUET_SCHEMA, Form4Filing, filings_to_arrow_table

logger = logging.getLogger(__name__)


class Form4Cache:
    """Append-only, year/month-partitioned Parquet cache."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _partition_dir(self, year: int, month: int) -> Path:
        return self.root / f"year={year:04d}" / f"month={month:02d}"

    def write(self, filings: Iterable[Form4Filing]) -> list[Path]:
        """Append a batch. Returns the list of files written.

        Rows are grouped by (filing_date.year, filing_date.month) so a single
        Form 4 always lands in the partition for its filing date — matching
        the way the strategist will query "give me filings from month X".

        Each call produces a new uniquely named file inside the partition,
        which keeps the writer concurrency-safe and lets re-runs add data
        without rewriting existing files. The trade-off is many small files
        over time; a periodic compaction job (not in scope here) can merge.
        """
        rows = list(filings)
        if not rows:
            return []

        groups: dict[tuple[int, int], list[Form4Filing]] = defaultdict(list)
        for row in rows:
            key = (row.filing_date.year, row.filing_date.month)
            groups[key].append(row)

        written: list[Path] = []
        for (year, month), batch in groups.items():
            part_dir = self._partition_dir(year, month)
            part_dir.mkdir(parents=True, exist_ok=True)
            table = filings_to_arrow_table(batch)
            path = part_dir / f"part-{uuid.uuid4().hex[:12]}.parquet"
            # Disable dictionary encoding on string columns so a re-read
            # produces the canonical `string` type (not `dictionary<...>`)
            # — keeps the on-disk schema identical to FORM4_PARQUET_SCHEMA.
            pq.write_table(
                table,
                path,
                compression="snappy",
                use_dictionary=False,
            )
            written.append(path)
            logger.info("wrote %d rows -> %s", len(batch), path)
        return written

    def existing_accessions(self) -> set[str]:
        """Return the set of accession numbers already present in the cache.

        Used by the backfill loop to skip already-ingested filings. Reads
        only the `accession_no` column from every partition for speed.
        """
        if not self.root.exists():
            return set()
        accessions: set[str] = set()
        for part in self.root.rglob("*.parquet"):
            try:
                table = pq.read_table(part, columns=["accession_no"])
            except Exception:
                logger.warning("failed to read accessions from %s", part)
                continue
            accessions.update(
                v.as_py() for v in table.column("accession_no") if v.as_py() is not None
            )
        return accessions

    @property
    def schema(self) -> pa.Schema:
        return FORM4_PARQUET_SCHEMA
