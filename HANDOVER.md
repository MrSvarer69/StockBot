# Handover — trading-bot project

> Snapshot for the next Claude/human session. `CLAUDE.md` has the durable
> project rules; this file captures conversation state and decisions that
> aren't in the codebase.

---

## Where we left off (2026-06-02 — overnight holding is now the paper-bot DEFAULT; risk-approved, uncommitted)

User confirmed they tested the overnight stock hold on Alpaca paper and it worked, then asked to make overnight holding the default for ORB and the regular intraday bot. Implemented across 5 files, full suite green (**409 passed, 2 deselected**), risk-officer **APPROVED** (APPROVE-WITH-NITS). Changes are **uncommitted** in the working tree on branch `feature/overnight-holding` (base `d857230`). No git ops performed — awaiting per-action sign-off per rule #5.

### What the feature was before

Overnight holding already existed but was opt-in (default off), gated behind two independent switches: the session-wide `--protect-overnight` flag (default `False`) AND each strategy's `flat_by_et` config (default `"15:55"`, which liquidates before the close). Both had to be flipped for a position to actually carry. The broker side (GTC bracket survives the close, carried-position records, adoption on restart, HELD-stop replace) was already built and tested in the three commits leading up to `d857230`.

### What changed this session

| File | Change |
|---|---|
| `scripts/run_paper.py` | `--protect-overnight` → `argparse.BooleanOptionalAction`, `default=True`; adds `--no-protect-overnight` opt-out. When on, runner does `replace(cfg, flat_by_et=None)` for **orb + pullback** so the intraday EOD flat is dropped and positions carry under the GTC bracket. Added startup INFO log (on/off). Added comment at insider wiring noting the coupling is intentionally orb/pullback-only. New `from dataclasses import replace`. |
| `src/trading_bot/strategy/orb/config.yaml` | **Comment only.** `flat_by_et` value deliberately left `"15:55"`. |
| `src/trading_bot/strategy/pullback/config.yaml` | **Comment only.** `flat_by_et` value deliberately left `"15:55"`. |
| `src/trading_bot/execution/session.py` | **Comment only** on the `protect_overnight` field doc. Dataclass default stays `False`. |
| `README.md` | Reframed the "Overnight holding" section from "experimental, opt-in" to "default on"; documented `--no-protect-overnight`. |

### The load-bearing design decision (read before editing further)

`flat_by_et` lives in the **shared** `config.yaml`, which **backtests and the test suite also load**. Setting `flat_by_et: null` there would silently change backtest semantics (positions carrying across days, which the engine isn't set up to model honestly) and break several default-config tests (`test_orb.py:41,197`, `test_pullback.py:136` all assert the 15:55 flat fires by default). So instead:

- `config.yaml` keeps `flat_by_et: "15:55"` → the **backtest / `--no-protect-overnight`** value. Backtests stay intraday and deterministic.
- The **live runner** forces it to `None` via `replace(...)` only when `protect_overnight` is on (now the default). The CLI is the **only** default-on surface.
- `SessionConfig.protect_overnight` dataclass default stays `False` → all programmatic / backtest / test callers remain flatten-safe.

**Consequence to remember:** editing `flat_by_et` in `config.yaml` does NOT change the live default — only the backtest/opt-out path. The live value is forced in `run_paper.py`.

### Per-strategy behavior under the new default

- **orb / pullback:** EOD flat dropped, position carries under its GTC stop/take bracket.
- **insider:** already carried across sessions via `holding_days` (no `flat_by_et`, so no override). The flag flip changes its bracket from DAY → GTC. Risk-officer flagged that insider was previously carrying **unprotected** (the DAY bracket expired at 16:00 ET while the position rolled), so this is **strictly safer, not a regression** — worth noting as a latent bug this change closes.

### Risk-officer verdict — APPROVE-WITH-NITS

Confirmed: no window where a position is held overnight without a GTC bracket (entry + both legs submit atomically as one GTC bracket via `OrderClass.BRACKET`, tif flows through `alpaca_paper.py:251,265`); `SessionConfig.protect_overnight` default still `False`; risk gauntlet (`validate_order`) is tif-agnostic so GTC passes the same checks; carried risk correctly stays counted against `max_position_count`/`max_capital_usd` into the next session; daily-loss baseline rebases per ET day; `TRADING_MODE` gate and risk routing untouched.

Nits:
1. *(optional, NOT done)* Surface the effective live `flat_by_et` in the startup log. Judged sufficient — the existing log line already states "intraday EOD flat dropped for orb/pullback".
2. *(DONE)* Added the insider-wiring comment.

### Caveats to carry forward

- **Trailing stop is frozen overnight.** The ratchet only runs while the bot is live; the GTC bracket still enforces the last stop level, but it won't tighten until the bot is back up. Already noted in `pullback/config.yaml`.
- **Paper-only.** Nothing here touches the live-trading gate.
- If GTC brackets are ever rejected on a new account (probe check #1 fails), run with `--no-protect-overnight` until sorted.

### Verification

- Full suite: `uv run --with pytest python -m pytest -q` → **409 passed, 2 deselected, 1 warning**. (Note: `pytest` must be invoked via `uv run --with pytest python -m pytest`; bare `pytest`/`python -m pytest` fail — not on PATH / not synced into the venv.)
- `--help` shows the `--protect-overnight | --no-protect-overnight` pair with the default-on help text.

### Suggested first move next session

> "Commit the 5-file working-tree change on `feature/overnight-holding` (needs user sign-off), then push and open a PR against `main` (HTTPS remote `https://github.com/MrSvarer69/StockBot.git`). Optional follow-ups: risk-officer nit #1 (log effective `flat_by_et`), and a live paper round-trip — stop with a position open, confirm a `CARRIED_*.json` lands in `data/ops/carried/`, relaunch, confirm the 'adopting carried overnight positions' log line."

### Note on the older `$435 deployment` thread below

Unrelated to this session and still open (see the 2026-05-23 sections). This session did not touch sizing, the universe, or the audit-log flake.

---

## Where we left off (2026-05-23, late evening — cheap-universe screen falsifies the $435 deployment hypothesis; one diagnostic still owed before parking)

This session attempted to answer the open `$435 deployment` problem from the previous (same-day, evening) session by hunting for a cheaper universe. Conclusion: at the existing acceptance bar, no cheap-deployable symbol clears it on either ORB or pullback at honest slippage. No code changes shipped; no tests written; the work was screening-only. The next session has a single small diagnostic to run, then a clean fork: deploy a single name or park the `$435` question entirely.

### Scope and method

User picked Tier A + Tier B (17 cheap candidates) and authorized raising `max_pct_per_trade` to 10% (≈$43.50/trade ceiling at $435 equity). Backfilled 1-min bars for all 17 over 2025-11-15 → 2026-05-13 (the same 6-month window used in prior screens, so results compare apples-to-apples). All 17 fetched cleanly via Alpaca IEX feed; no missing symbols, no rate-limit issues.

Tier A (12 liquid US-listed common / ADR): F, SOFI, PLUG, LCID, NIO, RIVN, SIRI, WBD, AAL, PBR, KGC, HL.
Tier B (5 crypto-miner small caps): BITF, CIFR, HUT, CLSK, IREN.

Also re-screened four already-cached deployable mid-tier names for comparison: RIOT, SMCI, RBLX, MARA.

Slippage rationale documented inline: tick size $0.01 = ~33bp on a $3 stock; the existing 3bp stress is too generous for cheap names. Settled on **30bp as the honest stress for cheap stocks**, with a note that 10bp is more appropriate for the $20–$50 mid-tier where 1 tick is ~2–5bp.

### Results — ORB

Full-window at 30bp (PF > 1.0):

| symbol | trades | PF | total_return% | max_dd% | last px |
|---|---|---|---|---|---|
| HUT | 40 | 1.62 | +3.6 | -2.5 | $108.33 |
| F | 56 | 1.34 | +0.8 | -1.1 | $13.59 |
| SOFI | 41 | 1.24 | +0.8 | -1.0 | $15.30 |
| SIRI | 43 | 1.13 | +0.4 | -0.9 | $26.36 |
| IREN | 45 | 1.07 | +0.5 | -2.8 | $55.28 |
| KGC | 54 | 1.01 | +0.0 | -1.6 | $31.28 |

IS half (2025-11-15 → 2026-02-14) and OOS half (2026-02-15 → 2026-05-13), 30bp, strict gate is PF > 1.0 in BOTH halves with ≥10 trades/half:

| symbol | IS PF | OOS PF | strict pass? | price | fits $43.50? |
|---|---|---|---|---|---|
| HUT | 1.49 | 1.06 | ✓ | $108 | ❌ |
| **SMCI** | **2.40** | **0.98** | ❌ (OOS misses by 0.02) | $32 | ✓ |
| RBLX | 1.32 | 0.68 | ❌ (flip) | $42 | ✓ |
| F | 0.60 | 2.71 | ❌ (flip) | $14 | ✓ |
| SOFI | 1.37 | 0.79 | ❌ (flip) | $15 | ✓ |
| SIRI | 0.84 | 1.36 | ❌ (flip) | $26 | ✓ |
| LCID | 1.73 | 0.57 | ❌ (flip) | n/a (in cheap-only set) | ✓ |
| KGC | 1.52 | 0.59 | ❌ (flip) | $31 | ✓ |
| IREN | 0.65 | 1.60 | ❌ (flip) | $55 | ❌ |
| RIOT | 0.91 | 0.75 | ❌ | $25 | ✓ |
| MARA | 0.91 | 0.60 | ❌ (justifies existing exclusion) | $13 | ✓ |
| HL | 1.14 | 0.58 | ❌ (flip) | n/a | ✓ |
| Everything else | <0.85 either half | | ❌ | | |

**Two key surprises:**

1. **HUT looks like the gate winner but it's currently $108/share.** At 10% × $435 = $43.50 budget, you can't buy a single share. The only strict-gate pass is undeployable.
2. **SMCI is the closest deployable near-miss** (IS 2.40, OOS 0.98 — misses by 0.02). It's also already a 2026-05-14 validated 7-name-screen survivor at the older bar. Worth flagging as the single defensible candidate IF the OOS rounding is acceptable.

Regime fragility is the dominant story: IS half (Nov-Feb) and OOS half (Feb-May) systematically disagree. Almost every cheap name passes one half and fails the other.

### Results — pullback

`run_pullback_backtest.py` with `--slippage-bps 1.0 --stress-slippage-bps 30.0` over the same 6-month window on 21 names (17 cheap + RIOT, SMCI, RBLX, MARA): **survivors at strict gate = 0**.

At 30bp stress, every cheap candidate's pullback PF dropped to 0.00–0.51. The strategy's per-trade R is too small to absorb realistic slippage at sub-$15 prices. **Pullback is structurally unviable on cheap stocks at honest slippage.** Best of the worst at 30bp: HUT (IS 0.51, OOS 0.30) — still well below 1.0, and HUT is undeployable anyway.

At 1bp slippage (i.e., ignoring the cheap-stock microstructure problem), a handful of names show PF > 1.0 in one half (HUT, IREN, LCID, WBD, SIRI, MARA, CIFR) but the strict IS+OOS gate still picks zero survivors — closest is HL at 0.99/1.10.

### What this means for the $435 question

Empirical answer: **the cheap-universe hypothesis is falsified for the current strategies at honest slippage assumptions.** The combination of (a) higher slippage on cheap stocks, (b) strategies whose edge depends on tight slippage, (c) recent regime change between the two halves, leaves no clean deployment.

Of the prior session's "realistic floor for current defaults is $5k–$10k" — this session corroborates that. SMCI at $32 is the only realistic single-name candidate, and its OOS slip just under the strict bar.

### The one diagnostic still owed before parking

I proposed and the user did NOT yet approve a final 10bp ORB IS/OOS screen on the deployable mid-tier (SMCI, RBLX, RIOT, F, SOFI, SIRI). Rationale: 30bp is the honest stress for sub-$10 names, but 1 tick on SMCI at $32 is only ~3bp, so 30bp = ~9 ticks of slippage is unrealistically punishing for SMCI specifically. **10bp is the price-appropriate stress for the $20–$50 mid-tier.** A cache-hit, sub-minute run.

Outcomes:
- If **SMCI clears strict IS/OOS at 10bp** → single-symbol SMCI deployment becomes defensible. Propose updating `_WIDE_UNIVERSE_DEFAULT` to `"SMCI"` and the `--max-pct-per-trade` default to 10% in `scripts/run_paper.py`, gated on user approval.
- If **nothing clears at 10bp** → park the $435 question with high confidence. Treat the bot as paper-only learning until capital permits. Pick up the audit-log flake or a different task next.

### Recommendation given to the user (still pending decision)

Option 4 — park the $435 deployment question — was the recommendation, with the 10bp diagnostic as the one cheap check before committing to that. Reasoning:
- Project's stated purpose (CLAUDE.md) is learning agents, not generating returns ("Profit from this is NOT a priority, only a nice thing.")
- Pullback is structurally dead on cheap stocks → 1 of 3 strategies gone
- Insider also fails this universe (Form 4 clusters live on $20–$100 regional banks, not $3 EVs)
- ORB alone on a single near-miss name in just one regime half is a thin base
- The realistic floor for the current strategy defaults is $5k–$10k — pushing 10–20× below that scale is the source of the problem, not the universe

User responded "What would you recommend the next step being?" → my response was "the 10bp diagnostic on SMCI specifically, then a clean fork." They then said "swite the handover" (this section). **No final decision was made** on either running the diagnostic or parking the question.

### Carry-forwards from the prior 2026-05-23 evening session (unchanged)

These were open before this session and remain open:

- **Pre-existing audit-log flake** (`test_filter_audit_log_emits_or_atr_and_cold_start`) — still the only failing test in the suite. Same shape (test caplog asks for INFO, the strategy emits at DEBUG). ~15 min for the strategist agent. Suggested follow-up if the $435 question is parked.
- **`broker-integration` agent** at `.claude/agents/broker-integration.md` — only loads at session start, so it would be available next session for any future broker work.
- **Capital cap (`max_capital_usd`)** is wired through risk sizing, kill-switch, and validation as of the prior session. Defaults to `None` (no cap). The `--max-capital-usd $435` flag is the deployment knob.
- **Pre-existing risk-officer concerns** carried from earlier sessions (still open):
  - `SessionConfig.flatten_on_exit` default flipped to `False` — needs justification or revert.
  - Reconciliation step at session start for stale `sess.open_entries` rows when a bracket child fires while the bot was offline.
  - Persist `open_entries` to disk so bot-side stop/take enforcement survives a restart.
  - Gate `--no-flatten-on-halt` behind a `TRADING_MODE=live` refusal whenever live trading is enabled.

### Suggested first move next session

> "Either: (a) run the 10bp ORB IS/OOS screen on SMCI + RBLX + RIOT + F + SOFI + SIRI and decide between single-symbol SMCI deployment vs parking; or (b) skip directly to parking the $435 question and pick up the audit-log flake as the small follow-up. Both paths start with the user confirming which fork to take."

### Data trail (for replay)

Screen reports written this session, all under `data/backtests/`:

- `screen-20260523T165850Z/` — ORB 1bp full-window, 17 cheap names
- `screen-20260523T170058Z/` — ORB 30bp full-window, 17 cheap names
- `screen-20260523T170100Z/` — ORB 30bp IS half, 17 cheap names
- `screen-20260523T170102Z/` — ORB 30bp OOS half, 17 cheap names
- `screen-20260523T170313Z/` — ORB 30bp IS half, 7 deployable mid-tier
- `screen-20260523T170315Z/` — ORB 30bp OOS half, 7 deployable mid-tier
- `pullback-20260523T170337Z/` — pullback IS/OOS at 30bp on 21 names, zero survivors

### Hard rules still in force (unchanged from prior sessions)

- Paper-only default.
- No forecasts in outputs — this section reports only computed-from-history results.
- Pure functions in `strategy/`.
- Every order placement goes through `trading_bot.risk`.
- Git: rule #5 now permits commits/pushes with explicit per-action operator confirmation; remote is HTTPS-only at `https://github.com/MrSvarer69/StockBot.git`. Claude does NOT push without asking.

---

## Where we left off (2026-05-23, evening — 5-item plan complete + capital cap added; $435 deployment shape is the open problem)

This session implemented a 5-item user-defined plan plus a capital cap for student-scale deployment. All work merged into the working tree; no git operations performed (user handles git). Full suite: **363 passed, 1 failed** (still the pre-existing `test_filter_audit_log_emits_or_atr_and_cold_start` audit-log flake — verified pre-existing on `main` from prior sessions).

### The 5-item plan, implemented in order 1 → 5 → 3 → 2 → 4

#### 1. Insider holds overnight (`strategy/insider/`)

Insider was flattening same-day because `_resolve_flat_timestamp` (`strategy.py:420-457`) clamped its target date down to the last available bar when bars didn't extend `holding_days` trading days past entry. In a live paper session bars only cover today, so the clamp always landed on today's 15:55 ET bar.

Fix: when `holding_days >= len(target_dates)`, return `None` (don't emit a flat). The position carries across the session boundary; the broker-side stop bracket is the overnight safety net. Updated the docstring and the misleading comment in `insider/config.yaml:24-32` (which claimed the session loop closes everything at flat_by_et — wrong, since `flatten_on_exit=False` is the default).

Tests: `test_flat_horizon_respects_bars_window_coverage` with 4 parametrized variants (carry-over / partial / exact-fit / over-fit).

#### 5. Bot name on every trade log

Threaded a `strategy: str` column (`"orb" | "pullback" | "insider" | ""`) end-to-end:

- `empty_signals_frame()` (`strategy/base.py:16`) and every strategy's row builder (ORB and pullback both populate via the shared `_row` helper; insider builds row dicts inline so two sites were updated).
- `strategy/composite.py` column projection preserved.
- `Trade.strategy` field (`contracts.py:119`, default empty string for back-compat with old fixtures).
- `session.py:_handle_entry_signal` → `_record_entry` → `open_entries[symbol]["strategy"]` → `_record_exit` → `Trade(...)` AND both `logger.info("trade entry"/"trade exit", extra={"strategy": ...})` calls. NaN-safe via the same `pd.isna` guard the backtest engine uses.
- `backtest/engine.py` — `open_strategy` state tracked alongside the existing open-position state; both `Trade(...)` construction sites populate.
- `ops/trade_table.py` — `render_trade_event` prefixes with `[orb]`/`[pullback]`/`[insider]` (slot width 10 matches `[pullback]`); `render_trade_table` has a new "Bot" column.

Tests: `test_strategy_attribution_round_trip` (4 parametrize variants) in `tests/test_execution/test_strategy_attribution.py`. 8 composite-test fixtures updated to populate the new column.

Risk-officer reviewed the `session.py` change: approved, no new order-placement paths, live-trading gate untouched.

#### 3. ORB pre-OR proxy via prior close + ATR (**default OFF — backtest showed it hurt edge**)

Added a pre-OR proxy path so ORB can fire entries during 09:30-10:00 ET instead of waiting for the actual opening range to complete. Uses `prior_close ± k × prior_session_ATR` as a proxy range; after 10:00 ET, the existing OR-breakout logic takes over unchanged.

Implementation in `strategy/orb/strategy.py`:
- New `prior_session_closes: deque[float]` parallel to the existing `prior_session_ranges` deque (lines 134-138, append sites mirrored at 178-179, 226-227, ~298-299).
- Pre-OR proxy block (lines 207-216) sits between the partial-session continue and the OR-window scan.
- `long_done`/`short_done` flags moved BEFORE the proxy block so the OR-breakout loop later in the same session honors them and doesn't double-fire.
- Score normalized to `2 × |close − prior_close| / proxy_atr` — same axis as OR-breakout's `or_range / session_ATR`, which fixed a picker regression where proxy scores were ranked against OR scores on incomparable scales.
- Config: `use_prior_close_proxy: bool = False`, `pre_or_k: float = 0.5`.

**A/B backtest** (2025-12-01 → 2026-02-28, SPY/NVDA/AAPL/JPM):

| metric | Proxy ON | Proxy OFF |
|---|---|---|
| trades | 157 | 101 |
| win rate | 49.0% | 56.4% |
| profit factor | 1.23 | **1.58** |
| gross PnL | $1,549.66 | **$1,805.41** |
| max drawdown | -0.28% | -0.22% |

Proxy fires more trades (125 in the 09:30-10:00 window, ~80% of total) but earns less per trade. Default flipped to **off** based on this evidence; feature ships as opt-in. If the user wants to revisit, try a higher `pre_or_k` (e.g. 1.5 or 2.0) on a different universe.

Tests: 4 proxy tests in `test_orb.py`. `test_min_range_filter_uses_session_scale_atr` was updated to explicitly pass `use_prior_close_proxy=False` so the OR-only behavior it tests stays isolated.

#### 2. Pullback warmup pre-loaded from prior session

Today the pullback strategy can't fire before ~12:50 ET because EMA-slow=200 needs 200 minutes of warmup. To enable 09:30 entries we pre-load with prior-session bars at session start. CLAUDE.md's "pure functions in `strategy/`" rule means the strategy can't do I/O itself — the runner pre-fetches and injects.

Built:
- `src/trading_bot/data/sessions.py` (NEW): `load_prior_session_bars(symbol, today, min_bars=200)` walks the per-UTC-date parquet cache backwards from `today - 1 day`, accumulating sessions until `min_bars` rows or the 10-calendar-day cap. Read-only, never raises, returns empty frame on cache miss. Re-exported from `trading_bot.data`.
- `PullbackStrategy.set_prior_session_bars(by_symbol: Mapping[str, pd.DataFrame])` setter; internal `dict[str, pd.DataFrame]` state.
- `_scan_session` now takes `entry_date` and gates entries/flats by that date — pre-loaded bars feed the EMA recursion but are NOT eligible to produce signals (date safety gate).
- `_prepend_prior` helper concatenates prior bars per-session, sort + drop-duplicate-index.
- `PullbackConfig.warmup_from_prior_session: bool = True`.
- `scripts/run_paper.py:261-275` builds the per-symbol prior-bars dict at startup and calls the setter; gated on the config flag.

Tests: 4 in `tests/test_strategy/pullback/test_pullback.py` covering entry-at-09:30 with warmup, prior-bars-never-emit-entries (date gate), fallback-to-today-only when no prior data, and flag-disabled-no-op.

#### 4. Dynamic trailing stop (fixed-dollar ratchet, broker-side stop kept in sync)

The big one. User example: buy at $100 with stop $80; if price rises to $105, stop ratchets to $85 (`offset = entry - initial_stop = $20`; new stop = `max(prior_stop, highest_price - offset)`).

New files:
- `src/trading_bot/risk/trailing.py` — pure math: `trail_offset`, `ratchet_long_stop`, `ratchet_short_stop`, `ratchet_stop` (dispatcher). Re-exported from `risk/__init__.py`.
- `.claude/agents/broker-integration.md` — new specialist agent definition created during this work item (user explicitly authorized). **The Claude Code harness only loads agent definitions at session start, so this agent is available NEXT session, not the one it was created in.** Work item 4's broker integration was therefore done directly by Claude instead of via the new specialist.

Protocol + Alpaca adapter:
- `BrokerClient.replace_stop_price(symbol, new_stop_price) -> bool` added to `execution/broker.py`.
- `AlpacaPaperBroker.replace_stop_price` (`execution/alpaca_paper.py`) — uses Alpaca's native `replace_order_by_id` (PATCH endpoint) so the position is **never briefly unprotected** by a cancel+place race. Quantizes via existing `_quantize_to_tick`. Idempotent no-op when existing == new. Returns `False` (no raise) when no stop leg found. Explicit family set `{"stop", "stop_loss", "stop_limit"}` for leg detection — deliberately excludes `"trailing_stop"` so a broker-side trailing leg cannot be silently overwritten by this bot-side ratchet.

Session integration (`execution/session.py`):
- `SessionConfig.trailing_stop_policy: Mapping[str, bool]` (default empty dict). `SessionState.trailing_stop_policy` mirror populated at session start in `run_session`.
- `_record_entry` stamps `trail_enabled`, `trail_offset`, `trail_extreme` on `open_entries[symbol]` when the producing strategy's policy allows AND `order.stop_price is not None`.
- `_check_stops_and_takes` ratchets BEFORE the trigger check: updates `trail_extreme` to max/min of latest price, calls `ratchet_stop`, on tightening calls `broker.replace_stop_price` and on success updates the local `stop_price` + emits a TRAIL event line via `render_trade_event` + structured `logger.info("trailing stop ratcheted")`. Auth/validation errors propagate (halt-class); transient `BrokerError` is logged and absorbed, prior stop preserved.

Per-strategy gate (each strategy's config now carries `enable_trailing_stop`):
- ORB: `True` (intraday momentum, ratcheting locks in gains as the breakout extends).
- Pullback: `True` (same rationale).
- Insider: `False` — multi-day swing with a deliberately wide 2.5×ATR stop; ratcheting would knock the position out during normal multi-day drawdowns the wide stop was sized to absorb.

`scripts/run_paper.py` builds the per-strategy `trailing_stop_policy` dict from each strategy's config at startup and passes to SessionConfig.

Backtest engine (`backtest/engine.py`) extended: new `trailing_stop_policy` parameter, parallel `open_trail_enabled` / `open_trail_offset` / `open_trail_extreme` state, ratchet applied per-bar against `bar_high` (long) / `bar_low` (short) as worst-case-within-bar extremes. Default `None` keeps existing backtest tests byte-identical. `scripts/compare_trailing.py` (new) is the A/B harness.

Risk-officer reviewed and approved with notes; notes addressed before tester ran (most notably the stop-leg detection was tightened from `startswith("stop")` to the explicit family set, so `"trailing_stop"` is correctly excluded).

Tests:
- `tests/test_risk/test_trailing.py` (NEW) — 20 tests for ratchet monotonicity (long/short), offset sign, no-op-on-equality, no-op-on-first-poll safety invariant, dispatcher.
- `tests/test_execution/test_alpaca_paper.py` — 16 new `replace_stop_price` cases including the family-detection parametrize (asserts `"trailing_stop"` is NOT picked up).
- `tests/test_execution/test_session_trailing.py` (NEW) — 15 session-level tests including the per-strategy gate (insider stays static even when policy contains it as `False`).

**A/B backtest** (same window as #3, trailing ON vs OFF):

| metric | Trail ON | Trail OFF |
|---|---|---|
| trades | 151 | 151 |
| win rate | 49.7% | 51.7% |
| profit factor | 1.55 | 1.54 |
| gross PnL | $1,776.03 | $1,805.99 |
| max drawdown | -0.22% | -0.22% |
| avg win | $66.59 | $65.69 |
| avg loss | -$42.34 | -$45.45 |

Trade counts identical (151/151) — the trail only tightened exit prices, didn't create new stop-outs. Aggregate: roughly a wash. Avg loss decreased modestly, avg win also decreased, gross PnL down $30. JPM was the one symbol where trailing meaningfully reduced the loss. Historical only — no forward-looking claim.

### Capital cap (`max_capital_usd`) — added after the 5-item plan

User asked for a hard cap because they're a student with at most ~3000 DKK to invest if this ever goes live.

- `RiskParams.max_capital_usd: Decimal | None = None` field (`contracts.py:62-69`).
- `effective_equity(equity, params)` helper in `risk/sizing.py`, re-exported from `trading_bot.risk`. Returns `min(equity, max_capital_usd)` when set, raw equity when not.
- `size_position`, `check_daily_loss`, and `validate_order` all route through `effective_equity` for their percentage-of-equity calculations.
- **Intentionally NOT capped:** `realized_pnl_today` in `session.py` — that's a real P&L delta; capping it would silently mask drawdowns. The risk-officer specifically asked this be documented; it now is in `kill_switch.py`.
- CLI: `--max-capital-usd` flag on `scripts/run_paper.py`. Help text notes the DKK→USD conversion (`3000 DKK ≈ $435 USD at ~6.9 DKK/USD`). Backtest scripts already build `RiskParams` directly, so the cap flows through without harness changes.
- Tests: 10 new across `test_sizing.py`, `test_kill_switch.py`, `test_validation.py`. Risk-officer approved with notes; all notes addressed.

### The actual problem now: $435 USD is too small for the current strategy defaults

Smoke-tested the cap end-to-end with `max_capital_usd=$435`. At that bankroll:

- `max_pct_per_trade=2%` → per-trade notional cap is **$8.70**.
- Most US equities in the bot's universe trade above $8.70/share, so `size_position` rounds qty down to **0 shares** for any reasonable entry. The bot would fire essentially no trades.
- `max_daily_loss_pct=3%` → daily-loss kill switch trips at **−$13.05** realized.

The current strategy defaults were tuned for ~$100k paper equity. At $435 the bot is functionally inert.

To make a $435 deployment meaningful the user needs **one or more of**:

1. **Cheaper symbols (under ~$10/share).** Current universe excludes most of these. The insider-track regional banks (CBC, WSBC, GABC, SFNC, FMBM, BWFG, CIVB) sit roughly in $20-$100; closer to viable but still won't fit at 2%-per-trade off $435.
2. **Higher `max_pct_per_trade`** (currently 2%; e.g. 10-25%) so a single trade can use a larger fraction of the tiny bankroll. **This is a real risk-profile change — backtest before flipping. A 25% per-trade allocation means one bad trade can knock 25% off the equity, which interacts with the daily-loss cap.**
3. **Higher `target_size_pct` per strategy** (currently 10% for ORB/pullback, 10% for insider). Less load-bearing than `max_pct_per_trade` here since the latter is the binding constraint.
4. **More capital before going live.** The realistic floor for the current strategy defaults is probably $5k–$10k USD; below that, per-trade sizing squeezes to zero on most symbols.

**Recommended diagnostic next session:** run a backtest with `max_capital_usd=$435` over the same Dec 2025 - Feb 2026 window and see how many trades actually fire and what the hypothetical PnL is. If it's ~zero trades, the user has a real architectural choice to make (raise `max_pct_per_trade`, narrow universe to cheap names, wait for more capital, or accept paper-only learning).

### Carry-forwards from this session

- **Pre-existing audit-log flake** (`test_filter_audit_log_emits_or_atr_and_cold_start`) is still the only failing test in the suite. Same shape as in the prior handover (test caplog asks for INFO, the strategy emits at DEBUG). Out of scope for the 5-item plan; not addressed this session. **Recommend picking it up as a small follow-up** — strategist would need ~15 minutes.
- **`broker-integration` agent** is on disk at `.claude/agents/broker-integration.md` but only loads on the next Claude Code restart. Future broker work (e.g. order-replace for take-profit legs, IBKR adapter, etc.) should use it then.
- **User memory updates this session:**
  - `feedback_new_function_workflow` — required 4-step process for every new function: subagent → reuse existing code → strip dead code → short comment.
  - `feedback_git_push_allowed` — push permitted only to `https://github.com/MrSvarer69/StockBot.git`; always confirm per-action.
  - `feedback_always_ask_before_changes` — per-action confirmation even when the user has previously granted broad authorization.
- **CLAUDE.md hard rule #5 updated by the user** to allow commits/pushes with confirmation (was previously "NO git commit or git push"). The HTTPS URL above is the only push target since the machine isn't logged into git.

### Suggested first move next session

> "Decide on the $435 deployment shape: (a) raise `max_pct_per_trade` to fit the small bankroll (and re-validate via backtest), (b) restrict the universe to cheap symbols, (c) wait for more capital before going live, or (d) accept the bot fires ~zero trades at this scale and use it purely for paper-only learning. Then either re-run the backtest with the chosen knobs or pick the audit-log flake as a small follow-up."

### Hard rules still in force (from CLAUDE.md, unchanged shape)

- Paper-only default.
- No forecasts in outputs (backtest results from real history are fine).
- Pure functions in `strategy/` — pullback's prior-bar state is set externally by the runner, preserving this rule.
- Every order placement goes through `trading_bot.risk` for sizing / validation; risk-officer reviews any `execution/` or `risk/` change. The new `effective_equity` and `ratchet_stop` helpers both live in `risk/` and are re-exported from the package.
- Bracket contract: every entry signal carries `stop_price` and `take_price`.
- Git: rule #5 now permits commits/pushes with explicit per-action operator confirmation; remote is HTTPS-only at `https://github.com/MrSvarer69/StockBot.git`. Claude does NOT push without asking.

---

## Where we left off (2026-05-20, late evening — insider live-refresh wired; user has set the target architecture as THREE separate bots)

This session diagnosed why today's paper-trading run did not place any trades, and built live-refresh for the insider data path so the same failure won't happen again. The user then declared the architectural target they want: **three separate, independently runnable bots — ORB, Insider, and a Midday trader.** The Midday trader does not exist yet and is the main outstanding build.

### The diagnosis from today's run

The user launched the bot twice (17:43 CEST and 20:26 CEST = 11:43 ET and 14:26 ET — both well after the 09:30 ET open) and saw only `skipping stale signal` warnings, no fills. Investigation confirmed:

1. **All "stale signal" warnings were ORB signals**, including `side: short` rows for GOOGL/BABA/TSLA/LLY/UNH that cannot have come from `InsiderStrategy` (long-only). Spread of timestamps (10:00–11:05 ET across symbols) is the natural ORB breakout-time spread. Correct freshness-gate behavior for an opening-only strategy launched midday.
2. **`InsiderStrategy` emitted zero signals.** `scripts/probe_insider_today.py` (new, kept) showed `signals with effective_entry_date == today: 0`. Root cause: the Form 4 parquet cache at `data/insider/` had a latest `filing_date` of **2026-05-15**, five days stale. `_next_trading_day(2026-05-15) = 2026-05-18`, so no signal in the cache had an `effective_entry_date >= today`.
3. **Alpaca IEX feed was healthy.** `scripts/probe_bars_freshness.py` (new, kept) showed last bars within ~1 minute of wall-clock for SPY/QQQ/NVDA/TSLA/AMZN/JPM/ARM/WSBC. The feed was never the problem.
4. The user originally claimed they launched at 15:30 CEST (= 09:30 ET, the open). Audit-log filenames disprove this — earliest log today is `20260520T154357Z.jsonl` (17:43 CEST). No log file from 13:30 UTC exists. Either an earlier launch failed silently before logging, or memory of the time is off. Worth confirming with shell history next session.

### What was built this session

**Live refresh for the Form 4 cache** — addresses the design gap that `InsiderStrategy` loaded parquet only at construction and never re-checked EDGAR. The user explicitly chose **startup pre-flight + 15-min in-session refresh** when asked.

- `src/trading_bot/data/insider/refresh.py` (new) — `refresh_cache_to_today(client, cache, ticker_map, lookback_days=7, today=None)` wraps `run_backfill` over a rolling 7-day window. Resumable, so repeated calls are cheap.
- `src/trading_bot/data/insider/__init__.py` — exports `refresh_cache_to_today`.
- `src/trading_bot/strategy/insider/strategy.py` — new `InsiderStrategy.reload()` method. Re-reads parquet, re-runs both detectors, atomically swaps in the new `_signals_by_entry_date`. Logs `n_signals_delta`. Returns the new total signal count.
- `src/trading_bot/execution/session.py` — `SessionConfig` gets `refresh_interval_minutes: int | None = None` and `refresh_hook: Callable[[], None] | None = None` (validated in `__post_init__` — non-callable hook or non-positive interval raises at construction). `SessionState` gets `last_refresh_at: datetime | None = None`. Main loop fires the hook at the top of each iteration (after the market-open check, before `get_account`) when the interval has elapsed; exceptions are absorbed and `last_refresh_at` is advanced on failure to prevent EDGAR-outage log-spam.
- `scripts/run_paper.py` — new flags `--insider-refresh-minutes` (default 15, 0 disables) and `--no-insider-preflight` (default: pre-flight is on). Pre-flight runs `refresh_cache_to_today` BEFORE constructing `InsiderStrategy`, so the in-memory index at session start reflects everything filed up to launch. In-session hook closes over the same `(client, cache, ticker_map)` and calls `refresh_cache_to_today` followed by `insider_strategy.reload()`.

**Important constraint to remember**: the anti-lookahead rule in `strategy/insider/strategy.py:21-25` means a filing published *during today's session* cannot trigger a same-day entry. It becomes tradable at tomorrow's 10:00 ET. The in-session refresh therefore primarily benefits *tomorrow's* signal set. The startup pre-flight is what fixes today's specific failure mode.

**Tests (all passing):**

- `tests/test_strategy/insider/test_strategy_adapter.py` — 2 new tests: `test_reload_picks_up_new_filings_written_to_parquet`, `test_reload_with_empty_parquet_root_clears_index`.
- `tests/test_execution/test_session.py` — 6 new tests: `test_refresh_hook_fires_on_first_iteration_when_due`, `test_refresh_hook_skipped_when_interval_not_elapsed`, `test_refresh_hook_failure_does_not_halt_session`, `test_refresh_hook_disabled_when_interval_none`, `test_refresh_hook_rejects_non_callable`, `test_refresh_interval_rejects_nonpositive`.
- Full `tests/test_execution/` + `tests/test_strategy/insider/` suites green (121 + 49 tests respectively). Pre-existing failure in `tests/test_strategy/test_orb.py::test_filter_audit_log_emits_or_atr_and_cold_start` is unchanged — fails the same way on unmodified `main`.

**Diagnostic scripts kept in `scripts/` (gitignored content unchanged, but the scripts themselves are new files):**

- `scripts/probe_bars_freshness.py` — prints the last-bar timestamp and row count from `AlpacaPaperBroker.get_recent_bars` for a handful of liquid names. Use to disambiguate "data feed is stale" from "strategy is buggy" when investigating a no-trade session.
- `scripts/probe_insider_today.py` — prints how many `InsiderStrategy` signals are dated for today's `effective_entry_date` plus the next 5 upcoming dates. Use to confirm whether the insider arm has anything to trade today before assuming a wiring bug.

### Risk-officer review on this session's change

Invoked at the end of the build. **Initial verdict: BLOCK** — but the block was driven by pre-existing uncommitted changes already in the working directory before this conversation started, not by anything built this session. The blockers the risk-officer flagged that are NOT mine:

1. **`SessionConfig.flatten_on_exit` default flipped `True → False`** at `session.py:101`. Travels in the diff because the 2026-05-19 stop/take work touched it. Test fixtures at `tests/test_execution/test_session.py:43` already pass `flatten_on_exit=False` explicitly, masking the default flip in the suite. Risk-officer correctly notes this is a safety-default regression that needs explicit justification — flagged in the 2026-05-19 handover section below but the change still sits in the diff uncommitted.
2. **`_check_stops_and_takes`** at `session.py:734-811`. Pre-existing from 2026-05-19 work. Already risk-officer-reviewed in that session.
3. **`--no-flatten` → `--flatten-on-exit`/`--no-flatten-on-halt`** CLI rename in `run_paper.py`. Pre-existing from 2026-05-19. Operator runbooks referencing the old flag would silently no-op.

For the refresh-hook change *in isolation*, the risk-officer's verdict was APPROVE WITH NITS. Nits applied this session:

- `refresh_hook` typed as `Callable[[], None] | None` instead of `object | None`.
- `SessionConfig.__post_init__` validates callable + positive interval at construction so misconfiguration fails fast instead of logging a refresh-failure forever.
- Runtime budget documented in the `SessionConfig` docstring — hook must return well inside `poll_interval_seconds` (recommended ≤30s for the 60s default).

**Net**: this session's refresh-hook change is risk-approved on its own merits. The three pre-existing items above remain open and were already flagged in the 2026-05-19 handover below.

---

### Target architecture (set by user this session): three separate, independently runnable bots

The user has stated the end-state they want:

| Bot | Status | Strategy | Window |
| --- | --- | --- | --- |
| **ORB trader** | EXISTS, runs today | `ORBStrategy` at `src/trading_bot/strategy/orb/strategy.py` | Opening hour (09:30–~11:00 ET) |
| **Insider trader** | EXISTS, now with live cache refresh | `InsiderStrategy` at `src/trading_bot/strategy/insider/strategy.py` | Fires at 10:00 ET on filings whose `effective_entry_date` matches today |
| **Midday trader** | **DOES NOT EXIST YET** — needs to be designed and built | TBD — see open questions below | Mid-session (roughly 11:00–14:00 ET, the gap between the ORB window and the close-of-day) |

**What "separate bots" means is not yet decided.** The user said "3 separate bots" but did not specify the deployment shape. There are two reasonable readings, and the next session must pick one with the user before any code is written:

| Option | What it is | Pros | Cons |
| --- | --- | --- | --- |
| **A. Three processes, one composite each** | Spawn three `run_paper.py` instances, each constructed with a single strategy (no `CompositeStrategy` wrapping). Each has its own audit log, its own `SessionState`, its own broker client. | Maximum isolation — a bug or hang in one bot cannot stop the others. Audit logs are cleanly per-strategy. Easy to A/B by simply not launching one. | Three Alpaca clients hammer the same paper account; rate limits could surface. Three independent `open_entries` and `realized_pnl_today` views — no shared kill-switch on daily loss. Three sets of `UNRECONCILED_*.json` sentinel files. Three startup pre-flight backfills (harmless, the cache is idempotent). |
| **B. One process, three named strategies in `CompositeStrategy`** | Keep today's `CompositeStrategy` shape. Add the Midday strategy as a third inner. `--no-orb`/`--no-insider`/`--no-midday` flags let the operator turn any of the three on/off without changing the deployment. | Single broker session, single shared risk cap, single audit log — much easier to reason about daily P&L and the kill switch. No new infrastructure. Already the shape `run_paper.py` is built for. | Not "separate bots" in the literal sense — they share a session. A logic bug in any one strategy can in principle affect the others (in practice, conflict resolution at `composite.py:144-176` already isolates entries). |

**My (Claude's) recommendation**: Option B with strict per-strategy attribution. The risk boundary in this project is shared capital — one bot, three strategies, one daily-loss kill-switch. Option A's headline isolation is appealing but reproduces the kill-switch + position-cap state across three processes, which is actively dangerous: a runaway in one bot can't be stopped by the others' caps. The 2026-05-19 handover already lists `Trade.strategy_id` as the missing piece for per-strategy P&L attribution — if we land that, Option B gives the user everything Option A promises without the shared-cap regression.

**User has not picked yet.** Do not start building the Midday strategy until they pick A vs B, because the wiring is different.

### Midday strategy — what to design (when the user gives the go-ahead)

Properties the design needs:

- **Fires throughout the mid-session window (roughly 11:00 ET to ~14:00 ET).** The user's frustration today was that ORB owns the open and Insider fires at 10:00 ET, but nothing covers the rest of the session. Signals should be available on every poll iteration in the window, not just at one specific time.
- **Clear stop + take on every entry signal.** The bracket contract at `execution/alpaca_paper.py:206` requires both. No naked entries.
- **No overnight thesis.** Project is daytrade-only per CLAUDE.md.
- **`score: float` in [reasonable bounded range]** comparable to ORB's `or_atr_ratio` and Insider's `strength`, so the cross-strategy picker at `strategy/picker.py` can rank candidates from all three on the same axis.

Candidate strategies (from the 2026-05-19 handover, all rejected on backtest then revisited with `csuite_conviction`-style cleanups still possible):

1. **VWAP reclaim / rejection** — already implemented at `src/trading_bot/strategy/vwap/strategy.py` and **failed the acceptance bar** per `memory/project_afternoon_strategy_failures.md`. Code is there but not wired. Could be revisited with tighter filters, a different universe, or as a "weak signal only fires when no other strategy has a candidate" mode.
2. **Rolling range breakout (RRB)** — also implemented at `src/trading_bot/strategy/rrb/strategy.py` and also failed acceptance. Same situation as VWAP.
3. **Pullback to moving average after a trending move** — not yet implemented. Probably the most promising untried idea.
4. **5-min RSI mean reversion** — not yet implemented. High-frequency but historically nasty drawdowns. Lowest priority.

The user's memory `memory/project_afternoon_strategy_failures.md` explicitly notes that VWAP and RRB both failed on the SPY/NVDA/AAPL/JPM universe. If we resurrect either, it needs a different universe or a different acceptance bar, justified up-front. **Strategist agent should propose a fresh design rather than re-running VWAP/RRB without changes.**

### Decisions the user must make before the Midday build starts

1. **Option A or Option B** for the "3 separate bots" architecture (see table above).
2. **Which Midday strategy concept.** Revisit a failed one with tighter rules, or design something new (e.g., pullback-to-MA)?
3. **Universe for the Midday bot.** Same wide universe as today? A different cohort (e.g., higher-beta names where mid-session moves are larger)? Universe choice is load-bearing — VWAP/RRB might pass on a different universe even though they failed on SPY/NVDA/AAPL/JPM.
4. **Picker behavior across three strategies.** Today the picker keeps top-N by `or_atr_ratio` from the composite. With three strategies all emitting on the same poll, does the user want strict per-strategy quotas (e.g., max 2 from ORB, 2 from Insider, 1 from Midday) or pure score-based selection? Tied to whether Option A or B is chosen.

### Pre-existing risk-officer concerns still open (not addressed this session)

These were flagged in the 2026-05-19 session and remain in the working directory uncommitted:

- `SessionConfig.flatten_on_exit` default flipped to `False` — needs an explicit justification or a revert. Risk-officer correctly notes test fixtures mask the default flip.
- `Trade.strategy_id` for per-strategy P&L attribution — required if Option B is chosen for the 3-bot architecture. Without it, daily P&L lumps strategies together.
- Reconciliation step at session start for stale `sess.open_entries` when a bracket child fires while the bot is offline.
- Persist `open_entries` to disk so bot-side stop/take enforcement survives a restart (without it, only broker bracket children guard rollover positions).
- Gate `--no-flatten-on-halt` behind a `TRADING_MODE=live` refusal whenever live trading is enabled.

### Suggested first move next session

> "Before any code: confirm Option A (three processes) vs Option B (one process, three strategies). Then pick the Midday strategy concept and universe. Then the strategist agent designs it, the backtester validates it, and only then is it wired into `run_paper.py` or a new entrypoint. Pre-flight: verify the insider refresh is doing what it should by tailing this morning's audit log and checking the `insider cache refresh starting`/`InsiderStrategy reloaded` lines fire ~every 15 minutes."

### One thing to verify on the next morning launch

Today's failure was the insider cache being 5 days stale. The pre-flight refresh built this session should fix that automatically on every launch, but the *first* launch after this change is the one to verify. Look for these new INFO lines in the audit log on next startup:

- `insider pre-flight refresh starting (last 7 days)`
- `insider cache refresh starting` (then `... done` with `filings_fetched` > 0 if anything new was filed since 2026-05-15)
- `InsiderStrategy reloaded` (every 15 minutes during the session)

If `filings_fetched` is 0 even though days have passed since the last cache write, EDGAR or the rate-limited client is wedged and the insider arm will still emit nothing — at which point inspect `data/insider_raw/` for the most recent fetch and `EdgarClient` logs.

---

## Where we left off (2026-05-20, evening — composite ORB + InsiderStrategy wired into run_paper.py; late-launch bug fixed)

This session validated, killed, and re-attempted the midday-coverage strategy three times. Final state: a working composite (ORB + Form 4 insider) wired into `scripts/run_paper.py`, with universe widened to 56 tickers and the InsiderStrategy adapter's late-launch bug fixed. Bot was launched twice today but both launches were after the morning ORB window had already fired; user has not yet observed a real intraday trade fire from the new composite.

### Status check — first thing to do next session

1. Ask: "Did you launch the bot tomorrow morning before 15:30 Danish time (09:30 ET / 13:30 UTC)?" — that's the only window where ORB signals are fresh.
2. If yes, read the most recent `data/ops/logs/<run>.jsonl`. Look for:
   - `InsiderStrategy constructed` (already confirmed firing on launch)
   - Any `submitting order` lines with `symbol` in the insider-track regional banks (CBC, WSBC, GABC, SFNC, FMBM, BWFG, CIVB, MIAX) or insider-track adds (EMN, MTDR, SPG, COO, NSP) — proves the insider arm fired and the picker selected it.
   - Whether `or_atr_ratio` for those insider signals is in [0, 1] (insider strength) vs the ORB signals where it's typically > 0.5 — confirms the picker is seeing both signal classes correctly.
3. If insider signals fire too rarely, that's the next decision point — widen the universe further toward the small-cap regional bank cohort (CBKM, AVBH, AVBC, GBLI, ATLO etc., all <$500M) or accept ORB-dominant flow.

### What was built this session

**Backtests (three rounds, all bar one negative):**

- VWAP on original universe (SPY/NVDA/AAPL/JPM) — PF 0.55-1.12 at 1bp; score corr 0.03-0.04. FAIL.
- RRB (Donchian-style breakout) on original universe — PF 0.62-0.88 at 1bp; score corr 0.0102 on N=2202. FAIL. Lookback ablation (30/60/120) didn't rescue.
- ORB/RRB/VWAP all on high-vol mega-cap universe (TSLA/AMD/COIN/MARA/MSTR/NVDA) — only ORB cleared (PF > 1.0 on 4/6 strict). RRB and VWAP both failed again.
- Form 4 insider strategies (cluster_buy, csuite_conviction) on data/insider/ parquet cache — aggregate PF positive across all 6 (strategy × holding) cells (1.33-7.62), but N≤58 per cell vs N>=200 bar. Score correlations noisy (-0.40 to +0.20).

The user explicitly authorized taking the underpowered-but-positive insider result to live paper trading rather than wait for more historical data.

**Composite integration (this session's main build):**

- `src/trading_bot/strategy/composite.py` — `CompositeStrategy` wraps N inner strategies under the existing `Strategy` Protocol. Conflict resolution: same-(timestamp, symbol) entries dedup by highest `score` with priority-order tiebreak. `flat` signals always pass through unchanged so per-strategy exit logic still runs.
- `src/trading_bot/strategy/insider/strategy.py` — `InsiderStrategy` adapter. Loads parquet at construction, runs both detectors, indexes signals by `effective_entry_date` (next trading day after `max_filing_date` — anti-lookahead). On each `generate_signals(bars)` call emits one entry at the latest bar at-or-after `entry_et` ET plus a forced flat `holding_days` later (default 10 days).
- `src/trading_bot/strategy/base.py` — added `score: float64` to `empty_signals_frame()`. ORB/VWAP/RRB all updated to emit `score = or_atr_ratio` for backward compat with the picker. The picker still reads `or_atr_ratio`; migration to `score` is deferred.
- `src/trading_bot/strategy/insider/gates.py` — patched to drop sentinel-string tickers ("NONE"/"N/A"/empty/whitespace) which were 193 of 828 cached signals (~23% data quality bug from the parser).
- `src/trading_bot/strategy/insider/cluster_buy.py` and `csuite_conviction.py` — added `max_filing_date` to the signal `metadata` dict so the adapter can derive the effective entry date.
- `scripts/run_paper.py` — instantiates `CompositeStrategy([ORBStrategy(...), InsiderStrategy(...)], names=["orb", "insider"])`. New CLI flags `--no-orb` / `--no-insider` for diagnostic isolation runs. Default `--symbols` widened from 24 to **56 names** with insider-track regional-bank additions documented inline.

**Late-launch bug fix (after user's first launch attempt revealed it):**

The user launched the bot twice today, both times after the morning ORB window. The first launch (~15:43 UTC) hit the documented one-shot ORB problem; the second launch (~20:26 UTC) was after market close. Both surfaced 18-25 "skipping stale signal" warnings. Investigation showed `InsiderStrategy.generate_signals` was anchoring to the **first** bar at-or-after 10:00 ET on each date, meaning any insider signal it emitted was instantly stale once the bot launched after ~10:05 ET — same failure mode as ORB by design.

Fix at `src/trading_bot/strategy/insider/strategy.py:272` and `:301`: use the **latest** bar at-or-after `entry_et` instead of the first. Signals stay fresh on every poll iteration; the session's `open_entries` dedup prevents double-entries. New test `test_entry_anchors_to_latest_bar_for_freshness` pins this behavior.

**Tests:**

- 293 pass + 1 pre-existing known failure (`test_filter_audit_log_emits_or_atr_and_cold_start`, unchanged).
- New test files: `tests/test_strategy/test_composite.py` (11 tests), `tests/test_strategy/insider/test_strategy_adapter.py` (10 tests including the new late-launch test), `tests/test_strategy/insider/test_strategy_adapter_integration.py` (`@pytest.mark.integration`, skips if `data/insider/` is empty), `tests/test_strategy/insider/test_form4_gates.py` (NONE/N/A sentinel filter).
- `pyproject.toml` — registered the `integration` pytest marker.

**Memory:**

- `memory/project_afternoon_strategy_failures.md` — updated to capture the 5-strategy attempt history, the universe-mismatch caveat, and that the user authorized taking the underpowered insider result to live paper.

### Universe state — what's in `--symbols` and why

56 tickers total in `_WIDE_UNIVERSE_DEFAULT`:

- **Index/sector ETFs:** SPY, QQQ, IWM, XLE, XLF, GLD
- **AI/semis:** NVDA, AMD, MU, AVGO, SMCI, MRVL, ARM
- **Mega-cap tech:** AAPL, MSFT, META, GOOGL, AMZN, NFLX, ORCL, CRWD, SNOW, SHOP, BABA
- **Financials:** JPM, GS
- **Crypto/high-vol:** COIN, MSTR, RIOT, HOOD, TSLA
- **Diversifiers:** ABNB, BA, LLY, LMT, DIS, NKE, UNH, RBLX, PLTR, FCX, CVX, XOM
- **Insider-track adds (high signal density, mid/large-cap):** EMN, MTDR, SPG, COO, NSP
- **Insider-track regional banks + others:** CBC, WSBC, GABC, FMBM, SFNC, BWFG, CIVB, MIAX

Explicit exclusions: MARA (PF 0.68 + microstructure, per 2026-05-20 backtest). Considered-and-rejected: AVBH, AVBC, AEBI, MKZR, AMRZ, STSS, BETA, MSDL (ambiguous ticker identities without an API lookup), ATLO/QNBC/CBKM (sub-$200M market caps).

### The universe-mismatch caveat (load-bearing)

Form 4 insider signals surface ~257 unique tickers in the cached window. Even with the 13 insider-track additions, the overlap between insider-surfaced tickers and `--symbols` is small. The InsiderStrategy adapter at `strategy.py:286-289` silently skips signals for tickers not in the bars frame — and bars are fetched only for `--symbols`. So the insider track will fire rarely in live paper trading until either (a) `--symbols` is widened to ~50+ small-cap regional banks (less liquid for paper fills but reasonable at $5k position sizes), or (b) the adapter is rebuilt to dynamically subscribe to bars for surfaced tickers (significant new infrastructure).

The user has not picked (a) or (b) yet. Default behavior right now: ORB-dominant flow with occasional insider fires on the ~6-7% overlap names.

### Deferred (do NOT do without risk-officer review)

These were on the original HANDOVER.md plan but explicitly deferred this session because they touch `execution/`, `risk/`, or `contracts.py`:

- `Trade.strategy_id` field on `src/trading_bot/contracts.py` for per-strategy P&L attribution.
- Plumbing of `strategy_id` through `sess.open_entries` and `_record_exit` at `src/trading_bot/execution/session.py`.
- Migrating the picker at `src/trading_bot/strategy/picker.py` from `or_atr_ratio` to the new `score` column. Currently both columns carry the same value, so this is cosmetic for the moment.
- Live EDGAR polling for new filings mid-session (adapter currently reads the static parquet only — last backfill 2026-05-17, ran ~10 hours overnight per the prior session's note).
- Reconciliation step at session start for stale `sess.open_entries` rows when a bracket child fires while the bot was offline (flagged by risk-officer in the 2026-05-19 session, still open).

### Open questions for the user

1. **Insider universe expansion.** Is `--symbols` good as-is, or should we widen to capture more Form 4 signals? The smallest-cap insider regional banks (sub-$500M) trade on real volume but have wider spreads — paper fills will be optimistic relative to live.
2. **Per-strategy P&L attribution.** Without `Trade.strategy_id`, end-of-session P&L lumps ORB and insider trades together. For diagnosis you can grep logs by symbol (ORB hits the mega-cap allowlist; insider hits the regional bank cohort) — but a real attribution column is the proper fix and needs a risk-officer-approved Trade schema change.
3. **What to do if insider fires zero times.** If the next live session shows zero insider entries, the universe-mismatch is real and the user needs to pick (a) widen `--symbols`, (b) build dynamic ticker subscription, or (c) accept ORB-only and use the morning launch window strictly.

### Suggested first move next session

> "Confirm tomorrow's morning launch fired ORB cleanly and report whether any insider entries appeared in the logs. Then pick a direction on the universe-mismatch caveat — widen `--symbols`, build dynamic subscription, or accept the current overlap."

### Hard rules still in force (from CLAUDE.md, unchanged)

- Paper-only default. `TRADING_MODE=paper`.
- No forecasts in outputs.
- No git operations from Claude.
- Pure functions in `strategy/`. Adapter's parquet-load-at-construction is the documented exception.
- Both `stop_price` and `take_price` must be set on every entry signal (bracket contract at `execution/alpaca_paper.py:206`). InsiderStrategy honors this via synthetic ATR-derived stop/take.
- Pre-existing `tests/test_strategy/test_orb.py::test_filter_audit_log_emits_or_atr_and_cold_start` failure is known and unrelated.

---

## Where we left off (2026-05-19, late afternoon — stop/take enforcement wired; second intraday strategy is the next build)

This session diagnosed and fixed a real wiring gap (stops/takes were computed but never enforced), then surfaced the next problem to solve: the bot only generates entry signals during the opening hour, so launching mid-day produces nothing.

The user wants a **second intraday-friendly strategy** built so that missing the open is no longer fatal. **Do not start building it yet — they asked for a handover only.** This section is the brief.

### Status check — first thing to do next session

1. Ask: "Did you launch the bot tomorrow morning before 13:30 UTC (09:30 ET)?" — confirms they had a clean ORB run after this session's stop/take fix.
2. Read the most recent `data/ops/logs/<run>.jsonl`. Confirm:
   - `stop/take triggered` lines appear (or don't, depending on price action) — proves `_check_stops_and_takes` runs.
   - Entry `submitting order` lines now include `"order_class": "bracket"` and `stop_price`/`take_price` fields — proves bracket children are attached at the broker.
3. If they then ask about Strategy #2, work through the open questions below.

### What was built this session

**Stop/take enforcement** (touches `execution/`, risk-officer approved twice):

- `src/trading_bot/execution/alpaca_paper.py:206` — `submit_order` attaches bracket children (`OrderClass.BRACKET` + `StopLossRequest` + `TakeProfitRequest`) whenever a `ProposedOrder` carries both `stop_price` and `take_price`. Falls back to OTO when only one is set, plain market when neither is set.
- `src/trading_bot/execution/session.py:712` — new `_check_stops_and_takes` helper. Runs once per poll iteration before signals process: fetches latest price for each open `sess.open_entries[symbol]`, closes on breach via `cancel_orders_for` (kills the bracket children) → `close_position` → `_record_exit` with `exit_reason="stop"` or `"take"`. Stop wins over take if both are simultaneously breached.

**Flatten gate split** (also `execution/`, also risk-officer approved):

- `SessionConfig.flatten_on_exit` default **changed from `True` to `False`** — applies on clean exit only (manual SIGINT, market close, max_iterations).
- New `SessionConfig.flatten_on_halt: bool = True` — applies on fault-class halt (kill switch, broker auth/validation, consecutive failures, mid-sleep failures).
- Gate logic at `session.py:1283-1289`: reads `sess.halt_reason` to pick which knob applies.
- CLI in `scripts/run_paper.py`: `--no-flatten` replaced with `--flatten-on-exit` (opt-in) and `--no-flatten-on-halt` (discouraged opt-out).

**Tests**: 71/71 pass in `tests/test_execution/`. 13 new tests for stop/take + bracket submission + flatten matrix.

**Risk-officer follow-up flags (non-blocking):**
1. If a bracket child fires while the bot is offline, the position vanishes server-side but `sess.open_entries` keeps the stale row until session-end — no `Trade` record is written. Safety-wise fine (capital not at risk; new entries re-check broker positions); observability gap. Worth a reconciliation step at session start later.
2. `open_entries` is in-memory only. With `flatten_on_exit=False`, positions roll across restarts but their stop/take levels are lost — only the broker bracket guards them on the next session. Persist `open_entries` to disk if the user wants bot-side enforcement to survive a restart.
3. In any future move toward live trading, gate `--no-flatten-on-halt` behind a `TRADING_MODE=live` refusal.

### Why the user wants Strategy #2 — the actual problem

ORB ("Opening Range Breakout") emits its entry signal **once per day per symbol**, in the first ~30–90 minutes after open. Today the user launched at 14:39 ET (18:39 UTC) and saw 14 "skipping stale signal" log lines and zero entries. Working as designed (the `max_signal_age_minutes=5` guard exists for the 2026-05-14 wash-trade reason — *do not lower it*), but it means the bot is useless after the morning window.

User's ask: a second strategy that **fires throughout the session** so a 14:00 ET launch still finds work, layered alongside ORB rather than replacing it.

### Decisions the user has made on this build (do NOT re-litigate)

- **Layered, not replacement.** ORB stays as-is. Strategy #2 runs in the same session, sharing risk/execution/picker plumbing.
- **Independence from the insider track.** This is unrelated to the Form 4 pipeline. Both can coexist; the insider work is its own thing.
- **Don't lower `max_signal_age_minutes`.** That guard exists for a reason. The fix is a strategy with fresh intraday signals, not weaker freshness checks.

### Strategy candidates worth proposing (rank order)

The strategist agent should pick one to design first. Properties needed: **emits signals across the session, not just at the open; clear stop/take rule (the new bracket wiring expects both); no overnight thesis (project is daytrade-only).**

1. **VWAP reclaim / rejection** — long when price closes back above VWAP from below (with N-bar confirmation); short on rejection from above. Strong fit: VWAP is the most-used institutional intraday benchmark; signals fire continuously; pairs naturally with ORB (ORB plays the breakout direction at the open; VWAP plays mean reversion later). Stop = N×ATR; take = R-multiple of stop distance. **Recommended starting point.**
2. **Range breakout (rolling, not opening)** — same mechanic as ORB but with a sliding N-minute reference window instead of a fixed opening-range. Conceptually closest to ORB; could share a lot of code; but signals can fire on choppy ranges and need a volume/volatility filter.
3. **Pullback to moving average** — after a trending move (defined by slope / consecutive higher-highs), enter on a pullback to a configurable MA (e.g., 20-period EMA on 5-min bars). Good signal density; needs a "what counts as a trend" filter that's tunable without lookahead bias.
4. **5-min RSI mean reversion** — buy oversold (RSI<25) / sell overbought (RSI>75) inside the session. High signal frequency, but mean-reversion strategies have notoriously sharp drawdowns; would need tight stops. Probably the riskiest of the four; mention but don't start here.

### Architectural fit — what needs to change to run two strategies in one session

Today `run_session(broker, strategy, config, risk_params, ...)` takes a single `Strategy` (Protocol at `src/trading_bot/strategy/base.py:10`). Three plausible refactors:

| Option | What it is | Pros | Cons |
| --- | --- | --- | --- |
| **A. CompositeStrategy wrapper** | A `CompositeStrategy` that holds N inner strategies, calls `generate_signals` on each, concatenates results, tags `strategy_id`. Implements the same `Strategy` Protocol. | Zero churn in `run_session`. Easy to test each strategy in isolation. | Per-symbol conflict resolution lives inside the wrapper, not the session. |
| **B. `run_session` takes a list** | Change signature to `strategies: list[Strategy]`. Session loops over them. | Conflict resolution happens at the session layer where the picker already lives. | Touches `execution/` — needs risk-officer review; breaks every existing test signature. |
| **C. Run two `run_session` processes** | Spin up two independent sessions, each with its own strategy. | Maximum isolation; no shared bugs. | Two broker clients, two sets of `open_entries`, can't share the position cap. **Don't do this.** |

**Recommended: Option A (CompositeStrategy).** Minimal churn, isolation per strategy, single session = shared risk cap. The strategist agent should design this.

### Open questions the user needs to decide before / during the build

1. **Which strategy first?** Recommend VWAP reclaim (see ranking above), but the strategist agent should validate against the existing backtest engine's capabilities.
2. **Same-symbol same-iteration conflict resolution.** If ORB says LONG SPY and VWAP says SHORT SPY in the same poll, what happens? Options: (a) earlier strategy wins (ORB always first), (b) higher-`or_atr_ratio`-equivalent wins, (c) suppress both. The existing `_handle_entry_signal` *already* skips entries when a position exists (`session.py:649`), so the second-iteration case is handled; only the first-iteration tie needs a rule.
3. **Ranking score across strategies.** The picker (`src/trading_bot/strategy/picker.py:27`) ranks by `or_atr_ratio` — an ORB-specific field. The handover for the build needs to spec a generic `score: float` column on the SignalsFrame that every strategy emits, comparable across strategies. Otherwise the picker can only rank within a strategy.
4. **Per-strategy or shared position cap?** Recommend shared (capital is one pool; current `max_position_count=5` is already a total cap). Don't introduce per-strategy caps unless backtests reveal one strategy crowds out the other.
5. **Trade tagging.** `Trade` (`contracts.py:107`) has no `strategy_id`. Needed so the end-of-session table and any later overlap analysis can attribute P&L by strategy. Schema change is small and additive (default to `"orb"` for back-compat).

### What to build, in order

1. **Strategist agent**: design the chosen strategy's signal rule, stop/take, and parameters. Output: `src/trading_bot/strategy/<name>/{config,strategy}.py` matching the ORB layout.
2. **Strategist + base.py**: add `score: float` to `empty_signals_frame()` and require all strategies to emit it (ORB can wrap `or_atr_ratio` into `score`). Keep `or_atr_ratio` for ORB-specific debugging.
3. **Strategist**: build `CompositeStrategy` in `src/trading_bot/strategy/composite.py` — implements `Strategy` Protocol, concatenates child signals, tags `strategy_id`, handles same-symbol same-iteration ties with a configurable resolver (default: highest `score` wins; ties broken by `strategy_id` priority order).
4. **Backtester agent**: backtest the new strategy in isolation. Verify it actually produces signals at non-opening times. Compare metrics to ORB.
5. **Backtester**: backtest the composite. Verify the composite ≈ sum of individual unless conflicts trigger (then docs explain the suppression).
6. **`contracts.Trade` + session.py**: add `strategy_id: str = "orb"` to `Trade`. Plumb through `sess.open_entries` and `_record_exit`. Tests for the new column in the end-of-session table.
7. **Wire `scripts/run_paper.py`**: instantiate `CompositeStrategy([ORBStrategy(...), <New>Strategy(...)])`. Add CLI flags to disable either strategy (`--no-orb`, `--no-<name>`) for diagnostic runs.
8. **Risk-officer review**: required because step 6 touches `execution/` (Trade schema, `_record_exit` plumbing). Step 7 just touches `scripts/`, no review needed.

### Pre-existing issue still hanging around

`tests/test_strategy/test_orb.py` was reported failing on `main` in the 2026-05-17 session. Not verified or fixed this session. Check whether it still fails before kicking off any new strategy work — backtesting against a broken ORB baseline would be misleading.

### Suggested first move in the next session

> "Confirm the bracket + poll-side stop wiring fired correctly on this morning's run. Then ask the strategist agent to design VWAP reclaim with a CompositeStrategy plan."

If the user wants to skip the strategist design step and write the strategy themselves, the contracts they need to hit are: `generate_signals(bars) -> DataFrame` with the SignalsFrame columns at `src/trading_bot/strategy/base.py:16`. The new `score: float` column hasn't been added yet — they'd add it as part of step 2.

---

## Where we left off (2026-05-17, evening — Form 4 insider pipeline built; backfill running overnight)

This session built a **completely new, independent strategy track** alongside ORB:
insider-trade signals from SEC Form 4 filings via EDGAR. The user wants insider and ORB
strategies kept **fully separate** (no merged signal) but compared after both have
backtested — to see whether they ever fire on the same ticker on the same day.

### Immediate state (what the user is doing right now)

The user started `uv run python scripts/backfill_form4.py --months 3` and is leaving
it running overnight in tmux. Expected runtime ~10 hours at the observed throughput.
It is **resumable** — re-running just skips accessions already in Parquet.

When the next session starts, the FIRST thing to do:

1. Ask: "Did the Form 4 backfill complete? Want me to verify the data?"
2. If yes: `uv run python -c "import pyarrow.dataset as ds; print(ds.dataset('data/insider', partitioning='hive').count_rows())"` — sanity-check row count and partition coverage (months Feb/Mar/Apr/May 2026).
3. If no / interrupted: re-run the same command; it resumes.

### The plan after the backfill is in

User's stated path forward (in order):

1. Verify Form 4 backfill is clean.
2. **Build historical OHLC price ingestion** — STILL MISSING. Required for any backtest.
   Suggested: `data-engineer` agent → `src/trading_bot/data/prices/`, Alpaca historical
   bars (project is already Alpaca-based; same API keys), or yfinance fallback. Schema:
   ticker, date (UTC), OHLCV, Decimal for prices.
3. Run the insider backtest — `backtester` agent. Joins filter signals to forward
   prices over configurable hold windows; reports computed-from-history metrics only
   (no forecasts per CLAUDE.md).
4. Run an ORB backtest on the same price data.
5. **Overlap analysis**: intersection of `{(date, ticker)}` from insider signals and
   from ORB signals. Observational only — *not* a merge.

### What was built this session

**Insider Form 4 ingestion** — `src/trading_bot/data/insider/`:
- `client.py` — polite SEC HTTP client (8 req/sec, retries, on-disk raw cache, no `Accept-Encoding: gzip` because urllib doesn't auto-decompress)
- `index.py` — quarterly `form.idx` fetch + regex-anchored row parser
- `parser.py` — Form 4 XML → `Form4Filing` rows
- `cache.py` — Parquet writer, hive-partitioned `year=YYYY/month=MM/`
- `tickers.py` — CIK → ticker map from SEC's `company_tickers.json`
- `backfill.py` — orchestrator; fetches `index.json` per filing to resolve the XML name (which is NOT `primary_doc.xml`), then the XML
- `schema.py` — `Form4Filing` frozen dataclass + Arrow schema, 18 columns, Decimal for monetary fields

**Filters** (pure functions, no I/O) — `src/trading_bot/strategy/insider/`:
- `gates.py` — universal gates: P-code only, drop 10b5-1, drop price <$5, drop null ticker, drop filing-delay >2 biz days
- `cluster_buy.py` — ≥3 distinct insiders within a 10-day window
- `csuite_conviction.py` — CEO/CFO open-market buy ≥$100k with 180-day cooldown per (insider, ticker)

**CLI**: `scripts/backfill_form4.py --months N --max-filings N`

**Storage layout** (matters for tooling):
- Parquet (clean root, scans via `pyarrow.dataset.dataset("data/insider", partitioning="hive")`):
  `data/insider/year=YYYY/month=MM/part-*.parquet`
- Raw HTTP cache (deliberately OUTSIDE the Parquet root): `data/insider_raw/`

**Tests**:
- `tests/test_data/insider/` — 16 unit tests passing
- `tests/test_strategy/insider/` — 31 unit tests passing
- Live EDGAR tests are `@pytest.mark.network`, opt-in only

### Decisions the user has made (do NOT re-litigate)

- **Insider and ORB stay independent.** Not mixed. Overlap analysis is observational.
- **Cluster Buy + C-Suite Conviction** are the chosen insider filters. Contrarian,
  buy/sell ratio flip, and small-cap subset were considered and parked.
- **Parquet, not SQL.** DuckDB-on-Parquet is an acceptable future query layer.
- **3-month backfill window** for the first pass (user wanted a faster first iteration than 12 months).

### Bug history from this session (fixed but worth knowing)

The data-engineer agent's first pass shipped four real bugs that all surfaced when
the user tried to run the backfill. Each was diagnosed against live EDGAR responses:

1. **gzip cache poisoning** — client advertised `Accept-Encoding: gzip, deflate` but
   urllib doesn't auto-decompress. Gzipped bytes got cached, then JSON-decoded as
   garbage. Fix: drop the Accept-Encoding header entirely. SEC returns identity.
2. **`form.idx` column parsing** — real EDGAR format has a single solid run of dashes
   for the divider (not space-separated `----  ----`) AND data columns are *wider*
   than the header (~155 chars vs. ~107). The parser now uses a regex anchored on
   the structural invariants (CIK = digits, date = ISO, file path starts with `edgar/`).
3. **Form 4 XML filename is not `primary_doc.xml`** — observed in the wild:
   `ownership.xml` (most common), `form4_*.xml`, `wk-form4_*.xml`, custom per filer
   agent. Resolution: fetch `index.json` per filing, pick the first `.xml` that
   isn't an index artifact. Costs 2 requests per filing.
4. **Raw cache nested under Parquet root** — `data/insider/raw/` broke
   `pyarrow.dataset.dataset(...)` schema inference because it tried to read the
   raw cache as Parquet. Moved to `data/insider_raw/`.

### Open semantic questions (strategist flagged; not yet resolved)

Strategist picked sensible defaults but called them out:

1. **Cooldown boundary**: `elapsed_days <= cooldown_days` = "still in cooldown" (day 180 suppressed, day 181 fires). Strict `<` would mean day 180 fires.
2. **Cluster window**: inclusive `[end − 9 days, end]`, calendar days (not business days).
3. **Multi-buy by same insider within a cluster**: collapsed to one membership entry with summed value. Distinct-insider count drives the threshold.
4. **`signal_date` dtype**: Python `date` objects, not `datetime64[ns, UTC]`.

Worth revisiting once the backtest exposes behavior — don't tune speculatively.

### Operational details

- Required env: `SEC_USER_AGENT="Name email@addr"` in `.env` (SEC fair-access policy)
- Throughput: ~2 filings/sec (rate limit 8 req/sec, 2 requests per filing)
- Resumability: re-running the script skips accessions already in Parquet AND skips raw fetches that hit the on-disk cache. Safe to Ctrl-C and resume.

### Pre-existing issue surfaced (not from this session)

Both data-engineer and strategist agents independently noticed
`tests/test_strategy/test_orb.py` is failing on `main`. Not caused by insider work
but blocks the ORB-backtest leg of the planned overlap analysis. Investigate when
convenient — see prior handover sections below for ORB context.

### Suggested first move in the next session

> "Verify the Form 4 backfill, then kick off price-data ingestion via the data-engineer agent."

If the user wants to skip ahead to the overlap analysis, remind them they need price
data first — the insider Parquet alone doesn't let the backtester compute outcomes.

---

## Where we left off (2026-05-14, late evening — 9-bug sweep + cache fix + cross-symbol picker)

**164/164 tests pass.** No commits — git is user-handled.

### Tomorrow's experiment (2026-05-15) — the user is testing the picker

**IMPORTANT to the next Claude:** the user is going to run the bot tomorrow on
paper. **Remind them at the end of the session to COMPARE the picker's choices
against expectations** — they explicitly said they might forget. If the next
session begins after market close on 2026-05-15, the FIRST thing to do is:

1. Ask the user: "Did you run the bot today with the picker? Want me to pull up
   the audit logs and compare?"
2. If yes, read `data/ops/logs/<latest-paper-session>.jsonl`, grep for
   `"picker selected entries"`, and walk through which symbols the picker
   chose vs. which it dropped each iteration. Pull the `or_atr_ratio` field
   from the `ORB filter check` records to explain the ranking.
3. Decide together whether to keep the picker, tune `picker_min_ratio`, or
   revert to the curated 7-name list.

### The plan for tomorrow

User will run **default settings** to test the picker:

```bash
uv run python scripts/run_paper.py
```

Defaults:
- 24-name wide universe (index ETFs + AI/semis + mega-cap tech + financials + crypto-adjacent)
- `--max-concurrent-entries 5` (picker picks top 5 by OR/ATR ratio each iteration)
- `--picker-min-ratio 1.0` (only candidates clearly above the 0.5 filter floor enter the pool)
- `--max-position-count 5` (hard risk cap, independent of universe size)

**Fallback if picker behaves badly:** the 7-name regime-stable set is still the
empirical baseline.
```bash
uv run python scripts/run_paper.py --symbols NVDA,QQQ,AMD,MU,COIN,ORCL,SMCI
```

### What to look at in the logs

- `"picker selected entries"` INFO records: `extra.candidates` is everything
  fresh that iteration; `extra.chosen` is what fired. Difference = what the
  picker dropped.
- `"ORB filter check"` INFO records per symbol-session: include
  `or_atr_ratio` so you can see why each candidate ranked where it did.
- `"trade entry"` / `"trade exit"` records: now ALWAYS have real `entry_price`
  / `exit_price` / `pnl` (Bug #1 fixed today — previously all zeros).

### Today's work — what changed in the codebase

This was a long session. Two phases:

**Phase A — review-driven bug sweep (9 fixes, all approved by relevant agents):**

| # | Fix | Reviewer verdict |
|---|---|---|
| 1 | Trade fill-price/pnl recording (`_await_fill` + Protocol `get_order`) | risk-officer **PAPER-SAFE** |
| 2 | Signal-staleness guard (`max_signal_age_minutes=5`, flats bypass) | strategist clean |
| 3 | Cancel pending orders before flat (`cancel_orders_for`) | risk-officer **APPROVED** |
| 4 | Reconcile-on-startup (refuse on orphan broker positions) | risk-officer **APPROVED** |
| 5 | `latest_entry_et: "12:00"` time gate in ORB | strategist clean |
| 6 | Stray `data/ops/session_state/2026-01-05.json` removed; isolation confirmed | janitorial |
| 7 | `BrokerAuthError`/`BrokerValidationError` halt immediately (401/422) | risk-officer **APPROVED** |
| 8 | Mid-sleep failure counter halts at N=3 consecutive | risk-officer **APPROVED for paper** |
| 9 | Structured ORB audit logs (`or_high/or_low/atr/ratio/cold_start`) at INFO | strategist clean |

**Phase B — historical-runs investigation + picker:**

10. **Partial-cache silent under-sampling bug** — `BarCache.read_or_fetch`
    added. All 4 backtest scripts (run_backtest, screen_symbols, sweep_orb,
    tune_orb) had identical helpers that returned ANY non-empty cached data
    as authoritative — so 4 days of cache silently served as a 6-week
    backtest. Coverage check with 5-day tolerance now refetches on
    incomplete coverage. Data-engineer reviewed.
11. **Cross-symbol picker** (`strategy/picker.py`) — pure ranking module.
    `SessionConfig.max_concurrent_entries` and `picker_min_ratio` added.
    Two-pass session loop refactor: flats always fire, entries route
    through the picker. `run_paper.py` defaults updated for wide-universe
    operation. Strategist + risk-officer reviewed.

### Strategy-relevant config changes (in `strategy/orb/config.yaml`)

- `atr_period_sessions: 14` (was `atr_period_minutes` pre-2026-05-14 morning)
- `latest_entry_et: "12:00"` (new — Bug #5)
- `min_range_atr_multiplier: 0.5` (unchanged, but now a per-symbol filter
  floor; the picker's pool-entry floor at `picker_min_ratio=1.0` is the
  cross-symbol second gate)

### Open carry-forwards for live-mode review (do NOT lift live gate without these)

Collected across all 11 reviews today:
- Cancel-on-timeout for stuck market orders (Bug #1)
- Configurable fill-poll budget (Bug #1)
- Entry-path cancel symmetry to mirror Bug #3 (currently only flat-path
  cancels pending opposite-side orders)
- Two-key `RECONCILED_BY=<operator-id>` override for sentinel + orphan
  paths (Bug #4)
- Stricter 403 handling for live (current paper policy: 403 → retry,
  because Alpaca overloads 403 for business-rule rejections)
- Tighten `max_consecutive_midsleep_failures` to 2 for live; add
  degraded-mode entry suppression; two-consecutive-success reset (Bug #8)
- Re-screen 7-name universe under the new `latest_entry_et=12:00` default
  (Bug #5)
- HMAC + `created_at` freshness on equity persistence file
- Trading-calendar lookup (`exchange_calendars`) for exact cache-coverage
  tolerance vs. the 5-day heuristic
- Guard `BarCache.write` against partial-intraday slice overwriting a
  complete cached day
- **Per-sector concentration cap** for the wide universe (e.g. max 2
  simultaneous positions in {NVDA, AMD, MU, AVGO, SMCI, MRVL, ARM}) —
  current AI bucket can stop 5-in-1; daily-loss cap at -3% is the backstop
- **Per-symbol enabled/disabled list** for the picker driven by recent
  backtest PF — the picker can't know AAPL has 0.36 PF, only the ratio
- Move the wide-universe default to `strategy/universe.yaml` (currently
  hardcoded in `scripts/run_paper.py`)

### Today's findings worth knowing

- **SPY rejects ~27 of 31 sessions** with the default `min_range_atr_multiplier=0.5`
  — SPY's OR/ATR ratio sits at 20-40%. The strategy correctly identifies
  SPY as a poor fit. Audit log (Bug #9) made this diagnosable in <30s.
- **JPM showed pf=5.86 on a 6-week April-May 2026 window** — small-sample
  noise. 14 trades, ALL exiting "time stop" (EOD), no stops/takes hit
  because JPM's daily range stayed inside the ATR band. Backtest engine
  confirmed to check intra-bar stops correctly. Not a real edge.
- **The picker is brand-new code with no live tape behind it.** The
  user explicitly said tomorrow's run is the test. Compare results to
  the empirical 7-name baseline.

---

## Where we left off (2026-05-14, evening — symbol screening + ORB filter fix)

This session focused on **expanding beyond the SPY-only paper run and validating
the strategy on a real universe**. It surfaced — and fixed — a real strategy bug,
and ended with a smoke-tested paper deploy on a 7-name universe. **125/125 tests
pass.** Git is user-handled — nothing committed.

### Session summary

1. **Multi-symbol screen pipeline built.** `scripts/screen_symbols.py` runs the
   ORB strategy across a list of symbols on one window, ranks them by metrics,
   writes a comparison CSV. Sister script to `sweep_orb.py` (which varies a
   parameter on one symbol).
2. **Found and fixed an inert filter.** `min_range_atr_multiplier` was comparing
   the 30-min opening range against a *per-minute* mean true range
   (`atr_period_minutes=14` read 14 of the prior session's *minute* bars and
   averaged them). Scale mismatch of ~30×; the filter never rejected a session.
   Strategist agent replaced `_atr()` with a rolling mean of prior-session
   high-low ranges; renamed config field to `atr_period_sessions: 14`; the 0.5
   default now rejects 10–30% of sessions per sanity check. Diff confined to
   `strategy/orb/`.
3. **Wider screen pipeline result (fixed strategy, 31 candidates, 2025-11-15 →
   2026-05-13, 1-min bars, gauntlet = 1bp screen → 3bp stress → IS/OOS halves
   at 3bp):** 31 → 17 → 16 → 13 → **7** clear pf > 1.0 in BOTH halves.
   Survivors: **NVDA, QQQ, AMD, MU, COIN, ORCL, SMCI**. `run_paper.py --symbols`
   default updated to this set.
4. **Non-tech diversifier hunt failed.** 13 non-tech candidates (XOM, CVX, JPM,
   GS, BA, LMT, NKE, FCX, IWM, XLE, GLD, XLF, UNH) → **zero strict survivors**
   at pf > 1.0 in both halves at 3bp. JPM was borderline at 1.00/1.09. The ORB
   strategy as currently configured does not generalize to lower-volatility
   names.
5. **Smoke-tested paper deploy** at 15:48 ET on the 7-name universe.
   Risk-officer approved (paper-only, this session only). Bot opened 4
   positions (NVDA, AMD, COIN, SMCI), SIGTERM'd cleanly, flatten path ran,
   no unreconciled sentinel.

### Files changed this session (NOT committed — user handles git)

- `src/trading_bot/strategy/orb/strategy.py` — replaced `_atr()` with rolling
  mean of prior-session ranges; cold-start fallback retained but disarms the
  filter on day 1 by construction (documented).
- `src/trading_bot/strategy/orb/config.yaml` — `atr_period_minutes` →
  `atr_period_sessions`.
- `scripts/run_paper.py` — `--symbols` default `"SPY"` → 7-name list.
- `scripts/sweep_orb.py` — updated sweepable field name.
- `scripts/screen_symbols.py` (new) — multi-symbol screen pipeline.
- `scripts/tune_orb.py` (new, written by backtester agent) — 2-D parameter
  grid × IS/OOS halves.
- `tests/test_strategy/test_orb.py` — two regression tests guarding session-
  scale ATR semantics.

### Honest read of the post-fix universe

- AMD and MU are the most credible names — they tune cleanly (`min_rng=0.3-0.5`,
  `atr_stop=1.5`) with **40+ trades per half** and floor_pf 1.18–1.38.
- NVDA and SNOW pass too but only at higher `min_rng` (≥0.7), which drops
  trade count to single digits per half — low-N, suggestive of overfit.
- 6 of 7 default-config survivors are AI/semi-correlated. **Single-cluster
  risk is real**: an AI-capex correction would draw down 6 of 7 in sympathy.
- MRVL is the one prior-survivor that fails at every tuned grid point. ORB
  doesn't fit MRVL; useful negative result for future universe-selection logic.

### Next steps — FIRST fix, THEN expand (per user)

The 7-name list is paper-deployable today but the strategy has known weaknesses
(noisy half-to-half pf on 4 of 7, cold-start filter disarm, no time-of-day
filter, AI concentration). Widening before fixing just adds noise.

**Phase 1 — Fix the current method (in priority order):**

1. **Cold-start fallback.** When the prior-session buffer is empty, ATR falls
   back to the OR window's own range, so `or_range/atr = 1.0` passes any
   `min_range_atr_multiplier ≤ 1.0` trivially. On a 60-session half that's
   ~23% of sessions with a disarmed filter. Options: (a) seed the buffer from
   the most recent N closed sessions on session-start; (b) require N≥3
   buffered sessions before producing signals at all (skip warm-up days);
   (c) accept and document the disarm, knowing early-window pf is consistently
   noisier than late-window. (a) is the cleanest engineering fix.
2. **Time-of-day filter** — carry-forward from the prior session's roadmap
   item #3. Add `latest_entry_et: "12:00"` (configurable) to suppress entries
   after late morning; ORB historically does poorly on late-day fades. One-line
   change to the strategy's signal loop.
3. **`atr_stop_multiplier` re-sweep with the fixed filter.** Phase A showed
   `atr_stop=1.5` works for AMD/MU at high trade counts; `atr_stop=2.0` works
   for SNOW/NVDA but at low trade counts (overfit-suspect). After (1) and (2)
   above, re-run the 2-D grid to find the genuinely robust shared default.
4. **MRVL post-mortem.** Why does it fail at every grid point while similar
   semis pass? One-symbol investigation: gap structure, sector flows, OR
   character. The answer is a data point for universe-selection criteria
   beyond raw volatility.
5. **Bracket orders at the broker** — carry-forward from prior handover.
   `AlpacaPaperBroker.submit_order` drops `stop_price`/`take_price` on the
   floor; stops/takes rely on strategy flat signals. A disconnect between
   flat emissions leaves positions unbracketed. Not paper-blocking; tighten
   before any unattended overnight runs.

**Phase 2 — Expand the universe (only after Phase 1 produces measurable
improvement):**

1. **Re-run the wider screen** with the improved strategy. The 31-symbol pool
   from this session plus the failed 13 non-tech (~44 unique, most cached).
   Targets: (a) more semi names surviving at lower `min_rng` if the
   time-of-day filter cuts the bad-trade tail; (b) any non-tech crossing the
   bar if cold-start fix reduces early-window noise.
2. **Diversifier hunt round 2.** Sectors more likely to provide orderly
   opening ranges than what was tested: high-momentum biotech (BIIB, REGN,
   MRNA), volatile fintech (SQ, AFRM, UPST), retail with vol (LULU, RH).
   Avoid lower-vol large-caps unless filter selectivity improves.
3. **Acceptance gate** — a name joins the live universe only if it clears
   pf > 1.0 in BOTH IS and OOS halves at 3bp at the *shared default config*
   (not per-symbol tuned), AND has ≥20 trades per half. Do not lower the bar
   to hit a number. Concentration risk is real but noise-substitution is
   worse.

### Quick commands cheat sheet (post-fix)

```bash
# Paper trade — uses the 7-symbol regime-stable default
uv run python scripts/run_paper.py

# Override the default universe at run time
uv run python scripts/run_paper.py --symbols NVDA,AMD

# Multi-symbol historical screen (default 1bp; pass --slippage-bps 3.0 to stress)
uv run python scripts/screen_symbols.py \
    --symbols NVDA,QQQ,AMD,MU,COIN,ORCL,SMCI \
    --start 2025-11-15 --end 2026-05-13

# 2-D parameter sweep × IS/OOS halves (check tune_orb.py --help for exact flags)
uv run python scripts/tune_orb.py --help

# Single-symbol parameter sweep (existing)
uv run python scripts/sweep_orb.py --symbol AMD --start 2025-11-15 --end 2026-05-13 \
    --param atr_stop_multiplier --values 1.0,1.5,2.0,2.5
```

### Key reports written this session

All under `data/backtests/`:
- `tune-20260514T165435Z/` — pre-fix grid; documents the inert-filter discovery.
- `tune-20260514T171307Z/` — post-fix grid; 5/6 prior survivors now have
  tunable both-half configs.
- `screen-20260514T17{19,21,22,23}*` — wider-universe pipeline stages.
- `screen-20260514T17303*` and `T1732*` — non-tech diversifier stages.

### Caveats and reminders

- Historical backtest results only, never a forecast (CLAUDE.md rule).
- Risk-officer approval was scoped: paper-only, that session only. Any
  symbol change, cap change, or `execution/`/`risk/` change requires re-review.
- `data/KILL` file removed at end of session; `data/ops/unreconciled/` is clean;
  audit log from today's smoke test at `data/ops/logs/20260514T174801Z.jsonl`.

---

## Where we left off (2026-05-14, after risk-officer round 5 + trade visibility)

The infrastructure is **APPROVED for paper deployment** after five rounds of
risk-officer review. Trade-by-trade visibility is now in place (live
ENTRY/EXIT lines + end-of-session table; matching backtest output; parameter
sweep CLI). **123/123 tests pass.**

### Quick commands cheat sheet

```bash
# Backtest with trade table at end
uv run python scripts/run_backtest.py --symbol SPY --start 2026-05-07 --end 2026-05-13

# Parameter sweep — any ORB config field over a list of values
uv run python scripts/sweep_orb.py --symbol SPY --start 2026-05-07 --end 2026-05-13 \
    --param min_range_atr_multiplier --values 0.3,0.5,1.0,2.0,5.0
# (also accepts: atr_stop_multiplier, take_r_multiple, target_size_pct,
#  opening_range_minutes, atr_period_minutes)

# Paper trade live — prints ENTRY/EXIT lines on stdout as decisions happen
uv run python scripts/run_paper.py --symbols SPY
# New flag: --force-unreconciled (only after manually reconciling a sentinel)
```

### Round-3/4/5 fixes (all in `src/trading_bot/execution/session.py`)

- **Drift threshold 0.50 → 0.20** (`_EQUITY_PERSISTENCE_MAX_DRIFT_PCT`). A real
  intra-day restart produces drift well under 5%; 20% is a generous-but-not-loose
  ceiling that catches tampering, cross-account restores, and out-of-band
  market moves where rebasing is the correct response anyway.
- **Sentinel filename collision-safety**. `_write_unreconciled_sentinel` now
  appends `_<n>` if the target path exists, never overwriting an earlier
  unreconciled record under same-second restarts.
- **Atomic sentinel write**. Symmetric with `_save_session_start_equity`:
  write to `<path>.tmp`, then `os.replace`. Failure-mode difference vs. equity
  persistence is intentional — equity-persist swallows OSError (optimization);
  sentinel write **re-raises** because the next-start refusal depends on the
  file existing.
- **`run_id` assertion** in the flatten block — replaced `sess.run_id or "unknown"`
  with `assert sess.run_id is not None`, so a future regression that drops the
  seeding fails loudly instead of writing a `UNRECONCILED_unknown.json`.
- **Documented mid-sleep `BrokerError` swallow** trade-off in the section below.

### New: trade-by-trade visibility (2026-05-14, late session)

Both the backtest CLI and the paper session loop now produce identical
console output for trade activity.

- **`src/trading_bot/ops/trade_table.py`** — shared Unicode box-drawing
  renderer (`render_trade_table`, `render_summary_line`, `render_trade_event`).
  Pure formatting, no I/O.
- **`scripts/run_backtest.py`** prints the trade table + summary line after
  the metrics block.
- **`scripts/sweep_orb.py`** — sweeps a single ORB config field over a list
  of values and prints a metrics-comparison table. Uses the same Parquet bar
  cache as `run_backtest.py`.
- **`SessionState.trades: list[Trade]`** and `SessionState.open_entries: dict`
  added. `_handle_entry_signal` records on submission; `_handle_flat_signal`
  pairs on close with `exit_reason="signal_flip"`; `_flatten_all` pairs on
  session-end close with `exit_reason="session_end"`. Only round-trips this
  session opened are tracked — pre-existing positions get flattened but not
  added to the trade table.
- **Live console lines** (stdout, not log): `[HH:MM:SS] ENTRY long SPY 13 @ 735.26 stop=730.10 take=745.58`
  and `[HH:MM:SS] EXIT long SPY 13 @ 734.69 pnl=−7.53 reason=signal flip`.
- Audit JSONL still gets `trade entry` / `trade exit` records via `logger.info`
  with the same fields in structured form.

### Strategy roadmap — what I'd do next, and what I'd hold off on

**Empirical state of ORB.** Over SPY 2026-05-07..2026-05-13 the current
parameterization produced 6 trades, 1 winner (16.7%), profit factor 0.33,
−$23.25 on $100k starting cash. A sweep of `min_range_atr_multiplier`
across 0.2–12.0 showed the filter doesn't bite until ~5.0× and then only
cuts the one winner — i.e. that knob is not the problem.

**Recommendation: do not add additional strategies yet.** Reasons:

1. The infrastructure is rock-solid (risk-officer signed off after 5
   rounds), but only one strategy is wired up. Adding more strategies
   before the first one has positive expectancy multiplies the
   debugging/eval surface without proof of edge. Adding two losing
   strategies just gives you two losing strategies.
2. The `Strategy` Protocol (`src/trading_bot/strategy/base.py`) already
   supports drop-in additions. Future-you can add a second strategy in
   an afternoon once the first is profitable. **The work is cheap to
   defer, expensive to do prematurely.**
3. Capital allocation across strategies is a meaningful design choice
   (equal-weight? regime-conditional? risk-parity?). It deserves
   thinking, not retrofitting under a deadline.

**Better next experiments inside ORB itself** (in roughly this order):

1. **`atr_stop_multiplier` sweep** (currently 1.5). Most losses in the
   backtest are fast stop-outs — May 12 trades 5 and 6 closed in 1 and 4
   minutes respectively. Try `--values 1.5,2.0,2.5,3.0,3.5` and see if a
   wider stop preserves the take-profit hits without inflating max loss.
2. **`take_r_multiple` sweep** (currently 2.0). Only 1 of 6 trades
   reached take. Try `--values 1.0,1.5,2.0,2.5`. The cost: lower-R takes
   reduce avg win; the benefit: more hits. Plot the trade-off.
3. **Time-of-day filter.** Trade 6 (May 12 15:16 ET, 45min before close)
   is exactly the late-day fade ORB historically does poorly on. Add a
   `latest_entry_et` config field that suppresses entries after, say,
   12:00 ET. One-line change to the strategy's signal loop.
4. **Multi-symbol** — different from multi-strategy! `--symbols SPY,QQQ,AAPL`
   already works today; the session loop iterates symbols and the strategy
   is symbol-agnostic. Run a multi-symbol backtest to see correlation
   structure and whether the daily-loss cap is the binding constraint.
   Adds diversification without architectural change.
5. **Out-of-sample test.** Whatever parameters survive (1)-(3) on the
   May 7-13 window, validate on a separate window (e.g. April or an
   older slice you haven't peeked at). If they don't survive OOS, the
   "best" in-sample params are overfit — strategist agent will push
   back on this exact pattern.

**When *would* you add a second strategy?** Once ORB shows positive
expectancy in OOS (positive total_return, profit_factor > 1.2, win_rate
sustainable for the R-multiple) over at least 30 trading days. Then a
mean-reversion or VWAP-fade strategy gives you genuine diversification of
edge. Adding before that is breadth-over-depth: optimizing the wrong axis.

### Known trade-offs and live-mode follow-ups

Carry-forward list — all paper-acceptable, all required for the eventual
live-mode review:

- **Mid-sleep `BrokerError` is silently swallowed** in `_check_daily_loss_mid_sleep`
  (`session.py` ~line 217-219). Real outage is reconciled by top-of-loop's
  `consecutive_failures` within one poll cycle; kill_file/kill_env remain
  live every 5s. A *flapping* connection that recovers each top-of-loop
  would silently mask mid-sleep blindness — accepted gap for paper. **Live
  fix:** add `consecutive_midsleep_failures` counter on `SessionState` and
  halt at N (suggest 3 ≈ 45s).
- **HMAC + `created_at` on persistence file**. Current tamper resistance
  is drift-based (20%). For live, add an explicit UTC `created_at` field
  with a freshness check (reject if older than ~18h or in the future)
  and/or HMAC the payload with a key from `.env`.
- **Two-key reconcile override**. `force_ignore_unreconciled=True` alone is
  enough for paper. For live, require it *plus* `RECONCILED_BY=<operator-id>`
  env var *plus* manual sentinel file deletion before restart.
- **Bracket order limitation**. `AlpacaPaperBroker.submit_order` currently
  drops `stop_price` and `take_price` on the floor — strategy-computed
  stop/take are NOT sent to the broker as bracket orders. In current
  design this is fine because the strategy emits flat signals to close
  positions and `_flatten_all` covers session end. But a bot disconnect
  between flat-signal emissions leaves positions unbracketed. For live,
  either send brackets via `OrderClass.BRACKET` in `MarketOrderRequest`,
  or polish the assumption that strategy flat signals are reliable
  exits. Not a paper blocker because losses are paper.
- **Configurable daily-loss-mid-sleep cadence** — currently a module
  constant (`_DAILY_LOSS_CHECK_EVERY_N_CHUNKS = 3`). Promote to
  `SessionConfig` when live-mode tuning starts.
- **Session-state file rotation** — `data/ops/session_state/<ET-date>.json`
  accumulates one file per trading day. Add a prune helper in `ops/`. Not
  a safety issue.

---

## Where we left off (2026-05-14, after risk-officer round 2)

All four risk-officer follow-ups from round 2 are implemented. Paper now
simulates the eventual live-mode safety surface. **112/112 tests pass.**
(Subsequent trade-tracking work pushed this to 123/123 — see top of file.)

### Round-2 changes (all in `src/trading_bot/execution/session.py`)

1. **Atomic equity persistence** — `_save_session_start_equity` writes to
   `<path>.tmp` and `os.replace`s into place. A crash mid-write cannot leave
   a truncated JSON.
2. **Baseline tamper resistance** — on first poll after loading a persisted
   baseline, compare to `broker.get_account().equity`. If drift exceeds 50%
   (`_EQUITY_PERSISTENCE_MAX_DRIFT_PCT`), log ERROR, rebase to current,
   rewrite the file.
3. **Unreconciled sentinel file** — partial flatten failures (one or more
   `positions_failed`) now write `data/ops/unreconciled/UNRECONCILED_<run_id>.json`
   AND log at ERROR. `run_session` refuses to start if any sentinel is
   present; `SessionConfig.force_ignore_unreconciled=True` (CLI flag
   `--force-unreconciled`) overrides for operators who reconciled manually.
4. **Mid-sleep daily-loss check** — `_sleep_with_kill_check` renamed to
   `_sleep_with_safety_checks`. Inside the sleep window, every ~15s (3 chunks
   × 5s), refreshes account equity and runs `check_daily_loss`. Worst-case
   detection of a runaway loss drops from `poll_interval_seconds` (60s
   default) to ~15s. Transient `BrokerError`s during these refreshes are
   logged and skipped — the top-of-loop will retry and bump
   `consecutive_failures` if real.

### Known trade-offs (paper-acceptable; live work needed)

- **Mid-sleep `BrokerError` is silently swallowed.**
  `_check_daily_loss_mid_sleep` (`session.py` ~line 212-219) logs the
  exception and returns `None` rather than escalating. Rationale: the
  top-of-loop on the next iteration will retry `broker.get_account` and
  bump `consecutive_failures` if the outage is real, so a real failure is
  still detected within ~one poll cycle; meanwhile, kill-file and kill-env
  remain live every 5s. A *flapping* connection that recovers each
  top-of-loop would silently mask mid-sleep blindness indefinitely — this
  is the accepted gap. **For live, add a `consecutive_midsleep_failures`
  counter on `SessionState` and halt when it crosses N (suggest 3 ≈ 45s).**

### Open follow-ups (not blocking paper; required for live-mode review)

- **HMAC + `created_at` on persistence file.** Current tamper resistance is
  drift-based (now 20% — tightened from 50% in round 3). For live, add an
  explicit UTC `created_at` field with a freshness check (reject if older
  than ~18h or in the future) and/or HMAC the payload with a key from `.env`.
- **Mid-sleep refresh-failure counter** (see trade-off above) — hard
  blocker for live per round-2 risk officer.
- **Two-key reconcile override.** `force_ignore_unreconciled=True` alone is
  enough for paper. For live, require it *plus* `RECONCILED_BY=<operator-id>`
  env var *plus* manual sentinel file deletion before restart.
- **Configurable daily-loss-mid-sleep cadence** — currently a module
  constant (`_DAILY_LOSS_CHECK_EVERY_N_CHUNKS = 3`). Promote to
  `SessionConfig` when live-mode tuning starts.
- **Session-state file rotation** — `data/ops/session_state/<ET-date>.json`
  accumulates one file per trading day. Add a `prune older than N days`
  helper in `ops/` or a scheduled task. Not a safety issue.

---

## Where we left off (2026-05-13, evening session)

### What exists and works

Full paper-trading bot, end to end, **risk-officer approved for paper**:

| Module | Status | Tests |
|---|---|---|
| `contracts.py` | shared types + DataFrame shape conventions | — |
| `data/` | Alpaca historical fetch + Parquet cache + bar validation | 20 |
| `strategy/orb/` | opening-range breakout, pure-function | 8 |
| `risk/` | sizing (Decimal), validate_order, kill switches (file/env/loss) | 38 |
| `backtest/` | bar-by-bar engine, honest fills, Sharpe + profit factor + reconcile() | 12 |
| `execution/` | `BrokerClient` Protocol, `AlpacaPaperBroker` (paper=True hardcoded), session run loop | 17 |
| `ops/logging.py` | JSONL structured logging | — |
| `ops/trade_table.py` | Unicode trade table + live event lines | 7 |
| `scripts/run_paper.py` | CLI: live paper trading with `TRADING_MODE=paper` gate | — |
| `scripts/run_backtest.py` | CLI: historical backtest with Markdown report + CSV + trade table | — |
| `scripts/sweep_orb.py` | CLI: sweep one ORB config field over a list of values | — |

**Total: 123/123 tests passing as of 2026-05-14** (was 95 at original handover; the
delta is round-1 through round-5 safety follow-ups + the trade-tracking work).
Two end-to-end smoke tests succeeded against live Alpaca: `scripts/hello_market.py`
(data fetch) and `scripts/run_backtest.py` (full pipeline on SPY 2026-05-08..05-12).

### How to actually run things

```bash
# One-time setup
cp .env.example .env  # fill in ALPACA_API_KEY + ALPACA_API_SECRET
uv sync --extra dev

# Paper trade live (must be during US market hours: 15:30–22:00 CET roughly)
uv run python scripts/run_paper.py --symbols SPY

# Backtest against historical data (any time)
uv run python scripts/run_backtest.py --symbol SPY --start 2026-05-01 --end 2026-05-12

# Kill switches (in priority order, all halt the loop within ~60s)
touch data/ops/KILL                        # file-based
TRADING_KILL=1 uv run ...                  # env-based
# Or just Ctrl+C — handler flattens then exits
```

Audit logs land in `data/ops/logs/<run-id>.jsonl`. Backtest reports in `data/backtests/<run-id>/`.

---

## Outstanding work (the next session should start here)

The risk-officer approved paper trading but flagged **three non-blocking follow-ups** that need doing before any live-mode conversation. None are urgent for paper:

1. **Narrow `_flatten_all` exceptions** — `src/trading_bot/execution/session.py:225-241` still uses bare `except Exception` per call. Replace with `except BrokerError`, return an aggregate `FlattenResult` (cancel_ok, positions_closed, positions_failed).

2. **Persist `session_start_equity` to disk** — currently set on first poll. If the bot crashes and restarts mid-day, the daily-loss baseline rebases to the (already-lower) equity, defeating the cap. Write `{"session_start_equity": "X"}` to `data/ops/session_state/<ET-date>.json`; load on startup if file exists.

3. **Tighten kill-switch latency** — checks happen once per `poll_interval_seconds` (default 60). Worst-case kill-file detection latency is 60s. Replace `sleep_fn(60)` with a chunked `_sleep_with_kill_check` that polls kill_file/kill_env every ~5s.

Task ID #13 in the task list captures these. They were started but **not committed** — user interrupted the work to ask for this handover.

---

## Key decisions made this session

### Architectural

- **`BrokerClient` Protocol** introduced in `execution/broker.py` so swapping brokers (e.g., IBKR for European markets) is a class swap, not a rewrite.
- **`paper=True` hardcoded** in `AlpacaPaperBroker.__init__` — not a constructor flag. `run_session` further refuses any broker where `is_paper` is False. Live mode requires a separate `AlpacaLiveBroker` class that doesn't exist yet, plus a second confirmation gate beyond `TRADING_MODE=live`.
- **`realized_pnl_today` is total daily P&L** (realized + unrealized), computed as `current_equity − session_start_equity`. Documented compromise: conservative for paper (unrealized drawdowns trip the daily-loss cap too), revisit for live where strict realized accounting may be wanted alongside a separate unrealized-drawdown cap.

### Investigated and rejected (for now)

**Multi-venue expansion to Xetra / LSE / Euronext via Alpaca EU** — user asked about this. Researched their April 2026 European launch and confirmed:

- Alpaca EU is the **Broker API model** (B2B fintech infrastructure for building brokerage apps), NOT the retail Trading API. The acquired entity is WealthKernel, rebranded Alpaca Europe.
- An individual algo trader cannot sign up for Alpaca EU paper the way they can for US paper.
- For retail-accessible European market access, **Interactive Brokers** (already noted in original handover as the backup) remains the path. Adds: `ib_async` dependency, second broker adapter, FX/currency awareness in risk module.

User decision: **stay Alpaca US for now**, do the risk fixes first, defer multi-venue work.

---

## Important context that isn't in CLAUDE.md or the code

### Sub-agent Write permission

In this harness configuration, **sub-agents are systematically denied Write tool access**. When the user asked to "build using dedicated agents", every sub-agent (data-engineer, strategist, general-purpose for risk) got blocked on every Write call. Workaround: build in the main session, then use sub-agents for the **review** pass (read-only — works fine). Don't waste cycles dispatching builder sub-agents until this is resolved.

The review pass via sub-agents was extremely valuable — risk-officer caught the missing `realized_pnl_today` plumbing that would have made the daily-loss kill switch dead code in production.

### Strategy quality

The ORB strategy is a **baseline**, not a tested edge. A 5-session backtest on SPY (May 8-12) showed 6 trades, 16.7% win rate, profit factor 0.33. The infrastructure is solid; the alpha is not. Future sessions should focus on:

- Per-session feature engineering (volume regime, prior-day range, gap size)
- The `take_r_multiple` (currently 2.0) and `min_range_atr_multiplier` (0.5) are arbitrary — neither has any mechanistic justification beyond "reasonable defaults"
- Strategist agent will push back on backtest-driven param tweaks (overfit yellow flag)

### Quirks worth knowing

- `FakeBroker` in `tests/test_execution/fakes.py` filters bars by time range — tests use `now_fn` to control which signals the strategy sees, since the strategy emits all session signals at once.
- `BacktestResult.reconcile()` enforces `initial_cash + sum(trade.pnl) == final_equity`. Use it as a sanity check in any new backtest test.
- Risk-officer's verdict pattern: they will FAIL items that look fine if logging/audit trail isn't there. Build with structured logs from the start.

---

## Quick orientation pointers

- **Project rules:** `CLAUDE.md` (paper-only default, Decimal for money, UTC internally, no forecasts)
- **Agents:** `.claude/agents/{data-engineer,strategist,backtester,risk-officer,ops,code-reviewer,tester}.md`
- **Skills:** `.claude/skills/{backtest,deploy-paper,replay-trade}/SKILL.md`
- **Memory index (auto-loaded):** `~/.claude/projects/-home-fts-Project/memory/MEMORY.md`
- **Shared types:** `src/trading_bot/contracts.py` — start here when adding a new module
- **Run-loop entry point:** `src/trading_bot/execution/session.py:run_session`
- **Audit logs:** `data/ops/logs/` (gitignored)
- **Backtest reports:** `data/backtests/<run-id>/{report.md, trades.csv, equity_curve.csv}`

---

## Suggested first move in the next session

> "Pick up the trading-bot handover. Run the three risk-officer follow-ups (task #13)."

The fixes are scoped, the test scaffolding is in place, and finishing them gives a clean foundation for whatever comes next — strategy iteration, IBKR adapter, or live-mode design.

---

## 2026-05-21 cleanup

The VWAP and RRB strategies (both failed their acceptance bar earlier in
the project) were removed in full: strategy packages, configs, tests,
backtest scripts, and the two `_*_ablation.py` probes. The active
strategy set is now exactly **ORB + Pullback (midday) + Insider**, all
three composed via `CompositeStrategy` in `scripts/run_paper.py`.

Two new subagents were added: `code-reviewer` (general code-quality
review for paths outside `execution/` and `risk/`) and `tester` (pytest
infrastructure ownership). Risk-officer still owns execution/risk veto.

No public contracts changed — Strategy Protocol, SignalsFrame schema,
BrokerClient, SessionConfig, and the backtest engine are all
byte-identical. The pre-existing failing test
`tests/test_strategy/test_orb.py::test_filter_audit_log_emits_or_atr_and_cold_start`
remains failing and is still owned by the strategist.
