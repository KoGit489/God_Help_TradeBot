# Deploying the bot to a cloud server (VPS)

Runs the bot 24/7 without your home PC. Sandbox-only; live trading stays off.

## What you need

- A small VPS (Ubuntu 22.04+). ~$4–6/month from DigitalOcean, Linode/Akamai,
  Vultr, Hetzner, or a free/cheap tier elsewhere. 1 GB RAM is plenty.
- Your Webull sandbox App Key / App Secret and the Individual Cash account ID.
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
git checkout Prototype
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

## Updating Prototype

Keep the options performance ledger and daily recovery state in the server's
working directory. Back them up before updating; do not delete them during a
code update.

```bash
set -e
cd /root/God_Help_TradeBot
if [ -n "$(git status --porcelain)" ]; then
  echo "Server worktree is dirty; preserve/reconcile local changes before updating."
  exit 1
fi
systemctl stop tradebot
backup_dir="/root/tradebot-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
for file in spy_options_state.json spy_options_performance.json; do
  if [ -f "$file" ]; then cp -a "$file" "$backup_dir/"; fi
done
git fetch origin
git switch Prototype
git pull --ff-only origin Prototype
source .venv/bin/activate
pip install .
systemctl restart tradebot
systemctl status --no-pager tradebot
journalctl -u tradebot -n 100 --no-pager
```

The command stops if the server worktree is dirty. Backups are stored outside
the repository under `/root/tradebot-backups/`. Keep
`spy_options_performance.json` to retain compounded equity and review history.
`spy_options_state.json` is separate same-session order recovery state; only
archive it after confirming the sandbox has no positions or open orders.

## Safety notes

- Default strategy is `spy_0dte_options`; use `strategy_mode="equity"` only to
  explicitly run the legacy equity strategy. Do not start options mode until
  the sandbox account is reset/flat and has no working orders.
- The SPY paper strategy monitors from 9:25 AM ET, buys same-day SPY contracts
  during CORE hours, and uses MARKET orders. The default $100 starting strategy
  budget compounds by realized option P&L. Contract premium is `ask x 100` and
  must fit the current budget; quantity is maximized within strategy equity,
  buying power, visible ask size, and `spy_option_max_contracts_per_order`.
  Market slippage can exceed the observed ask and budget.
- Strong/weak signal thresholds and the low-conviction inverse rule are
  configurable. Signals combine existing SPY chart/flow/analyst/ticker-news
  data with broad market news; Greeks validate contract exposure/liquidity,
  they do not predict direction. Missing required inputs means no trade.
- One entry attempt per session. TP/SL are +30%/-10% of the confirmed option
  premium fill and are triggered using the option bid. A market exit is
  submitted at 3:59 PM ET. Zero bid depth can leave a
  0DTE position unfilled through expiration; inspect logs and account state.
- Each five completed option trades, fewer than four winners tightens signal
  and spread gates; adjustments and realized P&L persist in
  `spy_options_performance.json`. The policy only tightens and pauses if it
  reaches its limits while still below target. It is not a validated options
  backtest and cannot promise long-term effectiveness; broker fees are not
  included in the local realized-P&L ledger. Back up this file across upgrades.
- `spy_options_state.json` is local recovery state and is Git-ignored. After a
  clean Webull reset and confirmed zero positions/open orders, archive or
  remove this file before a fresh options run.
- The existing equity backtest and weekly self-study do not model option
  contracts/Greeks/0DTE expiration and are skipped in SPY options mode. The
  rolling options review is based on realized paper fills only; neither system
  proves future profitability.
- Sandbox orders need executable streaming NBBO liquidity. A reference bid/ask
  snapshot is not evidence that the sandbox matching engine can fill an order.
  Webull entries now fail closed if `bid_volume` or `ask_volume` is absent or
  nonpositive. Missing snapshot depth may therefore stop all entries until the
  data feed is configured. Positive snapshot depth still does not guarantee a fill.
- `exit_liquidity_wait` means an existing exit is still pending without usable
  quote depth. The bot does not repeatedly cancel and replace it to manufacture
  liquidity. `cancel_pending` means replacement is blocked awaiting server
  confirmation. Neither event means the position is flat.
- Entry cutoff (one minute before close) and CORE market close are separate.
  All monitor entries, take-profits, stops, flatten orders, and exit retries
  use MARKET/CORE. They pause outside regular hours; no NIGHT limit-order
  fallback is submitted. An unfinished EOD liquidation request persists and
  resumes at the next regular open, including after a restart. Missing
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
- Local tests do not establish that the server receives depth fields. Observe
  `liquidity_rejected`, `exit_liquidity_wait`, and `cancel_pending` logs before
  judging profitability. Do not remove the price cap to force a fill.

### Market-only execution and scan capacity

- The monitor submits market orders rather than patient limit entries. Market
  orders cross the spread and may slip or remain unfilled when executable
  depth is absent. Existing spread, depth, sizing and direction gates remain.
  The broker and paper simulator still support other order types for isolated
  API testing, but the running strategy does not submit them.
- Legacy `entry_limit_spread_fraction` and `entry_chase_polls` settings are
  accepted for configuration compatibility but do not alter market-only entries.
- SDK 3.0.2 documents 200 non-paginated rows per screener ranking. Discovery
  now uses daily/3-minute/5-minute gainers and losers plus relative-volume,
  volume, turnover, turnover-rate and amplitude activity lists. With shorts
  enabled, that is up to 2,200 raw rows, not 2,200 guaranteed unique stocks.
  `screener_list_limit` defaults to 200; an existing lower override still applies.
- Symbols are deduplicated and price-filtered before quote enrichment. A pass
  quotes up to 1,200 symbols in 12 batches of 100; the offset rotates through
  larger eligible pools. Discovery refreshes no more often than every 30
  seconds. At that cadence, discovery uses at most 24 snapshot calls/minute,
  leaving headroom below the published sandbox 30/minute limit for entry and
  exit checks. Other clients sharing the app key consume the same quota.
- `[scan_summary]` reports observed unique, price-eligible and quote-enriched
  counts. Actual coverage depends on API responses, entitlements, overlap and
  failed batches. The confirmation budget remains five per cycle by default.
  This is a bounded scan expansion, not a measured hardware maximum or a
  claim to scan every US listing. Follow live counts and 429s before raising
  resource use further.
