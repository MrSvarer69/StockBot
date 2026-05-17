# Handover — trading-bot project

> Snapshot for the next Claude/human session. `CLAUDE.md` has the durable
> project rules; this file captures conversation state and decisions that
> aren't in the codebase.

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
- **Agents:** `.claude/agents/{data-engineer,strategist,backtester,risk-officer,ops}.md`
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
