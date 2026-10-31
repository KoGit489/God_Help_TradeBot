# God_Help_TradeBot

Sandbox-only Webull API bot. The default Prototype strategy is **SPY 0DTE long options**; live mode remains disabled.

## SPY 0DTE Strategy

- Starts monitoring SPY at 9:25 AM ET; option entries are only attempted during CORE market hours.
- Uses SPY 5-minute chart patterns/trend, ticker and broad-market news, flow/analyst signals when available, and option Greeks for contract validation.
- Strong directional consensus buys with the signal (bullish = call, bearish = put). Moderate directional consensus is inverted per the configured rule. Weak directional signals get a second-stage contract search if strict selection finds nothing.
- Seeds a persistent options budget from `spy_option_initial_budget_usd` (default $100). After each closed trade, realized option P&L compounds that budget; the next contract's ask premium (`ask x 100`) must fit the current budget. The most expensive qualifying contract is selected, and quantity uses the maximum affordable amount, capped by current sandbox buying power, visible ask size, and the configured order limit.
- Strong signals use strict spread, Greek, and depth filters. The weak fallback allows a configured spread up to 45% and missing secondary Greeks or reported day-volume, but still requires a same-day contract, fresh quote, correctly signed delta, positive two-sided displayed depth, and premium within budget. Market orders can still slip, partially fill, or remain unfilled; no daily fill can be guaranteed.
- Tracks one entry per session. Take-profit is +30% and stop-loss is -10% of the confirmed option premium fill, evaluated against the option's bid. The bot submits a market exit at 3:59 PM ET. The simulator may not fill an exit without bid liquidity; the state file and logs preserve that unresolved status.
- After each five completed trades, reviews the latest five. Fewer than four wins tightens signal and spread gates one step; gates never auto-relax. If all tightening limits are reached and the target is still missed, new entries pause for manual review. The ledger persists across restarts and dates in `spy_options_performance.json`.
- This review is a conservative filter, not evidence that future results will improve. It does not replay option data, account for fees, or guarantee effectiveness over weeks, months, or years. Back up the performance ledger and supervise the paper account.
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
  "spy_option_initial_budget_usd": 100.0,
  "spy_option_buying_power_fraction": 1.0,
  "spy_option_max_spread_percent": 20.0,
  "spy_option_weak_max_spread_percent": 45.0,
  "spy_option_strong_score": 0.70,
  "spy_option_weak_floor": 0.55
}
```

See [DEPLOY.md](DEPLOY.md) for the VPS install and service instructions.
