"""Insider-trading (EDGAR Form 4) signal filters. Pure functions over a Form-4 DataFrame.

Two filters:
- cluster_buy: multiple distinct insiders buying the same ticker in a short window.
- csuite_conviction: a large open-market buy by a CEO or CFO, with cooldown.

Both consume the same schema produced by the data-engineer's EDGAR ingestion
and emit the unified signals shape defined in this package.
"""

from .cluster_buy import cluster_buy_signals
from .csuite_conviction import csuite_conviction_signals
from .gates import apply_universal_gates, empty_insider_signals_frame
from .strategy import InsiderConfig, InsiderStrategy, load_config

__all__ = [
    "InsiderConfig",
    "InsiderStrategy",
    "apply_universal_gates",
    "cluster_buy_signals",
    "csuite_conviction_signals",
    "empty_insider_signals_frame",
    "load_config",
]
