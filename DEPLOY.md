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

- The bot only ever talks to `api.sandbox.webull.com` — simulated money.
- Never commit `.env` or real keys. On the server, prefer systemd `Environment=`
  or a locked-down `.env` (`chmod 600 .env`).
- First cloud run: watch `journalctl -u tradebot -f` for the first entry.
