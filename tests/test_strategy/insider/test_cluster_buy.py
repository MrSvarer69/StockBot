from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from trading_bot.strategy.insider import cluster_buy_signals

from .fixtures import filings_frame, make_cluster, make_filing


def test_three_insiders_within_window_emit_signal():
    rows = make_cluster()
    signals = cluster_buy_signals(filings_frame(rows))

    assert len(signals) == 1
    sig = signals.iloc[0]
    assert sig["ticker"] == "ACME"
    assert sig["strategy"] == "insider.cluster_buy"
    assert sig["metadata"]["distinct_insider_count"] == 3
    assert sig["metadata"]["total_value_usd"] == Decimal("225000")
    assert 0.0 <= sig["strength"] <= 1.0


def test_two_insiders_below_threshold_no_signal():
    rows = make_cluster(insider_ciks=["0000000100", "0000000101"])
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_min_insiders_minus_one_exact_boundary():
    rows = make_cluster()
    signals = cluster_buy_signals(filings_frame(rows), min_insiders=4)
    assert signals.empty


def test_same_insider_twice_does_not_count_twice():
    cik = "0000000100"
    rows = [
        make_filing(
            accession_no="a1", insider_cik=cik, insider_name="A",
            transaction_date=date(2026, 3, 1), filing_date=date(2026, 3, 2),
        ),
        make_filing(
            accession_no="a2", insider_cik=cik, insider_name="A",
            transaction_date=date(2026, 3, 3), filing_date=date(2026, 3, 4),
        ),
        make_filing(
            accession_no="a3", insider_cik="0000000101", insider_name="B",
            transaction_date=date(2026, 3, 4), filing_date=date(2026, 3, 5),
        ),
    ]
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_outside_window_does_not_cluster():
    rows = make_cluster(spacing_days=8)
    signals = cluster_buy_signals(filings_frame(rows), window_days=10)
    # Three insiders spaced 8 days apart -> total span 16 days, no 10-day window
    # contains all three; only the latter two fall in the same window which is
    # below threshold.
    assert signals.empty


def test_gate_excludes_10b5_1_plan_trades():
    rows = make_cluster()
    rows[0]["is_10b5_1"] = True
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_gate_excludes_non_p_codes():
    rows = make_cluster()
    rows[1]["transaction_code"] = "S"
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_gate_excludes_penny_stocks():
    rows = make_cluster()
    for r in rows:
        r["price_per_share"] = Decimal("4.99")
        r["value_usd"] = Decimal(str(r["shares"])) * r["price_per_share"]
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_gate_excludes_stale_filings():
    rows = make_cluster()
    rows[0]["filing_date"] = rows[0]["transaction_date"] + timedelta(days=30)
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_gate_excludes_null_ticker():
    rows = make_cluster()
    rows[0]["ticker"] = None
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_separate_tickers_do_not_combine():
    rows = make_cluster(ticker="ACME", insider_ciks=["100", "101"])
    rows += make_cluster(ticker="BETA", insider_ciks=["200"])
    signals = cluster_buy_signals(filings_frame(rows))
    assert signals.empty


def test_dedup_does_not_double_emit_for_same_membership():
    base = date(2026, 3, 1)
    rows = [
        make_filing(
            accession_no=f"acc-{i}",
            insider_cik=cik,
            insider_name=f"I{i}",
            transaction_date=base + timedelta(days=i),
            filing_date=base + timedelta(days=i + 1),
        )
        for i, cik in enumerate(["100", "101", "102"])
    ]
    # A fourth buy from an already-counted insider should not re-emit.
    rows.append(
        make_filing(
            accession_no="acc-3",
            insider_cik="100",
            insider_name="I0",
            transaction_date=base + timedelta(days=4),
            filing_date=base + timedelta(days=5),
        )
    )
    signals = cluster_buy_signals(filings_frame(rows))
    assert len(signals) == 1


def test_new_member_in_cluster_re_emits():
    base = date(2026, 3, 1)
    rows = [
        make_filing(
            accession_no=f"acc-{i}",
            insider_cik=cik,
            insider_name=f"I{i}",
            transaction_date=base + timedelta(days=i),
            filing_date=base + timedelta(days=i + 1),
        )
        for i, cik in enumerate(["100", "101", "102"])
    ]
    rows.append(
        make_filing(
            accession_no="acc-new",
            insider_cik="103",
            insider_name="I3",
            transaction_date=base + timedelta(days=5),
            filing_date=base + timedelta(days=6),
        )
    )
    signals = cluster_buy_signals(filings_frame(rows))
    assert len(signals) == 2
    assert signals.iloc[1]["metadata"]["distinct_insider_count"] == 4


def test_empty_input_returns_empty_frame():
    signals = cluster_buy_signals(filings_frame([]))
    assert signals.empty
    assert list(signals.columns) == ["ticker", "signal_date", "strategy", "strength", "metadata"]
