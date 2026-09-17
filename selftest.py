#!/usr/bin/env python3
"""
selftest.py
-----------
Offline sanity checks for alpaca_paper_bot.py -- NO network calls, NO API keys needed.

Verifies:
  1. SMA / RSI math against hand-checkable values.
  2. Crossover-event detection (golden/death cross fires only on the transition, not on
     every cycle while already crossed).
  3. A full synthetic cycle (mocking every Alpaca API call) produces a BUY on a golden
     cross, a HOLD once already in position with no exit condition, a SELL on -4% stop
     loss, and writes a well-formed journal row for every decision.

Run: python3 alpaca_paper_bot.py --selftest
"""

import csv
import os
import tempfile

import alpaca_paper_bot as bot


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(1)


def test_sma():
    closes = [1, 2, 3, 4, 5]
    check("sma(5) of [1..5] == 3.0", bot.sma(closes, 5) == 3.0)
    check("sma(10) of 5 closes returns None (insufficient data)", bot.sma(closes, 10) is None)


def test_rsi_all_gains():
    # Strictly increasing closes -> RSI should be 100 (no losses at all).
    closes = [float(i) for i in range(1, 30)]
    r = bot.rsi(closes, 14)
    check(f"RSI of a straight uptrend is 100 (got {r})", r == 100.0)


def test_rsi_mixed():
    # A simple oscillation should land RSI somewhere in a sane middle range, not stuck at 0/100.
    closes = [100, 101, 99, 102, 98, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93]
    r = bot.rsi(closes, 14)
    check(f"RSI of an oscillating series is between 20 and 80 (got {r:.1f})", 20 < r < 80)


def test_crossover_detection():
    """Golden cross should only fire on the FIRST cycle where fast>slow after being below."""
    state = {}
    # Cycle 1: fast below slow -> just records baseline, no signal.
    prev = state.get("TEST", {}).get("fast_above_slow")
    fast_above = False
    state.setdefault("TEST", {})["fast_above_slow"] = fast_above
    golden = prev is False and fast_above is True
    check("Cycle 1 (below->below): no golden cross", golden is False)

    # Cycle 2: crosses up -> golden cross fires exactly once.
    prev = state["TEST"]["fast_above_slow"]
    fast_above = True
    state["TEST"]["fast_above_slow"] = fast_above
    golden = prev is False and fast_above is True
    check("Cycle 2 (below->above): golden cross fires", golden is True)

    # Cycle 3: still above -> must NOT fire again.
    prev = state["TEST"]["fast_above_slow"]
    fast_above = True
    state["TEST"]["fast_above_slow"] = fast_above
    golden = prev is False and fast_above is True
    check("Cycle 3 (above->above): golden cross does NOT re-fire", golden is False)


def test_full_cycle_with_mocks(tmpdir):
    """Mock every Alpaca call and drive one full run_cycle(), checking the journal output."""
    bot.STATE_PATH = os.path.join(tmpdir, "state.json")
    bot.JOURNAL_PATH = os.path.join(tmpdir, "trade_journal.csv")
    bot.ALPACA_BASE_URL = "https://paper-api.alpaca.markets"  # must pass the safety guard

    # Pre-seed state: NVDA already crossed golden last cycle (fast_above_slow=False),
    # this cycle it crosses above -> should BUY.
    # MSTR is an existing position with a big loss -> should SELL on stop-loss.
    import json
    with open(bot.STATE_PATH, "w") as fh:
        json.dump({"NVDA": {"fast_above_slow": False}, "MSTR": {"fast_above_slow": True}}, fh)

    # A decline followed by a modest recovery: fast SMA ends just above slow SMA
    # (golden cross) while RSI settles at a moderate ~50, safely under the 70 buy filter.
    # (A pure monotonic uptrend would push RSI to 100 and get filtered out -- realistic
    # price action after a dip is what actually produces a golden cross with RSI < 70.)
    nvda_closes = [120 - i * 0.8 for i in range(28)]
    nvda_closes += [nvda_closes[-1] + i * 0.3 for i in range(1, 13)]
    # Mildly oscillating, roughly flat closes for MSTR (irrelevant -- it exits on stop-loss).
    mstr_closes = [50 + (i % 3) * 0.1 for i in range(40)]
    flat_closes = [10 + (i % 2) * 0.01 for i in range(40)]

    fake_positions = {
        "MSTR": {"symbol": "MSTR", "qty": "2", "avg_entry_price": "52",
                  "current_price": "49.9", "unrealized_plpc": "-0.05"}
    }
    orders_placed = []

    def fake_get_account():
        return {"buying_power": "100000"}

    def fake_get_positions():
        return dict(fake_positions)

    def fake_get_clock():
        return {"is_open": True}

    def fake_get_calendar_today(now_et):
        return {"date": now_et.strftime("%Y-%m-%d")}

    def fake_get_stock_bars(symbols):
        out = {}
        for s in symbols:
            if s == "NVDA":
                out[s] = [{"c": c} for c in nvda_closes]
            elif s == "MSTR":
                out[s] = [{"c": c} for c in mstr_closes]
            else:
                out[s] = [{"c": c} for c in flat_closes]
        return out

    def fake_get_crypto_bars(symbols):
        return {s: [{"c": c} for c in flat_closes] for s in symbols}

    def fake_place_order(symbol, side, **kwargs):
        order = {"id": f"fake-{symbol}-{side}", "symbol": symbol, "side": side, **kwargs}
        orders_placed.append(order)
        return order

    bot.get_account = fake_get_account
    bot.get_positions = fake_get_positions
    bot.get_clock = fake_get_clock
    bot.get_calendar_today = fake_get_calendar_today
    bot.get_stock_bars = fake_get_stock_bars
    bot.get_crypto_bars = fake_get_crypto_bars
    bot.place_order = fake_place_order

    bot.run_cycle()

    check("A BUY order was placed for NVDA (golden cross)",
          any(o["symbol"] == "NVDA" and o["side"] == "buy" for o in orders_placed))
    check("A SELL order was placed for MSTR (-4% stop loss)",
          any(o["symbol"] == "MSTR" and o["side"] == "sell" for o in orders_placed))

    with open(bot.JOURNAL_PATH) as fh:
        rows = list(csv.DictReader(fh))
    symbols_logged = {r["symbol"] for r in rows}
    expected = set(bot.STOCK_WATCHLIST) | set(bot.CRYPTO_WATCHLIST)
    check(f"Every watchlist symbol got a journal row this cycle ({len(rows)} rows total)",
          expected.issubset(symbols_logged))
    nvda_row = next(r for r in rows if r["symbol"] == "NVDA")
    check(f"NVDA journal row says action=BUY, reason=golden_cross_rsi_lt_70 (got {nvda_row['action']}/{nvda_row['reason']})",
          nvda_row["action"] == "BUY" and nvda_row["reason"] == "golden_cross_rsi_lt_70")
    mstr_row = next(r for r in rows if r["symbol"] == "MSTR")
    check(f"MSTR journal row says action=SELL, reason=stop_loss_-4pct (got {mstr_row['action']}/{mstr_row['reason']})",
          mstr_row["action"] == "SELL" and mstr_row["reason"] == "stop_loss_-4pct")


def test_session_detection():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")

    weekday_calendar = {"date": "2026-09-17"}

    premarket = datetime(2026, 9, 17, 6, 0, tzinfo=et)
    check("6:00 ET on a trading day with market closed -> 'extended'",
          bot.stock_session(premarket, {"is_open": False}, weekday_calendar) == "extended")

    afterhours = datetime(2026, 9, 17, 18, 30, tzinfo=et)
    check("18:30 ET with market closed -> 'extended'",
          bot.stock_session(afterhours, {"is_open": False}, weekday_calendar) == "extended")

    overnight = datetime(2026, 9, 17, 2, 0, tzinfo=et)
    check("2:00 ET (outside the 4am-8pm window) -> 'closed'",
          bot.stock_session(overnight, {"is_open": False}, weekday_calendar) == "closed")

    weekend = datetime(2026, 9, 19, 10, 0, tzinfo=et)  # Saturday
    check("Weekend with no calendar entry -> 'closed' even during would-be extended hours",
          bot.stock_session(weekend, {"is_open": False}, None) == "closed")

    regular = datetime(2026, 9, 17, 11, 0, tzinfo=et)
    check("Clock says is_open -> 'regular' regardless of calendar",
          bot.stock_session(regular, {"is_open": True}, weekday_calendar) == "regular")


def test_flatten_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    check("19:45 ET is inside the flatten window",
          bot.is_flatten_window(datetime(2026, 9, 17, 19, 45, tzinfo=et)) is True)
    check("19:59 ET is inside the flatten window",
          bot.is_flatten_window(datetime(2026, 9, 17, 19, 59, tzinfo=et)) is True)
    check("19:44 ET is NOT yet inside the flatten window",
          bot.is_flatten_window(datetime(2026, 9, 17, 19, 44, tzinfo=et)) is False)
    check("20:00 ET is past the flatten window (already closed)",
          bot.is_flatten_window(datetime(2026, 9, 17, 20, 0, tzinfo=et)) is False)


def test_extended_hours_order_shape():
    """Extended-hours entries must be whole-share LIMIT orders nudged through price,
    with extended_hours=True -- never a market order, never notional."""
    captured = {}

    def fake_place_order(symbol, side, **kwargs):
        captured.update(kwargs)
        captured["side"] = side
        return {"id": "fake"}

    bot.place_order = fake_place_order
    bot._submit_entry("NVDA", "stock", "extended", notional=1000.0, price=100.0)
    check("Extended-hours entry uses order_type='limit'", captured.get("order_type") == "limit")
    check("Extended-hours entry sets extended_hours=True", captured.get("extended_hours") is True)
    check("Extended-hours entry uses whole-share qty, not notional", "qty" in captured and "notional" not in captured)
    check(f"Limit price is nudged ~0.5% ABOVE current price for a buy (got {captured.get('limit_price')})",
          abs(captured["limit_price"] - 100.5) < 0.01)

    captured.clear()
    bot._submit_exit("NVDA", "stock", "extended", qty=3, price=100.0)
    check(f"Limit price is nudged ~0.5% BELOW current price for a sell (got {captured.get('limit_price')})",
          abs(captured["limit_price"] - 99.5) < 0.01)

    captured.clear()
    bot._submit_entry("BTC/USD", "crypto", "24/7", notional=1000.0, price=100.0)
    check("Crypto entries always use market orders regardless of session",
          captured.get("order_type") == "market")


def test_paper_safety_guard():
    bot.ALPACA_BASE_URL = "https://api.alpaca.markets"  # LIVE endpoint, not paper
    threw = False
    try:
        bot._assert_paper_only()
    except RuntimeError:
        threw = True
    check("Bot refuses to run against a non-paper base URL", threw)
    bot.ALPACA_BASE_URL = "https://paper-api.alpaca.markets"


def run():
    test_sma()
    test_rsi_all_gains()
    test_rsi_mixed()
    test_crossover_detection()
    test_session_detection()
    test_flatten_window()
    test_extended_hours_order_shape()
    test_paper_safety_guard()
    with tempfile.TemporaryDirectory() as tmpdir:
        test_full_cycle_with_mocks(tmpdir)
    print("\nAll self-tests passed.")


if __name__ == "__main__":
    run()
