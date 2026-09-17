# Alpaca Paper Day-Trading Bot

Rule-based (not LLM-driven) day trader for Alpaca's free **paper** trading API.
5-min SMA(5/15) crossover + RSI(14) filter, 5% position sizing, max 6 open positions,
-4% stop loss, extended-hours marketable-limit orders, automatic overnight flatten for
stocks, 24/7 crypto, full CSV audit trail. Runs on GitHub Actions' free scheduler --
nothing runs on your computer, and no LLM is involved at runtime.

**Hard safety guard:** the script refuses to run at all unless `ALPACA_BASE_URL` points at
`https://paper-api.alpaca.markets`. It cannot place a live trade even by mistake.

---

## What you need to do yourself (exact steps)

### 1. Get your Alpaca paper keys ready
You said you already have a paper account and API key/secret -- just double-check, in
the Alpaca dashboard (top-right toggle set to **Paper**):
- **Paper Trading > API Keys**: note your Key ID and Secret Key (regenerate if you don't
  have the secret anymore -- it's only shown once).
- **Account Configuration**: make sure crypto trading is enabled for the paper account
  (it's on by default for most accounts, but confirm).
- Extended-hours trading doesn't need a separate toggle -- it's controlled per-order via
  the `extended_hours` flag, which the script already sets correctly.

### 2. Create the GitHub repo
1. Go to github.com -> **New repository**. Name it something like `alpaca-paper-bot`.
   Private or public, your choice (public is fine -- your keys never go in the code).
2. Upload every file from this folder into the repo, **preserving the folder structure**
   -- especially `.github/workflows/trading-bot.yml` must stay under `.github/workflows/`.
   Easiest way: `git clone` the empty repo locally, copy these files in, then:
   ```
   git add .
   git commit -m "Initial bot setup"
   git push
   ```

### 3. Add your Alpaca keys as encrypted GitHub secrets
In your repo: **Settings -> Secrets and variables -> Actions -> New repository secret**
- Name: `ALPACA_API_KEY` -> value: your Key ID
- Name: `ALPACA_SECRET_KEY` -> value: your Secret Key

These are encrypted at rest, never appear in logs, and are never visible in the code.

### 4. Turn the workflow on
GitHub Actions is enabled by default. Go to the **Actions** tab in your repo -- you
should see "Alpaca Paper Day-Trading Bot" listed. Click it, then **Run workflow** once
manually to confirm it works before letting the 5-minute schedule take over.

### 5. Watch it run
- Every run shows up under the **Actions** tab with full logs (great for screen-recording
  the tutorial).
- Every decision -- buys, sells, holds, flattens, even errors -- gets appended as a row
  to `trade_journal.csv`, committed back to the repo automatically. That file is your
  full audit trail.
- `state.json` just tracks each symbol's last known fast/slow-SMA relationship so the bot
  can tell a genuine crossover *event* apart from "still above from before."

That's it -- steps 1-4 are the only manual work. Everything else below already exists
in this folder and needs no further setup.

---

## What's already built for you (no action needed)

- `alpaca_paper_bot.py` -- the whole strategy: indicators, signal logic, position sizing,
  stop-loss, session-aware order types, overnight flatten, CSV journaling. Pure Python
  standard library, no dependencies to install.
- `selftest.py` -- an offline test suite (26 checks) that validates the math and decision
  logic with synthetic data and mocked API calls, no network or keys needed. Run it
  yourself any time with `python3 alpaca_paper_bot.py --selftest`.
- `.github/workflows/trading-bot.yml` -- the cron schedule that fires the bot every
  5 minutes on GitHub's servers and commits the results back.
- `state.json` / `trade_journal.csv` -- starter files with the right shape; the bot
  updates them each cycle.

---

## Exactly how the strategy is implemented

| Requirement | Implementation |
|---|---|
| 5-min bars | Alpaca `/v2/stocks/bars` and `/v1beta3/crypto/us/bars`, `timeframe=5Min` |
| Golden/death cross | Detected as a state **transition** (fast SMA crossing the slow SMA), not just "currently above" -- so it won't re-buy every cycle while already crossed |
| RSI filter | Buy only if RSI(14) < 70 on a golden cross; exit if RSI > 80 regardless of trend |
| Stop loss | Exits if Alpaca's own `unrealized_plpc` on the position is <= -4% |
| Position sizing | `5% of current buying power`, re-queried fresh every cycle |
| Max positions | 6 open positions across stocks + crypto combined; new entries are skipped (and logged as HOLD) once the cap is hit -- exits are never blocked by the cap |
| No overnight stock positions | Between 19:45-20:00 ET, all open **stock** positions are force-closed regardless of signal (crypto is explicitly exempt) |
| Extended hours | 4:00-9:30 and 16:00-20:00 ET use whole-share **limit** orders with `extended_hours=true`, nudged 0.5% through the current price to stay marketable, per Alpaca's rules. Regular hours (9:30-16:00 ET, or whenever Alpaca's own `/v2/clock` says `is_open`) use plain market orders |
| Crypto 24/7 | No session check at all for crypto -- always market orders, every cycle, never flattened |
| Full audit trail | Every symbol gets a journal row every single cycle, including every HOLD and its reason (`no_entry_signal`, `max_open_positions_reached`, `market_closed`, etc.) |

## Known limitations worth knowing before you film

- GitHub's cron scheduler is "best effort" -- under heavy load across all of GitHub, a
  `*/5` schedule can occasionally slip by a few minutes. Not something we can fix from
  our side; if you need rock-solid timing for the video, run it manually via
  **Actions -> Run workflow** during the demo instead of waiting on the schedule.
- The bot only sees the last ~60 five-minute bars each cycle (about 5 hours of history),
  which is plenty for SMA(15)/RSI(14) but means it "forgets" older price action --
  that's intentional and matches how the strategy is specified, not a bug.
- This is intentionally simple, readable code for a tutorial -- no retry/backoff on
  transient API errors beyond logging them as an `ERROR` journal row and moving on to
  the next symbol.

## Free crash detection (no LLM needed)

GitHub already emails the repo owner by default whenever a scheduled workflow run
fails outright (e.g. bad credentials, Alpaca API down). Nothing extra to build --
just don't turn that notification off in your GitHub notification settings. That's
your "watchdog" for zero additional cost.
