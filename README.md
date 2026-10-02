# God_Help_TradeBot

Private workspace for a Webull trading bot; test and validate before live trading.

## Current status

The `Prototype` branch currently contains a broker-independent foundation:

- Explicit price, position-value, and dollar-risk guardrails.
- Decimal-based position sizing and take-profit planning.
- Deterministic paper-mode candidate screening using price, momentum, volume, spread, and range position.
- Virtual cash, positions, market/limit/stop fills, and long OCO-style target/stop behavior.
- U.S. trading-session detection with holiday and early-close handling.
- No Webull credentials, network calls, or live order submission.

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

The next integration step is a paper-mode Webull adapter whose default behavior is read-only and preview-only.
