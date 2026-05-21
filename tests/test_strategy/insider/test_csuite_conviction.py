from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from trading_bot.strategy.insider import csuite_conviction_signals

from .fixtures import filings_frame, make_filing


def _ceo_buy(**overrides):
    base = dict(
        accession_no="acc-ceo-1",
        ticker="ACME",
        insider_cik="ceo-1",
        insider_name="Jane CEO",
        insider_title="Chief Executive Officer",
        is_officer=True,
        is_director=False,
        transaction_date=date(2026, 3, 1),
        filing_date=date(2026, 3, 2),
        shares=Decimal("2500"),
        price_per_share=Decimal("60"),
        value_usd=Decimal("150000"),
    )
    base.update(overrides)
    return make_filing(**base)


def test_large_ceo_buy_emits_signal():
    rows = [_ceo_buy()]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert len(signals) == 1
    sig = signals.iloc[0]
    assert sig["ticker"] == "ACME"
    assert sig["strategy"] == "insider.csuite_conviction"
    assert sig["metadata"]["insider_cik"] == "ceo-1"
    assert sig["metadata"]["value_usd"] == Decimal("150000")
    assert 0.0 <= sig["strength"] <= 1.0


def test_cfo_title_variants_are_accepted():
    titles = ["CFO", "Chief Financial Officer", "Chief Financial Officer & EVP", "cfo"]
    rows = [
        _ceo_buy(
            accession_no=f"acc-cfo-{i}",
            insider_cik=f"cfo-{i}",
            insider_name=f"CFO {i}",
            insider_title=t,
        )
        for i, t in enumerate(titles)
    ]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert len(signals) == len(titles)


def test_non_csuite_title_rejected():
    rows = [_ceo_buy(insider_title="VP of Sales")]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert signals.empty


def test_not_officer_rejected_even_with_ceo_title():
    rows = [_ceo_buy(is_officer=False)]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert signals.empty


def test_below_min_value_rejected():
    rows = [_ceo_buy(shares=Decimal("100"), price_per_share=Decimal("50"), value_usd=Decimal("5000"))]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert signals.empty


def test_at_min_value_threshold_inclusive():
    rows = [_ceo_buy(value_usd=Decimal("100000"))]
    signals = csuite_conviction_signals(filings_frame(rows), min_value_usd=Decimal("100000"))
    assert len(signals) == 1


def test_cooldown_blocks_repeat_buy_inside_window():
    rows = [
        _ceo_buy(accession_no="acc-1", transaction_date=date(2026, 1, 1), filing_date=date(2026, 1, 2)),
        _ceo_buy(accession_no="acc-2", transaction_date=date(2026, 3, 1), filing_date=date(2026, 3, 2)),
    ]
    signals = csuite_conviction_signals(filings_frame(rows), cooldown_days=180)
    assert len(signals) == 1
    assert signals.iloc[0]["signal_date"] == date(2026, 1, 1)


def test_cooldown_exact_boundary_still_blocks():
    first = date(2026, 1, 1)
    second = first + timedelta(days=180)
    rows = [
        _ceo_buy(accession_no="acc-1", transaction_date=first, filing_date=first + timedelta(days=1)),
        _ceo_buy(accession_no="acc-2", transaction_date=second, filing_date=second + timedelta(days=1)),
    ]
    signals = csuite_conviction_signals(filings_frame(rows), cooldown_days=180)
    assert len(signals) == 1


def test_cooldown_one_day_after_window_emits_again():
    first = date(2026, 1, 1)
    second = first + timedelta(days=181)
    rows = [
        _ceo_buy(accession_no="acc-1", transaction_date=first, filing_date=first + timedelta(days=1)),
        _ceo_buy(accession_no="acc-2", transaction_date=second, filing_date=second + timedelta(days=1)),
    ]
    signals = csuite_conviction_signals(filings_frame(rows), cooldown_days=180)
    assert len(signals) == 2


def test_different_tickers_have_independent_cooldown():
    rows = [
        _ceo_buy(accession_no="acc-1", ticker="ACME", transaction_date=date(2026, 1, 1), filing_date=date(2026, 1, 2)),
        _ceo_buy(accession_no="acc-2", ticker="BETA", transaction_date=date(2026, 1, 1), filing_date=date(2026, 1, 2)),
    ]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert len(signals) == 2


def test_different_insiders_same_ticker_have_independent_cooldown():
    rows = [
        _ceo_buy(accession_no="acc-1", insider_cik="ceo-1", insider_name="A", transaction_date=date(2026, 1, 1), filing_date=date(2026, 1, 2)),
        _ceo_buy(accession_no="acc-2", insider_cik="cfo-1", insider_name="B", insider_title="CFO", transaction_date=date(2026, 1, 5), filing_date=date(2026, 1, 6)),
    ]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert len(signals) == 2


def test_10b5_1_plan_excluded():
    rows = [_ceo_buy(is_10b5_1=True)]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert signals.empty


def test_non_p_code_excluded():
    rows = [_ceo_buy(transaction_code="S")]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert signals.empty


def test_penny_stock_excluded():
    rows = [_ceo_buy(price_per_share=Decimal("4.99"), shares=Decimal("30000"), value_usd=Decimal("149700"))]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert signals.empty


def test_stale_filing_excluded():
    rows = [_ceo_buy(transaction_date=date(2026, 3, 1), filing_date=date(2026, 4, 1))]
    signals = csuite_conviction_signals(filings_frame(rows))
    assert signals.empty


def test_strength_increases_with_value():
    rows = [
        _ceo_buy(accession_no="acc-low", ticker="ACME", insider_cik="cik-low", value_usd=Decimal("150000")),
        _ceo_buy(accession_no="acc-hi", ticker="BETA", insider_cik="cik-hi", value_usd=Decimal("5000000")),
    ]
    signals = csuite_conviction_signals(filings_frame(rows)).set_index("ticker")
    assert signals.loc["BETA", "strength"] > signals.loc["ACME", "strength"]


def test_empty_input_returns_empty_frame():
    signals = csuite_conviction_signals(filings_frame([]))
    assert signals.empty
    assert list(signals.columns) == ["ticker", "signal_date", "strategy", "strength", "metadata"]
