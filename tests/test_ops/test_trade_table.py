from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd

from trading_bot.contracts import Trade
from trading_bot.ops.trade_table import (
    render_summary_line,
    render_trade_event,
    render_trade_table,
)


def _make_trade(
    *,
    side="long",
    entry_hour=14,
    entry_minute=40,
    exit_hour=15,
    exit_minute=26,
    entry_price="735.26",
    exit_price="734.69",
    qty="13",
    pnl="-7.53",
    exit_reason="stop",
) -> Trade:
    return Trade(
        symbol="SPY",
        side=side,
        entry_time=pd.Timestamp(datetime(2026, 5, 7, entry_hour, entry_minute, tzinfo=UTC)),
        exit_time=pd.Timestamp(datetime(2026, 5, 7, exit_hour, exit_minute, tzinfo=UTC)),
        entry_price=Decimal(entry_price),
        exit_price=Decimal(exit_price),
        qty=Decimal(qty),
        pnl=Decimal(pnl),
        exit_reason=exit_reason,
    )


def test_empty_table():
    assert render_trade_table([]) == "(no trades)"


def test_single_trade_layout():
    out = render_trade_table([_make_trade()])
    # Box-drawing corners present
    assert out.startswith("┌")
    assert out.endswith("┘")
    # Headers and data both rendered
    assert "Entry (UTC)" in out
    assert "14:40" in out
    assert "735.26" in out
    assert "−7.53" in out  # Unicode minus
    assert "stop" in out


def test_multi_trade_renders_all_rows():
    trades = [
        _make_trade(side="long"),
        _make_trade(side="short", pnl="11.55", exit_reason="take"),
        _make_trade(side="long", pnl="-9.14", exit_reason="stop"),
    ]
    out = render_trade_table(trades)
    # One header row + 3 body rows + separators
    assert out.count("│ 1 │") == 1
    assert out.count("│ 2 │") == 1
    assert out.count("│ 3 │") == 1
    assert "take-profit" in out  # label mapped from "take"
    assert "+11.55" in out
    assert "−9.14" in out


def test_summary_line_includes_total_and_winrate():
    trades = [
        _make_trade(pnl="11.55"),
        _make_trade(pnl="-7.53"),
        _make_trade(pnl="-9.14"),
    ]
    s = render_summary_line(trades)
    assert "3 trades" in s
    # 11.55 - 7.53 - 9.14 = -5.12
    assert "−5.12" in s
    # 1/3 wins ≈ 33.3
    assert "33.3" in s


def test_summary_line_empty():
    assert render_summary_line([]) == "Total: 0 trades"


def test_render_trade_event_entry():
    line = render_trade_event(
        event="ENTRY",
        symbol="SPY",
        side="long",
        qty=Decimal("13"),
        price=Decimal("735.26"),
        timestamp=pd.Timestamp(datetime(2026, 5, 7, 14, 40, 0, tzinfo=UTC)),
        stop_price=Decimal("730.10"),
        take_price=Decimal("745.58"),
    )
    assert "ENTRY" in line
    assert "SPY" in line
    assert "long" in line
    assert "735.26" in line
    assert "stop=730.10" in line
    assert "take=745.58" in line


def test_render_trade_event_exit():
    line = render_trade_event(
        event="EXIT",
        symbol="SPY",
        side="long",
        qty=Decimal("13"),
        price=Decimal("734.69"),
        timestamp=pd.Timestamp(datetime(2026, 5, 7, 15, 26, 0, tzinfo=UTC)),
        pnl=Decimal("-7.53"),
        reason="stop",
    )
    assert "EXIT" in line
    assert "pnl=−7.53" in line
    assert "reason=stop" in line
