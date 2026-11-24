# Deploying the bot to a cloud server (VPS)

Runs the bot 24/7 without your home PC. Sandbox-only; live trading stays off.

## What you need

- A small VPS (Ubuntu 22.04+). ~$4–6/month from DigitalOcean, Linode/Akamai,
  Vultr, Hetzner, or a free/cheap tier elsewhere. 1 GB RAM is plenty.
- Your Webull sandbox App Key / App Secret and the exact sandbox account ID. Shorting requires a verified sandbox margin account.
- Your Alpha Vantage API key (optional but recommended for news).

## 1. Create the server

Create an Ubuntu VPS and note its public IP. (If your Webull app uses an IP
whitelist, add this IP under API Management.)

## 2. Connect and install

```bash
ssh root@YOUR_SERVER_IP
apt update && apt install -y python3 python3-pip python3-venv git
```

## 3. Get the code

```bash
git clone https://github.com/KoGit489/God_Help_TradeBot.git
cd God_Help_TradeBot
git checkout main
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install .
```

## 4. Set credentials (environment variables)

```bash
export WEBULL_API_KEY="your_sandbox_app_key"
export WEBULL_API_SECRET="your_sandbox_app_secret"
export WEBULL_ACCOUNT_ID="your_individual_cash_account_id"
export ALPHAVANTAGE_API_KEY="your_alpha_vantage_key"   # optional
```

To make them survive reboots, add the same lines to `~/.bashrc` (or use a
systemd `Environment=` file — see below).

## 5. Run it

```bash
source .venv/bin/activate
god-help-tradebot
# or: python -m god_help_tradebot
```

Logs go to `bot.log`. Stop with `Ctrl+C`.

## 6. Keep it running after you disconnect (systemd)

Create `/etc/systemd/system/tradebot.service`:

```ini
[Unit]
Description=God Help TradeBot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/God_Help_TradeBot
Environment=WEBULL_API_KEY=your_sandbox_app_key
Environment=WEBULL_API_SECRET=your_sandbox_app_secret
Environment=WEBULL_ACCOUNT_ID=your_individual_cash_account_id
Environment=ALPHAVANTAGE_API_KEY=your_alpha_vantage_key
ExecStart=/root/God_Help_TradeBot/.venv/bin/god-help-tradebot
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then:

```bash
systemctl daemon-reload
systemctl enable --now tradebot
journalctl -u tradebot -f     # watch logs live
```

## Safety notes

### Actual five-trade review

`trade_review_enabled=true` (default) maintains `equity_performance.json` for the
configured sandbox API account. This is separate from recorded-day replay study.
It records executed entry/exit prices and quantities, gross realized P&L, outcomes,
pending client order IDs, and review history. Source quote prices are not stored
in this ledger. Existing journal logs are not imported as historical trades.

Each new batch of five completed trades must contain at least four positive-P&L
winners. Flat trades count as non-winners; no-trade days do not count, and split
or partial exits count as one trade only after matching exits and broker flatness
are confirmed. Pending orders and cumulative fill checkpoints survive restarts.
Unrelated remote orders still block startup; an account mismatch or corrupt ledger
requires manual reconciliation. A crash before an acknowledged order ID is saved
can still require manual order reconciliation; do not bypass that interlock.

If a completed order-detail response lacks a price, the broker checks the
U.S.-supported order-history endpoint at most once every five minutes. It only
accepts a positive `filled_price` from an exact client-order-ID, symbol, side,
and filled-quantity match. New entries remain paused until tracked fills are
recorded, trades finalize, and the account is flat with no tracked orders.
Unmatched or unavailable history never clears the pause.

Below 4/5, the policy tightens relative-volume quorum, available signal count,
entry spread, and confirmation-score gates with bounded steps. The changes take
effect in the running loop and persist across restarts; fallback volume gates
tighten too, while price priority and inverse execution remain. At all tightening
limits, another missed target pauses new entries, not management of existing
positions. Missing execution prices or disappearing positions without matching
fills also require manual reconciliation; they are not invented as wins/losses.

Keep and back up the ledger across updates. Do not delete it when resetting the
paper account or changing account IDs; reconcile/archive it deliberately first.
The automatic replay study is disabled while actual-trade review is enabled;
recordings remain available for manual analysis. Five trades are a very small
sample. Tightening is a heuristic policy, not verified improvement or a guarantee
of 80% wins, profitability, or durability across future market regimes. Fees and
borrow costs are not included in the ledger's gross P&L.

### Fallback five-trade review

New long and short fallback entries are tagged at order tracking and counted
only once their fills reconcile and the position is flat. Previously untagged
trades are not guessed or imported. `fallback_trade_completed` logs each
trade's gross realized P&L. `fallback_performance_review` logs each group of
five fallback trades, the number of winners, batch and cumulative fallback
P&L through that batch, and the current tightening level. A 4/5 batch can
still lose money, so check both figures.

A score below 4/5 raises the fallback screening level by one, up to four.
Each level adds 0.25 to the fallback relative-volume minimum and reduces the
snapshot spread cap by 0.5 percentage points. Good batches retain the current
level. This review changes only fallback screening and does not itself pause
entries. Fallback trades still count toward the separate all-trades review.
History and tightening survive restarts and upgrades as long as the account's
`equity_performance.json` is retained and backed up. These tighter filters are
a heuristic, not evidence that future trades will be more accurate.

### Five-session take-profit review

The equity ledger also records one result per observed regular U.S. trading
session, finalized after the close and after tracked fills reconcile. A hit is
at least one completed long or short trade recorded that session with a gross
realized return of 30% or more on entry notional. A short's profit is its entry
proceeds minus the cost to cover. Prior long-only misses are corrected when
the ledger has qualifying short fills. An observed no-trade session is a miss;
unobserved sessions are not invented. Existing verified fills seed their close
dates. A 2.52% win therefore earns a session slot, not a 30% hit. Market-wide
gainers and unrealized price moves are not credited as bot results.

`[take_profit_session]` logs each day and `[take_profit_review]` logs each
non-overlapping five-session review. After any review below four hits, the
ledger persistently prefers eligible pre-pop long setups over momentum longs
when account-price priority is active, retaining price order within each
profile. Short slots, fallback, confirmation rules, win-review gates,
position sizing and exits are unchanged. This is a ranking heuristic, not
evidence of an edge. Do not retune the strategy from five observations alone.

Ticker news confirmation uses timestamped coverage available in the prior 72
hours at fetch time. Sparse coverage pulls both bullish and bearish sentiment
toward neutral. The daily cache remains in place to limit requests, so it is
not a real-time catalyst feed. A move from the prior close may already have
happened before the open; neither the headline nor the gap certifies a
tradable 30% move after entry, especially around halts and fixed-price deals.

When Webull ranking rows include `open` and `pre_close`, the explosive-move
score separates the opening gap from the move since the open. Directional
follow-through gets more weight, strong relative volume supports it, and a
move against the proposed direction can offset gap credit. Both longs and
shorts use this calculation without extra API calls. Missing opening fields
retain the original score. The result affects momentum and bearish profile
scores, not price priority, entry gates or risk limits. New recordings retain
the inputs for replay; older files still load but lack the signal. Measure
outcomes on later sessions before relying on this heuristic.

### Scanner recovery

Equity discovery refreshes once per 60 seconds and starts at most four
100-symbol quote batches per pass. Each batch gets at most one retry after
unsupported symbols named by Webull are removed from the current process cache,
so quote enrichment is capped at eight snapshot requests per pass. Additional
invalid symbols wait for a later pass. HTTP 429 stops further quote/screener
requests for that cycle and backs off for 300 seconds; it does not count toward
the fatal consecutive-error halt. Other errors still use the normal halt/restart
guard. `entry_blocked` events include a concise rejection reason.

### Account-price-priority mode

Set `account_price_priority` to `true` in `bot_config.json` to ignore the legacy
`min_symbol_price` / `max_symbol_price` interval and fixed dollar-risk/value caps.
Normal candidates are ranked by descending stock price, then screen score, while
retaining confirmation, trend, spread, volume, timing, and recovery gates. The
bounded discovery scanner is not an exhaustive scan of every US stock.

The bot reports buying power at startup as `[account_buying_power]`, refreshes
it before each new candidate scan and selection, and again before entry. It
uses 98% of it for sizing, rounds down to whole shares, and retains the 199,999
share limit. Long sizing uses the latest ask; short sizing uses the latest bid.
Missing or nonpositive buying power blocks entries, with no fixed-budget fallback.
Zero buying power is preserved; account equity is never substituted for missing
buying power. Cash balance is used only when the buying-power field is absent.
Missing funds block entry scans without disabling existing-position exit management.
Whole shares, the share cap, market-price changes, and broker margin rules can
leave funds unused or cause rejection. Margin buying power may include leverage;
it is not the cash balance or equity. Full-budget sizing is highly concentrated.

Fallback retains its existing relaxed filters and first-hour/tape protections.
With `paradox_fallback` enabled, the weakest eligible screen score takes
priority across bullish and bearish reads, with higher price breaking equal
scores in account-price-priority mode. Bullish fallback reads are shorted;
bearish fallback reads are bought. Without Paradox, account-price-priority
fallback still ranks highest affordable price first. Weakest score is a heuristic,
not a measured probability of loss. Buying-power eligibility is not proof that
a particular stock is borrowable or its short margin requirement is satisfied.

To enable shorts, set `allow_shorts` to `true` and set `WEBULL_ACCOUNT_ID` in the
service environment to the exact sandbox margin account ID returned by OpenAPI.
Startup requires a matching account with `account_type=MARGIN`; an unknown type
or cash account blocks startup. No automatic account switching is performed.
Confirm the sandbox account has no positions or working orders before changing
accounts; preserve and reconcile local recovery state instead of deleting it.
Live order submission remains disabled.

- Penny-stock entries require valid live bid/ask prices and a spread within the
  configured limit, but no longer require displayed bid/ask sizes. This can
  increase entry attempts when size fields are absent; it does not guarantee a
  sandbox fill, and market orders can slip or remain unfilled.
- `exit_liquidity_wait` means an existing exit is still pending without usable
  quote size. The bot does not repeatedly cancel and replace it without usable
  size. `cancel_pending` means replacement is blocked awaiting server
  confirmation. Neither event means the position is flat.
- The exchange calendar includes standard weekday holidays, Good Friday,
  Thanksgiving, Christmas, Juneteenth, and known early-close rules. Verified
  NYSE/Nasdaq closure headlines refresh once hourly and persist exact-date
  overrides to `exchange_calendar.json`; ambiguous, undated, or unofficial news
  is ignored. Verified early-close notices persist the exact close time; the
  bot submits EOD flatten at close minus one minute (for example, 12:59 p.m.
  for a 1:00 p.m. close or 1:59 p.m. for a 2:00 p.m. close).
- Entry cutoff (one minute before close) and CORE market close are separate.
  All monitor entries, take-profits, stops, flatten orders, and exit retries
  use MARKET/CORE. They pause outside regular hours; no NIGHT limit-order
  fallback is submitted. An unfinished EOD liquidation request persists and
  resumes at the next regular open, including after a restart; existing exit
  fills are logged before normal candidate scanning resumes. A new entry starts
  only after broker positions confirm flat and existing account buying-power,
  price-priority, spread, confirmation, and sizing rules pass again.
  Missing
  liquidity can still leave overnight exposure. A fatal loop halt exits nonzero
  so the unit's `Restart=on-failure` policy can act; this does not guarantee
  execution. Startup now queries server-side open orders and refuses to start
  if any exist or their response cannot be verified. Resolve those orders
  explicitly before restarting; the bot does not silently cancel them.

### Resetting the sandbox

1. Keep `tradebot` stopped while resetting. Webull support confirmed that this
  deployment's API test account is linked to its app paper-trading account.
  Select that linked account, never a live brokerage account.
2. Use the linked paper-trading account's Reset Account / Reset Assets control.
  Do not assume the same linkage for another account: verify it with Webull.
  Do not use Reset Key: it rotates credentials, not account assets.
3. After reset, query positions AND server-side open orders. Confirm both are
  empty and verify the starting cash before resuming the service.
4. Only then archive old local `data/`, `study_state.json`, and `bot_state.json`
  before starting a fresh study. Deleting those files never resets Webull.
5. Support recommends AAPL/TSLA/SPY for order-flow checks. They may exceed the
  strategy's price ceiling; use a separate sandbox test rather than relaxing
  strategy filters. Local paper replays do not prove sandbox execution quality.

- The bot only ever talks to `api.sandbox.webull.com` — simulated money.
- Never commit `.env` or real keys. On the server, prefer systemd `Environment=`
  or a locked-down `.env` (`chmod 600 .env`).
- First cloud run: watch `journalctl -u tradebot -f` for the first entry.

### Preactivation review

- The strategy retains the configured $1-$20 default price interval, pre-pop
  priority, momentum chase cap (+20% default), normal confirmation gates,
  and opposite-direction weakest-score fallback after the first hour.
  Pending second confirmations and unexamined budget-overflow candidates do
  not count as rejected signals and cannot trigger the paradox fallback.
- The weakest screen score is a heuristic, not a calibrated probability of
  failure. Neither paradox selection nor historical winning days prove an edge.
- TP/SL remain 30%/10% by default, anchored to confirmed entry fills. EOD
  flatten starts one minute before exchange close; this is a submission time,
  not a guarantee of liquidation before the bell.
- Short direction survives saved-state recovery. Cancellation must be
  confirmed before replacement; a fill racing cancellation blocks replacement.
- Study results are simulated replays, not the actual account's daily P&L.
  Replays omit live news/confirmation and do not reproduce the sandbox matching
  engine. Fixed-date comparisons, earlier-win protection and bounded nudges
  reduce some fitting risks but do not prevent overfitting. For the initial
  monitored sessions, recommend explicitly setting `study_auto_improve` to
  `false`; recording and reports remain available without promoting changes.
- Observe `quote_rejected`, `spread_rejected`, `exit_liquidity_wait`, and
  `cancel_pending` logs when reviewing paper runs. Do not treat entries submitted
  without displayed size as proof of available execution liquidity.

### Market-only execution and scan capacity

- The monitor submits market orders rather than patient limit entries. Market
  orders cross the spread and may slip or remain unfilled when executable
  depth is absent. Existing spread, depth, sizing and direction gates remain.
  The broker and paper simulator still support other order types for isolated
  API testing, but the running strategy does not submit them.
- Legacy `entry_limit_spread_fraction` and `entry_chase_polls` settings are
  accepted for configuration compatibility but do not alter market-only entries.
- SDK 3.0.2 documents 200 non-paginated rows per screener ranking. Discovery
  uses daily/3-minute/5-minute gainers and losers plus relative-volume, volume,
  turnover, turnover-rate and amplitude activity lists. With shorts enabled,
  that is up to 2,200 raw rows, not 2,200 guaranteed unique stocks.
  `screener_list_limit` defaults to 200; an existing lower override still applies.
- Symbols are deduplicated and price-filtered before quote enrichment. A pass
  starts up to four 100-symbol batches and makes at most one invalid-symbol
  retry per batch (eight quote requests maximum). The offset rotates through
  larger eligible pools; discovery refreshes no more often than every 60
  seconds. Rate limits remain possible because screeners, confirmations, exits,
  and other clients sharing the app key also consume API capacity; monitor
  `[rate_limit_backoff]` events rather than raising scan volume preemptively.
- `[scan_summary]` reports observed unique, price-eligible and quote-enriched
  counts. Actual coverage depends on API responses, entitlements, overlap and
  failed batches. The confirmation budget remains five per cycle by default.
  This is a bounded scan expansion, not a measured hardware maximum or a
  claim to scan every US listing. Follow live counts and 429s before raising
  resource use further.
