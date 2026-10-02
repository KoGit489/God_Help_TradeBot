# God_Help_TradeBot

Private workspace for a Webull trading bot; test and validate before live trading.

## Current status

The `Prototype` branch currently contains a broker-independent foundation:

- Explicit price, position-value, and dollar-risk guardrails.
- Decimal-based position sizing and take-profit planning.
- Deterministic paper-mode candidate screening using price, momentum, volume, spread, and range position.
- Virtual cash, positions, market/limit/stop fills, and long OCO-style target/stop behavior.
- U.S. trading-session detection with holiday and early-close handling.
- Webull sandbox adapter backed by the official OpenAPI SDK: authenticated quotes,
  positions, real order previews, and simulated place/cancel against `api.sandbox.webull.com`.
- No live trading: `live_mode` cannot be enabled and production endpoints are never used.

Sandbox credentials come from Webull OpenAPI Management; the bot targets the
**Individual Cash** sandbox account (`WEBULL_ACCOUNT_ID`) and the free
**Nasdaq Basic – Non Display** OpenAPI market-data permission.

Run the tests with:

```powershell
$env:PYTHONPATH = "$PWD\src"
py -3 -m pytest -q
```

Sandbox setup:

1. Copy `.env.example` to `.env` in the project root.
2. Fill in the Webull sandbox credentials only.
3. Keep `.env` local and uncommitted; it is already ignored by Git.
4. Load the config through the bot with `load_webull_config()`.

```python
from god_help_tradebot import load_webull_config

config = load_webull_config()
print(config.api_key, config.account_id)
```

The next integration step is the strategy monitoring loop: screen candidates, enter a
sandbox position with bracket orders, and track exits against live sandbox quotes.
