"""Polite SEC EDGAR HTTP client.

SEC's fair-access policy requires a descriptive User-Agent and caps traffic
at ~10 requests/sec per source IP. This client enforces both, with
exponential backoff on 429/503.

Stdlib-only on purpose: keeps the data layer free of extra deps. If we
ever need parallelism, swap in httpx with a semaphore.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

EDGAR_BASE = "https://www.sec.gov"
EDGAR_DATA_BASE = "https://www.sec.gov"  # full-index, Archives, files all share host
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC limit is 10 req/sec; pin to 8 to leave headroom for clock drift.
DEFAULT_REQUESTS_PER_SECOND = 8

# Fallback UA used only if SEC_USER_AGENT is unset. SEC explicitly asks for
# a contact string; "trading-bot-dev" plus a placeholder address is the
# minimal compliant form while loudly suggesting the operator override it.
_FALLBACK_UA = "trading-bot-dev contact@example.invalid"


class EdgarHTTPError(RuntimeError):
    """Non-retryable EDGAR response (4xx other than 429, or final 5xx)."""

    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"EDGAR {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


class _RateLimiter:
    """Sliding-window limiter: at most N requests in any rolling 1-second window."""

    def __init__(self, max_per_second: int):
        self._max = max_per_second
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            cutoff = now - 1.0
            while self._times and self._times[0] < cutoff:
                self._times.popleft()
            if len(self._times) >= self._max:
                sleep_for = 1.0 - (now - self._times[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                cutoff = now - 1.0
                while self._times and self._times[0] < cutoff:
                    self._times.popleft()
            self._times.append(now)


class EdgarClient:
    """Thin urllib wrapper with rate limiting, retries, and an on-disk raw cache.

    Caching: every successful GET is mirrored into `cache_dir` keyed by the
    URL path. Re-runs of the backfill skip the network entirely for already
    cached filings.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        user_agent: str | None = None,
        requests_per_second: int = DEFAULT_REQUESTS_PER_SECOND,
        max_retries: int = 5,
        backoff_base: float = 1.0,
    ):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        ua = user_agent or os.environ.get("SEC_USER_AGENT") or _FALLBACK_UA
        if ua == _FALLBACK_UA:
            logger.warning(
                "SEC_USER_AGENT not set; using fallback UA. "
                "Set SEC_USER_AGENT='Your Name your@email' for compliance."
            )
        self._headers = {
            "User-Agent": ua,
            "Host": "www.sec.gov",
        }
        self._limiter = _RateLimiter(requests_per_second)
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    # --- public ----------------------------------------------------------

    def get_bytes(self, url: str, *, cache_key: str | None = None) -> bytes:
        """GET `url`, returning the raw body. Uses on-disk cache when present."""
        cache_path = self._cache_path(url, cache_key)
        if cache_path.exists():
            return cache_path.read_bytes()
        body = self._fetch_with_retry(url)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(cache_path)
        return body

    def get_text(self, url: str, *, cache_key: str | None = None, encoding: str = "utf-8") -> str:
        return self.get_bytes(url, cache_key=cache_key).decode(encoding, errors="replace")

    # --- internals -------------------------------------------------------

    def _cache_path(self, url: str, cache_key: str | None) -> Path:
        if cache_key is None:
            # Strip scheme/host so file layout mirrors EDGAR's path structure;
            # easier to inspect than an opaque hash.
            key = url.split("://", 1)[-1]
            key = key.replace("?", "_q_").replace("&", "_a_").replace("=", "_e_")
        else:
            key = cache_key
        return self._cache_dir / key

    def _fetch_with_retry(self, url: str) -> bytes:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            self._limiter.acquire()
            try:
                req = urllib.request.Request(url, headers=self._headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code in (429, 503):
                    wait = self._backoff_base * (2**attempt)
                    logger.warning(
                        "EDGAR %s on %s; backoff %.1fs (attempt %d/%d)",
                        exc.code,
                        url,
                        wait,
                        attempt + 1,
                        self._max_retries,
                    )
                    time.sleep(wait)
                    continue
                # 404 etc. are not worth retrying — surface immediately.
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise EdgarHTTPError(exc.code, url, body) from exc
            except urllib.error.URLError as exc:
                last_exc = exc
                wait = self._backoff_base * (2**attempt)
                logger.warning(
                    "EDGAR URL error on %s: %s; backoff %.1fs (attempt %d/%d)",
                    url,
                    exc,
                    wait,
                    attempt + 1,
                    self._max_retries,
                )
                time.sleep(wait)
                continue
        raise EdgarHTTPError(0, url, f"exhausted retries: {last_exc!r}")
