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

## Safety notes

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
  Exits use CORE until the actual close, pause in unsupported hours, and may
  retry in NIGHT if the symbol supports it. A fatal loop halt exits nonzero
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

- The strategy retains the configured price ceiling (default $10), pre-pop
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
