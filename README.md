# God_Help_TradeBot

Sandbox-only Webull API bot. The default Prototype strategy is **SPY 0DTE long options**; live mode remains disabled.

## SPY 0DTE Strategy

- Starts monitoring SPY at 9:25 AM ET; option entries are only attempted during CORE market hours.
- Uses SPY 5-minute chart patterns/trend, ticker and broad-market news, flow/analyst signals when available, and option Greeks for contract validation.
- Strong directional consensus buys with the signal (bullish = call, bearish = put). Moderate directional consensus is inverted per the configured rule. Neutral, conflicting, stale, illiquid, or incomplete data means no trade.
- Selects the highest-ask same-day SPY call/put with premium no greater than $1.00 per share ($100 per standard 100-share contract), positive two-sided option depth, acceptable spread, and valid Greeks.
- Paper contract quantity is bounded by sandbox buying power, visible ask size, and the per-order contract cap. Market orders can still slip, partially fill, or remain unfilled.
- Tracks one entry per day. Take-profit is +30%, stop-loss is -10% from confirmed average fill, and the bot submits a market exit at 3:59 PM ET. The simulator may not fill an exit without bid liquidity; the state file and logs preserve that unresolved status.
- The previous equity strategy is available only with `strategy_mode: "equity"`; its backtest/self-study is not an options backtest and is not run in SPY options mode.

This is experimental software, not investment advice or a guarantee of profit. 0DTE options can lose the entire premium rapidly. Test only in the sandbox and supervise every session.

## Run Tests

```powershell
$env:PYTHONPATH = "$PWD\src"
py -3 -m pytest tests -q
```

## Configure

Copy `.env.example` to `.env` and set sandbox API credentials. Keep secrets out of Git. The default `BotConfig` selects `spy_0dte_options`; use `bot_config.json` or `BOT_STRATEGY_MODE=equity` to select the legacy equity monitor explicitly.

Useful SPY controls in `bot_config.json`:

```json
{
  "strategy_mode": "spy_0dte_options",
  "spy_option_contract_budget_usd": 100.0,
  "spy_option_buying_power_fraction": 0.98,
  "spy_option_max_spread_percent": 20.0,
  "spy_option_strong_score": 0.70,
  "spy_option_weak_floor": 0.55
}
```

See [DEPLOY.md](DEPLOY.md) for the VPS install and service instructions.
