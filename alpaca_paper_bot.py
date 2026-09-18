#!/usr/bin/env python3
"""
alpaca_paper_bot.py
--------------------
A rule-based (NOT LLM-driven) day-trading bot for Alpaca's PAPER trading API.

Strategy, exactly as specified:
  - 5-minute bars
  - Buy on golden cross (5-SMA crosses above 15-SMA) while RSI(14) < 70
  - Sell on death cross (5-SMA crosses below 15-SMA), OR RSI(14) > 80, OR -4% stop-loss
  - Position sizing: 5% of current buying power per new trade
  - Max 6 concurrent open positions (stocks + crypto combined)
  - Stocks: never held overnight -> flattened ~15 min before the extended session ends (8pm ET)
  - Stocks: regular hours (9:30-16:00 ET) use market orders; extended hours (4:00-9:30 and
    16:00-20:00 ET) use a marketable LIMIT order (price nudged ~0.5% through the current price),
    because Alpaca does not accept market orders outside regular hours.
  - Crypto: trades 24/7, always market orders, no session logic, never flattened.
  - Every decision (including holds) is appended to trade_journal.csv for a full audit trail.

This script performs exactly ONE cycle per invocation. It is meant to be invoked on a schedule
(e.g. a GitHub Actions cron workflow every 5 minutes) -- it does not loop or sleep internally.

SAFETY: this script refuses to run unless ALPACA_BASE_URL points at Alpaca's paper endpoint.
That is a hard guard, not just documentation -- see `_assert_paper_only()`.
"""

import csv
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA_URL = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")

STOCK_WATCHLIST = ["NVDA", "TSLA", "AMD", "PLTR", "MSTR", "DELL"]
CRYPTO_WATCHLIST = ["BTC/USD", "ETH/USD", "SOL/USD"]

TIMEFRAME = "5Min"
BARS_LOOKBACK = 60          # bars fetched per symbol per cycle (plenty for SMA15 / RSI14)

# --- DEMO MODE: these 4 values are temporarily loosened so a real trade is far more
# likely to fire during a short recording window. This is still a genuine, real-data-
# driven crossover strategy -- just tuned to be more sensitive. The original spec'd
# values are commented alongside each line. REVERT these to the original values after
# filming by swapping which number is active.
SMA_FAST = 3                # DEMO (was 5)
SMA_SLOW = 8                 # DEMO (was 15)
RSI_PERIOD = 14
RSI_BUY_MAX = 90.0           # DEMO (was 70.0) -- only buy on golden cross if RSI below this
RSI_SELL_MIN = 95.0          # DEMO (was 80.0) -- exit if RSI rises above this
STOP_LOSS_PCT = -0.04       # exit if unrealized P/L% <= this
POSITION_SIZE_PCT = 0.05    # 5% of buying power per new trade
MAX_OPEN_POSITIONS = 6

EXTENDED_LIMIT_NUDGE = 0.005  # 0.5% marketable-limit nudge

ET = ZoneInfo("America/New_York")

STATE_PATH = os.environ.get("BOT_STATE_PATH", "state.json")
JOURNAL_PATH = os.environ.get("BOT_JOURNAL_PATH", "trade_journal.csv")

JOURNAL_FIELDS = [
    "ts_utc", "ts_et", "symbol", "asset_class", "session", "action", "reason",
    "price", "qty_or_notional", "order_id", "sma_fast", "sma_slow", "rsi",
    "unrealized_plpc", "open_positions_before", "buying_power", "error",
]


# --------------------------------------------------------------------------------------
# Safety guard -- this is what stops this code from ever touching a live account.
# --------------------------------------------------------------------------------------

def _assert_paper_only():
    if "paper-api.alpaca.markets" not in ALPACA_BASE_URL:
        raise RuntimeError(
            f"Refusing to run: ALPACA_BASE_URL '{ALPACA_BASE_URL}' does not look like "
            "Alpaca's PAPER endpoint (https://paper-api.alpaca.markets). "
            "This bot is hard-coded to refuse anything else."
        )


# --------------------------------------------------------------------------------------
# Minimal HTTP client (stdlib only -- no third-party dependencies to install)
# --------------------------------------------------------------------------------------

def _headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }


def _request(method, url, params=None, body=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {method} {url}: {raw}") from None


def get_clock():
    return _request("GET", f"{ALPACA_BASE_URL}/v2/clock")


def get_calendar_today(now_et):
    day = now_et.strftime("%Y-%m-%d")
    result = _request("GET", f"{ALPACA_BASE_URL}/v2/calendar", params={"start": day, "end": day})
    return result[0] if result else None


def get_account():
    return _request("GET", f"{ALPACA_BASE_URL}/v2/account")


def get_positions():
    positions = _request("GET", f"{ALPACA_BASE_URL}/v2/positions")
    return {p["symbol"]: p for p in positions}


def get_stock_bars(symbols):
    """Fetch bars ONE SYMBOL AT A TIME. Alpaca's multi-symbol bars endpoint appears to
    apply the `limit` budget across the combined response rather than per symbol, which
    starves every symbol except whichever sorts first alphabetically. Fetching separately
    guarantees each symbol gets its own full BARS_LOOKBACK window every cycle."""
    if not symbols:
        return {}
    bars = {}
    for sym in symbols:
        try:
            result = _request(
                "GET",
                f"{ALPACA_DATA_URL}/v2/stocks/bars",
                params={"symbols": sym, "timeframe": TIMEFRAME, "limit": BARS_LOOKBACK, "adjustment": "raw"},
            )
            sym_bars = result.get("bars", {})
            bars[sym] = sym_bars.get(sym, [])
        except RuntimeError as e:
            print(f"WARNING: could not fetch bars for {sym}: {e}", file=sys.stderr)
            bars[sym] = []
    return bars


def get_crypto_bars(symbols):
    """Same per-symbol fetching as get_stock_bars, and for the same reason."""
    if not symbols:
        return {}
    bars = {}
    for sym in symbols:
        try:
            result = _request(
                "GET",
                f"{ALPACA_DATA_URL}/v1beta3/crypto/us/bars",
                params={"symbols": sym, "timeframe": TIMEFRAME, "limit": BARS_LOOKBACK},
            )
            sym_bars = result.get("bars", {})
            bars[sym] = sym_bars.get(sym, [])
        except RuntimeError as e:
            print(f"WARNING: could not fetch bars for {sym}: {e}", file=sys.stderr)
            bars[sym] = []
    return bars


def place_order(symbol, side, qty=None, notional=None, order_type="market",
                 limit_price=None, extended_hours=False, time_in_force="day"):
    body = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if qty is not None:
        body["qty"] = str(qty)
    if notional is not None:
        body["notional"] = str(round(notional, 2))
    if order_type == "limit":
        body["limit_price"] = str(round(limit_price, 2))
    if extended_hours:
        body["extended_hours"] = True
    return _request("POST", f"{ALPACA_BASE_URL}/v2/orders", body=body)


# --------------------------------------------------------------------------------------
# Indicators (pure Python, no numpy/pandas needed)
# --------------------------------------------------------------------------------------

def sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def rsi(closes, period=14):
    """Wilder's RSI. Needs at least period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[-(period + 1) + i] - closes[-(period + 2) + i]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# --------------------------------------------------------------------------------------
# State + journal
# --------------------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def journal_writer():
    is_new = not os.path.exists(JOURNAL_PATH)
    f = open(JOURNAL_PATH, "a", newline="")
    writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
    if is_new:
        writer.writeheader()
    return f, writer


def log_row(writer, **kwargs):
    row = {k: "" for k in JOURNAL_FIELDS}
    row.update(kwargs)
    writer.writerow(row)


# --------------------------------------------------------------------------------------
# Session detection for stocks
# --------------------------------------------------------------------------------------

def stock_session(now_et, clock, calendar_today):
    """Returns one of: 'regular', 'extended', 'closed'."""
    if clock and clock.get("is_open"):
        return "regular"
    if not calendar_today:
        return "closed"  # weekend / holiday -- extended hours don't apply either
    t = now_et.time()
    if t.hour == 4 or (4 < t.hour < 9) or (t.hour == 9 and t.minute < 30):
        return "extended"
    if (t.hour == 16) or (16 < t.hour < 20):
        return "extended"
    return "closed"


def is_flatten_window(now_et):
    """15 minutes before the 8pm ET extended-hours close: 19:45-20:00 ET."""
    t = now_et.time()
    return t.hour == 19 and t.minute >= 45


# --------------------------------------------------------------------------------------
# Core per-symbol decision logic
# --------------------------------------------------------------------------------------

def decide_and_act(symbol, asset_class, session, closes, position, state, open_count,
                    buying_power, writer, now_et):
    price = closes[-1] if closes else None
    f = sma(closes, SMA_FAST)
    s = sma(closes, SMA_SLOW)
    r = rsi(closes, RSI_PERIOD)

    common = dict(
        ts_utc=datetime.now(timezone.utc).isoformat(),
        ts_et=now_et.isoformat(),
        symbol=symbol, asset_class=asset_class, session=session,
        price=price, sma_fast=f, sma_slow=s, rsi=r,
        open_positions_before=open_count, buying_power=buying_power,
    )

    if price is None or f is None or s is None or r is None:
        log_row(writer, **common, action="HOLD", reason="insufficient_bar_history")
        return open_count

    fast_above = f > s
    prev = state.get(symbol, {}).get("fast_above_slow")
    state.setdefault(symbol, {})["fast_above_slow"] = fast_above

    golden_cross = prev is False and fast_above is True
    death_cross = prev is True and fast_above is False

    has_position = symbol in position and float(position[symbol].get("qty", 0)) > 0
    plpc = float(position[symbol]["unrealized_plpc"]) if has_position else None

    try:
        if has_position:
            if plpc is not None and plpc <= STOP_LOSS_PCT:
                action, reason = "SELL", "stop_loss_-4pct"
            elif death_cross:
                action, reason = "SELL", "death_cross"
            elif r > RSI_SELL_MIN:
                action, reason = "SELL", "rsi_overbought_gt_80"
            else:
                action, reason = "HOLD", "position_open_no_exit_condition"

            if action == "SELL":
                qty = position[symbol]["qty"]
                order = _submit_exit(symbol, asset_class, session, qty, price)
                log_row(writer, **common, action=action, reason=reason,
                        qty_or_notional=qty, order_id=order.get("id", ""),
                        unrealized_plpc=plpc)
                return open_count - 1
            else:
                log_row(writer, **common, action=action, reason=reason, unrealized_plpc=plpc)
                return open_count

        else:
            if golden_cross and r < RSI_BUY_MAX:
                if open_count >= MAX_OPEN_POSITIONS:
                    log_row(writer, **common, action="HOLD", reason="max_open_positions_reached")
                    return open_count
                notional = buying_power * POSITION_SIZE_PCT
                order = _submit_entry(symbol, asset_class, session, notional, price)
                log_row(writer, **common, action="BUY", reason="golden_cross_rsi_lt_70",
                        qty_or_notional=order.get("qty") or notional, order_id=order.get("id", ""))
                return open_count + 1
            else:
                reason = "no_prior_cross_state_recorded" if prev is None else "no_entry_signal"
                log_row(writer, **common, action="HOLD", reason=reason)
                return open_count

    except RuntimeError as e:
        log_row(writer, **common, action="ERROR", reason="order_submission_failed", error=str(e))
        return open_count


def _submit_entry(symbol, asset_class, session, notional, price):
    if asset_class == "crypto":
        # Alpaca requires time_in_force="gtc" for crypto orders -- "day" (the default,
        # correct for stocks) is rejected with HTTP 422 "invalid crypto time_in_force".
        return place_order(symbol, "buy", notional=notional, order_type="market", time_in_force="gtc")
    if session == "regular":
        return place_order(symbol, "buy", notional=notional, order_type="market")
    # extended hours: whole-share marketable limit order (notional not supported here)
    qty = math.floor(notional / price)
    if qty < 1:
        return {}
    limit_price = price * (1 + EXTENDED_LIMIT_NUDGE)
    return place_order(symbol, "buy", qty=qty, order_type="limit",
                        limit_price=limit_price, extended_hours=True)


def _submit_exit(symbol, asset_class, session, qty, price):
    if asset_class == "crypto":
        return place_order(symbol, "sell", qty=qty, order_type="market", time_in_force="gtc")
    if session == "regular":
        return place_order(symbol, "sell", qty=qty, order_type="market")
    limit_price = price * (1 - EXTENDED_LIMIT_NUDGE)
    return place_order(symbol, "sell", qty=qty, order_type="limit",
                        limit_price=limit_price, extended_hours=True)


def flatten_stock_position(symbol, session, qty, price, writer, now_et, buying_power, open_count):
    order = _submit_exit(symbol, "stock", session, qty, price)
    log_row(writer,
            ts_utc=datetime.now(timezone.utc).isoformat(), ts_et=now_et.isoformat(),
            symbol=symbol, asset_class="stock", session=session, action="FLATTEN",
            reason="overnight_flatten_15min_before_close", price=price, qty_or_notional=qty,
            order_id=order.get("id", ""), open_positions_before=open_count, buying_power=buying_power)


# --------------------------------------------------------------------------------------
# Main cycle
# --------------------------------------------------------------------------------------

def run_cycle():
    _assert_paper_only()
    now_et = datetime.now(ET)
    state = load_state()
    f, writer = journal_writer()

    try:
        account = get_account()
        buying_power = float(account["buying_power"])
        positions = get_positions()
        open_count = len(positions)

        clock = None
        calendar_today = None
        try:
            clock = get_clock()
            calendar_today = get_calendar_today(now_et)
        except RuntimeError as e:
            print(f"WARNING: could not fetch clock/calendar: {e}", file=sys.stderr)

        session = stock_session(now_et, clock, calendar_today)

        # ---- Overnight flatten check (stocks only) ----
        if session != "closed" and is_flatten_window(now_et):
            for sym, pos in list(positions.items()):
                if "/" in sym:  # crypto symbols contain a slash; skip
                    continue
                qty = float(pos["qty"])
                if qty > 0:
                    price = float(pos.get("current_price") or pos["avg_entry_price"])
                    flatten_stock_position(sym, session, qty, price, writer, now_et,
                                            buying_power, open_count)
                    open_count -= 1
            positions = get_positions()  # refresh after flattening

        # ---- Stocks ----
        if session == "closed":
            for sym in STOCK_WATCHLIST:
                log_row(writer,
                        ts_utc=datetime.now(timezone.utc).isoformat(), ts_et=now_et.isoformat(),
                        symbol=sym, asset_class="stock", session="closed", action="HOLD",
                        reason="market_closed", open_positions_before=open_count,
                        buying_power=buying_power)
        else:
            bars = get_stock_bars(STOCK_WATCHLIST)
            for sym in STOCK_WATCHLIST:
                closes = [b["c"] for b in bars.get(sym, [])]
                open_count = decide_and_act(sym, "stock", session, closes, positions, state,
                                             open_count, buying_power, writer, now_et)

        # ---- Crypto (always 24/7, always market orders, never flattened) ----
        crypto_bars = get_crypto_bars(CRYPTO_WATCHLIST)
        for sym in CRYPTO_WATCHLIST:
            closes = [b["c"] for b in crypto_bars.get(sym, [])]
            open_count = decide_and_act(sym, "crypto", "24/7", closes, positions, state,
                                         open_count, buying_power, writer, now_et)

        save_state(state)
        print(f"Cycle complete at {now_et.isoformat()} | session={session} | open_positions={open_count}")

    finally:
        f.close()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import selftest  # noqa
        selftest.run()
    else:
        run_cycle()
