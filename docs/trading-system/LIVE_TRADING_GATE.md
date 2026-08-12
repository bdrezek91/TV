# Live Trading Gate

**This document is a requirements checklist only. It contains no
implementation, no order-placement code, and authorizes nothing by
itself.** No code in this repository can place a real order — this file
exists so that if live trading is ever considered in the future, there is
an explicit, written bar it must clear first, decided by the user, not by
an AI.

Nothing in Block 1, 2, or 3 moves this bar. `TRADING_MODE` is validated at
process startup to be exactly `"paper"` (see
`core/config/trading_settings.py::TradingSettings._validate_trading_mode`)
and the system refuses to start otherwise — that check is not a suggestion
this document could override, it's a hard `TradingSettingsError`.

## Required before any live-trading implementation work even begins

1. **Minimum paper-trade sample size.** A statistically meaningful number
   of *closed* paper trades per strategy (not merely candidates/signals —
   trades that actually went through entry → exit). As a starting bar:
   at least 100 closed trades per active strategy (`trend_pullback`,
   `breakout_retest`), spanning multiple distinct market regimes (not all
   from one single trending week).

2. **Positive expectancy after costs.** `strategy_performance`'s
   `expectancy` (or the Replay Engine's `expectancy_r`) must be positive
   *after* fees, slippage, and funding — not gross PnL.

3. **Out-of-sample stability.** Performance on data the strategy logic
   was never tuned against (`ReplayReport.summary("out_of_sample")`) must
   not be dramatically worse than in-sample performance. A strategy that
   only "works" in-sample is curve-fit, not validated.

4. **Walk-forward results.** `ReplayReport.summary("walk_forward")` must
   show consistent (not necessarily identical, but not collapsing)
   performance across sequential walk-forward windows.

5. **Performance across regimes.** `by_regime` results must not show the
   strategy is only profitable in exactly one regime it happened to be
   built during — a `TREND_UP`-only edge is not a validated trend-pullback
   strategy.

6. **Acceptable drawdown.** Max drawdown (in R and in account-equity
   terms) must stay within a bound the user explicitly sets and accepts in
   advance, in writing — not decided after the fact once a bad drawdown
   already happened.

7. **Independent Risk Engine audit.** A human (not the AI that built the
   Risk Engine) reviews `core/analysis/risk_engine.py` line by line against
   this document's requirements — no martingale, no risk increase after a
   loss, no averaging down, no duplicate positions, correct position
   sizing — and signs off explicitly.

8. **A kill switch.** A single, tested, always-available mechanism to
   immediately halt all trading (new order submission AND standing
   working orders) that does not depend on any AI or Claude session being
   available or cooperative to trigger.

9. **A withdrawal-disabled API key.** Any real exchange API key used must
   have withdrawal permissions disabled at the exchange level — a
   compromised key must never be able to move funds out, only place/cancel
   orders within account equity.

10. **IP whitelisting.** The API key must be restricted to the VPS's
    static IP (or a short whitelist) at the exchange level.

11. **Manual user approval — every time, explicitly.** No automated
    process (including any Claude routine) may cross from paper to live
    trading on its own. The user must explicitly, manually flip
    `TRADING_MODE` (or whatever future equivalent gate exists) after
    personally reviewing items 1-10 above. An AI recommending "you're
    ready" is not consent; consent is the user's own action.

## What this document deliberately does NOT do

- It does not implement any of the above. There is no kill-switch code,
  no live order-placement client, no live-key handling code anywhere in
  this repository.
- It does not set a specific numeric pass/fail bar for items 1-6 beyond
  the illustrative starting points above — those are the user's business
  decision to finalize once real paper-trading data exists to calibrate
  against.
- It does not get "satisfied" automatically by any test suite, CI job, or
  AI judgment call. Every item above requires the user's own review and
  explicit sign-off.

## Where the data to evaluate this gate comes from

Once Block 1-3 are deployed and have run for a meaningful period:
`strategy_performance` (MCP tool, Block 3) and the Replay Engine
(`core/backtest/replay_engine.py`, run manually / from a future
Block 4+ tool against accumulated real paper-trading history) are the two
sources of the numbers this document's checklist is evaluated against.
Neither currently has enough real data to evaluate anything — this
sandbox has no live VPS, so no real paper trades have ever been executed
against real Bybit prices. This document is therefore purely prospective
at the time of writing.
