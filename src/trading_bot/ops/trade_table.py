"""Unicode box-drawing table for trade output.

Used by both the backtest CLI (post-run summary) and the paper session loop
(end-of-session summary + live event lines). Pure formatting — no I/O.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..contracts import Trade

_EXIT_REASON_LABELS = {
    "stop": "stop",
    "take": "take-profit",
    "signal_flip": "signal flip",
    "session_end": "session end",
    "time": "time stop",
    "flatten": "flatten",
}

_HEADERS = (
    "#",
    "Date",
    "Side",
    "Entry (UTC)",
    "Entry $",
    "Exit (UTC)",
    "Exit $",
    "Qty",
    "P&L",
    "Why exited",
)


def _fmt_pnl(pnl: Decimal) -> str:
    """Currency P&L with Unicode minus sign so width stays aligned vs '+'."""
    val = float(pnl)
    sign = "+" if val >= 0 else "−"
    return f"{sign}{abs(val):.2f}"


def _fmt_qty(qty: Decimal) -> str:
    """Whole-share quantities render as integers; fractional left as Decimal."""
    if qty == qty.to_integral_value():
        return str(int(qty))
    return format(qty.normalize(), "f")


def _fmt_date(ts) -> str:
    # %-d (no leading zero) isn't portable; emulate manually.
    return ts.strftime("%b ") + str(ts.day)


def _fmt_time(ts) -> str:
    return ts.strftime("%H:%M")


def _row_for_trade(i: int, t: Trade) -> list[str]:
    return [
        str(i),
        _fmt_date(t.entry_time),
        t.side,
        _fmt_time(t.entry_time),
        f"{float(t.entry_price):.2f}",
        _fmt_time(t.exit_time),
        f"{float(t.exit_price):.2f}",
        _fmt_qty(t.qty),
        _fmt_pnl(t.pnl),
        _EXIT_REASON_LABELS.get(t.exit_reason, t.exit_reason),
    ]


def render_trade_table(trades: Sequence[Trade]) -> str:
    """Render a Unicode box table of trades. Empty input → '(no trades)'."""
    if not trades:
        return "(no trades)"

    headers = list(_HEADERS)
    body = [_row_for_trade(i, t) for i, t in enumerate(trades, start=1)]

    widths = [len(h) for h in headers]
    for row in body:
        for j, cell in enumerate(row):
            if len(cell) > widths[j]:
                widths[j] = len(cell)

    def hbar(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def render_row(row: Sequence[str], center: bool) -> str:
        out = []
        for cell, w in zip(row, widths):
            out.append(f" {cell:^{w}} " if center else f" {cell:<{w}} ")
        return "│" + "│".join(out) + "│"

    lines = [
        hbar("┌", "┬", "┐"),
        render_row(headers, center=True),
        hbar("├", "┼", "┤"),
    ]
    for i, row in enumerate(body):
        lines.append(render_row(row, center=False))
        if i < len(body) - 1:
            lines.append(hbar("├", "┼", "┤"))
    lines.append(hbar("└", "┴", "┘"))
    return "\n".join(lines)


def render_summary_line(trades: Sequence[Trade]) -> str:
    """One-line P&L + win-rate summary suitable below the table."""
    if not trades:
        return "Total: 0 trades"
    total_pnl = sum((t.pnl for t in trades), Decimal("0"))
    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = (wins / len(trades)) * 100
    return (
        f"Total: {len(trades)} trades  "
        f"net P&L {_fmt_pnl(total_pnl)}  "
        f"win-rate {win_rate:.1f}%"
    )


def render_trade_event(
    *,
    event: str,
    symbol: str,
    side: str,
    qty: Decimal,
    price: Decimal,
    timestamp,
    stop_price: Decimal | None = None,
    take_price: Decimal | None = None,
    pnl: Decimal | None = None,
    reason: str | None = None,
) -> str:
    """One-line trade event for live console output.

    Shapes:
      [HH:MM:SS] ENTRY  long  SPY  13 @ 735.26  stop=730.10 take=745.58
      [HH:MM:SS] EXIT   long  SPY  13 @ 734.69  pnl=−7.53   reason=signal flip
    """
    ts = timestamp.astimezone(timestamp.tzinfo).strftime("%H:%M:%S")
    head = f"[{ts}] {event:<6} {side:<5} {symbol:<5} {_fmt_qty(qty)} @ {float(price):.2f}"
    tail_parts: list[str] = []
    if stop_price is not None:
        tail_parts.append(f"stop={float(stop_price):.2f}")
    if take_price is not None:
        tail_parts.append(f"take={float(take_price):.2f}")
    if pnl is not None:
        tail_parts.append(f"pnl={_fmt_pnl(pnl)}")
    if reason is not None:
        label = _EXIT_REASON_LABELS.get(reason, reason)
        tail_parts.append(f"reason={label}")
    return head + ("  " + "  ".join(tail_parts) if tail_parts else "")
