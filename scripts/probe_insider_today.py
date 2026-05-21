"""How many insider signals does the adapter have for today's entry date?

Run alongside the bot to confirm whether InsiderStrategy *should* be
producing any signals today. Prints the per-detector signal count and
the symbols.
"""

from __future__ import annotations

from datetime import date

from trading_bot.strategy.insider import InsiderStrategy, load_config


def main() -> int:
    cfg = load_config()
    strat = InsiderStrategy(cfg)
    idx = strat._signals_by_entry_date  # private; this is a diagnostic
    today = date.today()
    print(f"today (local-date used for indexing): {today.isoformat()}")
    print(f"total entry-dates in index: {len(idx)}")
    print(f"total signals across all dates: {sum(len(v) for v in idx.values())}")
    print()
    todays = idx.get(today, [])
    print(f"signals with effective_entry_date == today: {len(todays)}")
    for sig in todays:
        print(
            f"  {sig['ticker']:<8} strategy={sig['strategy']:<18} "
            f"strength={sig['strength']:.3f}  max_filing_date={sig['max_filing_date']}"
        )

    # Also show the next few entry dates so we know when Insider WILL fire.
    upcoming = sorted(d for d in idx.keys() if d >= today)[:5]
    print()
    print("upcoming entry dates with signals:")
    for d in upcoming:
        print(f"  {d.isoformat()}  ({len(idx[d])} signals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
