from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.backtest.replay_engine import ReplayBar, ReplayEngine, ReplaySignal


def _bars(n=20, start_price=Decimal("100"), step=Decimal("0"), start=None):
    start = start or dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    out = []
    price = start_price
    for i in range(n):
        out.append(
            ReplayBar(
                timestamp=start + dt.timedelta(minutes=15 * i),
                open=price, high=price + 1, low=price - 1, close=price,
            )
        )
        price += step
    return out


def test_replay_no_look_ahead_only_uses_bars_after_signal_index():
    """The bar the signal fires ON is never itself used for the fill —
    only bars strictly after it."""
    bars = _bars(5)
    engine = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    signal = ReplaySignal(
        bar_index=4,  # the LAST bar — there is nothing after it
        symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"),
        take_profit_2=Decimal("120"), expires_at=bars[-1].timestamp + dt.timedelta(hours=10),
        quantity=Decimal("1"),
    )
    report = engine.run([signal])
    assert report.trades[0].exit_reason == "NO_FILL"  # no future bars exist to simulate against


def test_replay_take_profit_1_hit():
    bars = _bars(10, start_price=Decimal("100"))
    # bump price up sharply on bar 5 to trigger TP1
    bars = list(bars)
    bars[5] = ReplayBar(timestamp=bars[5].timestamp, open=Decimal("100"), high=Decimal("112"), low=Decimal("99"), close=Decimal("111"))
    engine = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    signal = ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"), take_profit_2=Decimal("120"),
        expires_at=bars[-1].timestamp + dt.timedelta(hours=1), quantity=Decimal("1"),
    )
    report = engine.run([signal])
    trade = report.trades[0]
    assert trade.exit_reason == "TP1"
    assert trade.pnl == Decimal("10")  # 110 - 100, no fees
    assert trade.r_multiple == Decimal("2.0000")  # risk=5, reward=10 -> R=2


def test_replay_stop_loss_hit_before_target():
    bars = _bars(10, start_price=Decimal("100"))
    bars = list(bars)
    bars[2] = ReplayBar(timestamp=bars[2].timestamp, open=Decimal("100"), high=Decimal("101"), low=Decimal("93"), close=Decimal("94"))
    engine = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    signal = ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"), take_profit_2=Decimal("120"),
        expires_at=bars[-1].timestamp + dt.timedelta(hours=1), quantity=Decimal("1"),
    )
    report = engine.run([signal])
    trade = report.trades[0]
    assert trade.exit_reason == "STOPPED"
    assert trade.pnl < 0


def test_replay_same_bar_stop_and_target_prefers_conservative_stop():
    bars = _bars(5, start_price=Decimal("100"))
    bars = list(bars)
    # A single wild bar that touches both the stop AND both targets.
    bars[1] = ReplayBar(timestamp=bars[1].timestamp, open=Decimal("100"), high=Decimal("125"), low=Decimal("90"), close=Decimal("100"))
    engine = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    signal = ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"), take_profit_2=Decimal("120"),
        expires_at=bars[-1].timestamp + dt.timedelta(hours=1), quantity=Decimal("1"),
    )
    report = engine.run([signal])
    assert report.trades[0].exit_reason == "STOPPED"  # conservative: stop wins ambiguity


def test_replay_setup_expiry():
    bars = _bars(10, start_price=Decimal("100"))  # flat — never hits SL or TP
    engine = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    signal = ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"), take_profit_2=Decimal("120"),
        expires_at=bars[3].timestamp, quantity=Decimal("1"),
    )
    report = engine.run([signal])
    assert report.trades[0].exit_reason == "EXPIRED"


def test_replay_short_direction_pnl():
    bars = _bars(10, start_price=Decimal("100"))
    bars = list(bars)
    bars[3] = ReplayBar(timestamp=bars[3].timestamp, open=Decimal("100"), high=Decimal("101"), low=Decimal("88"), close=Decimal("89"))
    engine = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    signal = ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="breakout_retest", direction="SHORT", market_regime="TREND_DOWN",
        entry_price=Decimal("100"), stop_loss=Decimal("105"), take_profit_1=Decimal("90"), take_profit_2=Decimal("80"),
        expires_at=bars[-1].timestamp + dt.timedelta(hours=1), quantity=Decimal("1"),
    )
    report = engine.run([signal])
    trade = report.trades[0]
    assert trade.exit_reason == "TP1"
    assert trade.pnl == Decimal("10")  # 100 - 90


def test_replay_fees_reduce_pnl():
    bars = _bars(10, start_price=Decimal("100"))
    bars = list(bars)
    bars[5] = ReplayBar(timestamp=bars[5].timestamp, open=Decimal("100"), high=Decimal("112"), low=Decimal("99"), close=Decimal("111"))
    engine_no_fee = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    engine_with_fee = ReplayEngine(bars, fee_rate=Decimal("0.001"), slippage=Decimal("0"))
    signal = ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"), take_profit_2=Decimal("120"),
        expires_at=bars[-1].timestamp + dt.timedelta(hours=1), quantity=Decimal("1"),
    )
    r1 = engine_no_fee.run([signal]).trades[0]
    r2 = engine_with_fee.run([signal]).trades[0]
    assert r2.pnl < r1.pnl


def test_replay_report_splits_in_sample_out_of_sample_walk_forward():
    bars = _bars(10, start_price=Decimal("100"))
    bars = list(bars)
    bars[5] = ReplayBar(timestamp=bars[5].timestamp, open=Decimal("100"), high=Decimal("112"), low=Decimal("99"), close=Decimal("111"))
    engine = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    make_signal = lambda sample: ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"), take_profit_2=Decimal("120"),
        expires_at=bars[-1].timestamp + dt.timedelta(hours=1), quantity=Decimal("1"), sample=sample,
    )
    report = engine.run([make_signal("in_sample"), make_signal("out_of_sample"), make_signal("walk_forward")])
    full = report.full_report()
    assert full["in_sample"]["trades"] == 1
    assert full["out_of_sample"]["trades"] == 1
    assert full["walk_forward"]["trades"] == 1
    assert full["combined"]["trades"] == 3


def test_replay_win_rate_and_profit_factor():
    bars = _bars(10, start_price=Decimal("100"))
    win_bars = list(bars)
    win_bars[5] = ReplayBar(timestamp=bars[5].timestamp, open=Decimal("100"), high=Decimal("112"), low=Decimal("99"), close=Decimal("111"))
    loss_bars = list(bars)
    loss_bars[2] = ReplayBar(timestamp=bars[2].timestamp, open=Decimal("100"), high=Decimal("101"), low=Decimal("93"), close=Decimal("94"))

    win_signal = ReplaySignal(
        bar_index=0, symbol="BTCUSDT", setup_name="trend_pullback", direction="LONG", market_regime="TREND_UP",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit_1=Decimal("110"), take_profit_2=Decimal("120"),
        expires_at=bars[-1].timestamp + dt.timedelta(hours=1), quantity=Decimal("1"),
    )
    win_report = ReplayEngine(win_bars, fee_rate=Decimal("0"), slippage=Decimal("0")).run([win_signal])
    loss_report = ReplayEngine(loss_bars, fee_rate=Decimal("0"), slippage=Decimal("0")).run([win_signal])

    combined = ReplayEngine(bars, fee_rate=Decimal("0"), slippage=Decimal("0"))
    combined.bars = bars  # unused; just demonstrating separate reports combine cleanly
    all_trades = win_report.trades + loss_report.trades
    from tradingview_mcp.core.backtest.replay_engine import ReplayReport

    merged = ReplayReport(trades=all_trades)
    summary = merged.summary()
    assert summary["trades"] == 2
    assert Decimal(summary["win_rate"]) == Decimal("0.5")
    assert summary["profit_factor"] is not None


def test_no_auto_optimizer_exists_in_replay_module():
    """The plan explicitly forbids an auto-apply-best-params loop. Assert
    the module surface has no such function name."""
    import tradingview_mcp.core.backtest.replay_engine as mod

    forbidden_substrings = ("optimize", "auto_tune", "apply_best", "best_params")
    public_names = [n for n in dir(mod) if not n.startswith("_")]
    for name in public_names:
        lowered = name.lower()
        assert not any(f in lowered for f in forbidden_substrings), f"found forbidden auto-optimizer-like name: {name}"
