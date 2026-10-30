"""Sandbox-only, single-entry-per-day SPY 0DTE long-options strategy."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any

from .config import BotConfig
from .confirm import ConfirmationReport, confirm_candidate, confirm_candidate_bearish
from .monitor import LoopEvent, LoopSummary
from .patterns import trend_direction
from .schedule import EASTERN, TradingSession, get_trading_session
from .worldnews import fetch_market_news

SPY_SYMBOL = "SPY"
OPTION_MULTIPLIER = Decimal("100")
STATE_FILE = Path("spy_options_state.json")
logger = logging.getLogger("god_help_tradebot")


@dataclass(frozen=True)
class OptionContract:
    option_symbol: str
    expiration_date: str
    strike: Decimal
    option_type: str
    multiplier: Decimal = OPTION_MULTIPLIER


@dataclass(frozen=True)
class OptionQuote:
    option_symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float | None
    implied_volatility: float | None
    quote_time: str | None
    delay_minutes: float | None
    volume: Decimal


@dataclass(frozen=True)
class OptionSignal:
    option_type: str | None
    regime: str
    reason: str
    bullish_score: float
    bearish_score: float
    signals_present: int
    trend: str | None


@dataclass(frozen=True)
class ContractSelection:
    contract: OptionContract | None
    quote: OptionQuote | None
    reason: str
    candidates_seen: int


def _decimal(record: dict[str, Any], *names: str) -> Decimal | None:
    for name in names:
        value = record.get(name)
        if value in (None, ""):
            continue
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if result.is_finite():
            return result
    return None


def _float(record: dict[str, Any], *names: str) -> float | None:
    value = _decimal(record, *names)
    return float(value) if value is not None else None


def records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "contracts", "items", "records", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            rows = records_from_payload(value)
            if rows:
                return rows
    return []


def pagination_key(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("pagination_key") or payload.get("paginationKey")
    if value:
        return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        value = data.get("pagination_key") or data.get("paginationKey")
        return str(value) if value else None
    return None


def parse_contract(record: dict[str, Any]) -> OptionContract | None:
    symbol = str(record.get("option_symbol") or record.get("symbol") or "").upper()
    expiry = str(record.get("expiration_date") or record.get("option_expire_date") or record.get("expire_date") or "")[:10]
    strike = _decimal(record, "strike_price", "strike")
    option_type = str(record.get("option_type") or "").upper()
    if option_type.endswith("CALL_OPTION") or option_type == "C":
        option_type = "CALL"
    elif option_type.endswith("PUT_OPTION") or option_type == "P":
        option_type = "PUT"
    multiplier = _decimal(record, "multiplier", "contract_multiplier") or OPTION_MULTIPLIER
    if not symbol or not expiry or strike is None or option_type not in ("CALL", "PUT") or multiplier != OPTION_MULTIPLIER:
        return None
    return OptionContract(symbol, expiry, strike, option_type, multiplier)


def parse_option_quote(record: dict[str, Any] | None) -> OptionQuote | None:
    if not isinstance(record, dict):
        return None
    symbol = str(record.get("symbol") or record.get("option_symbol") or "").upper()
    bid = _decimal(record, "bid", "bid_price")
    ask = _decimal(record, "ask", "ask_price")
    bid_size = _decimal(record, "bid_size", "bid_volume")
    ask_size = _decimal(record, "ask_size", "ask_volume")
    greeks = {key: _float(record, key) for key in ("delta", "gamma", "theta", "vega")}
    if not symbol or bid is None or ask is None or bid_size is None or ask_size is None or any(v is None for v in greeks.values()):
        return None
    return OptionQuote(
        symbol, bid, ask, bid_size, ask_size,
        greeks["delta"], greeks["gamma"], greeks["theta"], greeks["vega"],
        _float(record, "rho"), _float(record, "imp_vol", "implied_volatility", "iv"),
        str(record["quote_time"]) if record.get("quote_time") is not None else None,
        _float(record, "delay_minutes"), _decimal(record, "volume") or Decimal("0"),
    )


def _quote_fresh(quote: OptionQuote, now: datetime, max_delay_minutes: int) -> bool:
    if quote.delay_minutes is not None and quote.delay_minutes > max_delay_minutes:
        return False
    if quote.quote_time is None:
        return quote.delay_minutes is not None
    try:
        raw = quote.quote_time
        if raw.isdigit():
            stamp = float(raw)
            if stamp > 10_000_000_000:
                stamp /= 1000
            quote_time = datetime.fromtimestamp(stamp, tz=timezone.utc)
        else:
            quote_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if quote_time.tzinfo is None:
                quote_time = quote_time.replace(tzinfo=timezone.utc)
            quote_time = quote_time.astimezone(timezone.utc)
        age = (now.astimezone(timezone.utc) - quote_time).total_seconds() / 60
        return 0 <= age <= max_delay_minutes
    except (ValueError, OverflowError, OSError):
        return False


def decide_option_direction(
    bullish: ConfirmationReport,
    bearish: ConfirmationReport,
    config: BotConfig,
    *,
    trend: str | None,
) -> OptionSignal:
    """Strong readings follow the signal; moderate directional readings invert."""
    present = min(
        (bullish.signals_present or 0) + (1 if bullish.market_article_count else 0),
        (bearish.signals_present or 0) + (1 if bearish.market_article_count else 0),
    )
    bull = _news_adjusted_score(bullish, bearish=False)
    bear = _news_adjusted_score(bearish, bearish=True)
    if trend == "up":
        bull = min(1.0, bull + 0.05)
        bear = max(0.0, bear - 0.05)
    elif trend == "down":
        bear = min(1.0, bear + 0.05)
        bull = max(0.0, bull - 0.05)
    gap = abs(bull - bear)
    if present < config.spy_option_min_signal_count:
        return OptionSignal(None, "insufficient", "too few real signal groups", bull, bear, present, trend)
    if bull >= config.spy_option_strong_score and bull - bear >= config.spy_option_strong_direction_gap:
        return OptionSignal("CALL", "strong_bullish", "strong bullish consensus", bull, bear, present, trend)
    if bear >= config.spy_option_strong_score and bear - bull >= config.spy_option_strong_direction_gap:
        return OptionSignal("PUT", "strong_bearish", "strong bearish consensus", bull, bear, present, trend)
    if gap < config.spy_option_min_direction_gap:
        return OptionSignal(None, "neutral", "directional scores conflict or are too close", bull, bear, present, trend)
    if bull > bear and bull >= config.spy_option_weak_floor:
        return OptionSignal("PUT", "weak_bullish_fade", "low-conviction bullish read; inverse put", bull, bear, present, trend)
    if bear > bull and bear >= config.spy_option_weak_floor:
        return OptionSignal("CALL", "weak_bearish_fade", "low-conviction bearish read; inverse call", bull, bear, present, trend)
    return OptionSignal(None, "neutral", "neither side reached the weak-signal floor", bull, bear, present, trend)


def _news_adjusted_score(report: ConfirmationReport, *, bearish: bool) -> float:
    """Blend broad market-news sentiment into the existing chart/ticker signal."""
    score = report.score
    if report.market_score is not None and report.market_article_count > 0:
        macro = 1.0 - report.market_score if bearish else report.market_score
        score = 0.85 * score + 0.15 * macro
    return min(max(score, 0.0), 1.0)


def select_option_contract(
    contracts: list[OptionContract],
    snapshots: dict[str, dict[str, Any]],
    option_type: str,
    config: BotConfig,
    now: datetime,
) -> ContractSelection:
    """Pick the highest ask premium <= $1/share with fresh depth and long-option Greeks."""
    per_share_budget = Decimal(str(config.spy_option_contract_budget_usd)) / OPTION_MULTIPLIER
    eligible: list[tuple[OptionContract, OptionQuote]] = []
    rejected = 0
    for contract in contracts:
        if contract.expiration_date != now.date().isoformat() or contract.option_type != option_type:
            continue
        quote = parse_option_quote(snapshots.get(contract.option_symbol))
        if quote is None:
            rejected += 1
            continue
        if (
            quote.bid <= 0 or quote.ask < quote.bid or quote.ask <= 0
            or quote.bid_size <= 0 or quote.ask_size <= 0 or quote.ask > per_share_budget
            or quote.gamma <= 0 or quote.theta >= 0 or quote.vega <= 0
            or quote.rho is None or quote.implied_volatility is None or quote.implied_volatility <= 0
            or (option_type == "CALL" and quote.delta <= 0)
            or (option_type == "PUT" and quote.delta >= 0)
            or (option_type == "CALL" and quote.rho <= 0)
            or (option_type == "PUT" and quote.rho >= 0)
            or not config.spy_option_min_abs_delta <= abs(quote.delta) <= config.spy_option_max_abs_delta
            or not _quote_fresh(quote, now, config.spy_option_max_quote_delay_minutes)
        ):
            rejected += 1
            continue
        spread_pct = float((quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 100)
        if spread_pct > config.spy_option_max_spread_percent:
            rejected += 1
            continue
        eligible.append((contract, quote))
    if not eligible:
        return ContractSelection(None, None, f"no fresh liquid {option_type} contract within premium/Greek limits ({rejected} rejected)", rejected)
    contract, quote = max(eligible, key=lambda item: (item[1].ask, item[1].ask_size, item[1].volume))
    return ContractSelection(contract, quote, "highest ask premium within per-contract budget", rejected)


def _is_quantity_rejection(exc: Exception) -> bool:
    text = str(exc).upper()
    return any(value in text for value in (
        "ORDER_RISK_RULE_OPTIONS", "ORDER_RISK_RULE_SHARES", "ORDER_QUANTITY_EXCEED_LIMIT",
        "MAXIMUM QUANTITY", "MAX ORDER QUANTITY",
    ))


def _state_contract(state: dict[str, Any]) -> OptionContract:
    return OptionContract(
        state["option_symbol"], state["expiration"], Decimal(state["strike"]),
        state["option_type"], OPTION_MULTIPLIER,
    )


class SPY0DTEOptionsLoop:
    """Paper-only SPY 0DTE loop. One entry decision/trade attempt per session."""

    max_consecutive_errors = 5
    error_backoff_seconds = 30.0

    def __init__(self, broker, config: BotConfig, *, state_file: str | Path = STATE_FILE, now_provider=None, allow_entries=True):
        if not broker.sandbox_mode or broker.live_mode:
            raise RuntimeError("SPY 0DTE options strategy is sandbox-only")
        self.broker = broker
        self.config = config
        self.allow_entries = allow_entries
        self.state_path = Path(state_file)
        self._now = now_provider or (lambda: datetime.now(EASTERN))
        self._consecutive_errors = 0
        self._market_news = None
        self._state = self._load_state()
        if not self._state and any(position.quantity for position in broker.get_positions().values()):
            raise RuntimeError("Sandbox has positions but no SPY option recovery state; startup blocked")
        status = self._state.get("status")
        if status in ("OPEN", "ENTRY_PENDING", "EXIT_PENDING") and self._state.get("session_date") != self._now().date().isoformat():
            if any(position.quantity for position in broker.get_positions().values()):
                raise RuntimeError("Stale SPY option state and account positions remain; manual reconciliation required")
            self._state = {}

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("SPY option state unreadable; startup blocked") from exc
        if not isinstance(data, dict):
            raise RuntimeError("SPY option state invalid; startup blocked")
        return data

    @property
    def recovery_order_ids(self) -> frozenset[str]:
        client_order_id = self._state.get("pending_order_id")
        return frozenset((client_order_id,)) if client_order_id else frozenset()

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def run_once(self, summary: LoopSummary | None = None) -> LoopSummary:
        summary = summary or LoopSummary()
        try:
            self._run_cycle(summary)
            self._consecutive_errors = 0
        except Exception as exc:
            summary.polls += 1
            self._consecutive_errors += 1
            summary.events.append(LoopEvent("error", SPY_SYMBOL, f"{type(exc).__name__}: {exc} (x{self._consecutive_errors})"))
        return summary

    def _run_cycle(self, summary: LoopSummary) -> None:
        summary.polls += 1
        now = self._now()
        session = get_trading_session(now.date())
        if session is None:
            return
        today = now.date().isoformat()
        if self._state and self._state.get("session_date") != today:
            if self._state.get("status") not in ("CLOSED", "NO_FILL", "SCANNING"):
                if any(position.quantity for position in self.broker.get_positions().values()):
                    raise RuntimeError("Prior SPY option position remains; refusing a new session")
            self._state = {}
        if not self._state:
            self._state = {"session_date": today, "status": "SCANNING", "entry_attempted": False}
            self._save_state()
        if self._state.get("pending_order_id"):
            self._reconcile_order(summary)
        if self._state.get("status") == "OPEN":
            self._manage_position(now, session, summary)
            self._save_state()
            return
        if self._state.get("status") in ("ENTRY_PENDING", "EXIT_PENDING", "CLOSED", "NO_FILL"):
            self._save_state()
            return
        start = session.market_open - timedelta(minutes=self.config.spy_option_scan_minutes_before_open)
        if now < start or now >= session.exit_time:
            return
        signal = self._evaluate_signal()
        summary.events.append(LoopEvent(
            "spy_signal", SPY_SYMBOL,
            f"{signal.regime}: {signal.reason}; bull={signal.bullish_score:.3f} bear={signal.bearish_score:.3f} "
            f"signals={signal.signals_present} trend={signal.trend}",
        ))
        if not self.allow_entries or signal.option_type is None or now < session.market_open or self._state.get("entry_attempted"):
            return
        self._open_position(signal, now, summary)

    def _evaluate_signal(self) -> OptionSignal:
        if self._market_news is None:
            try:
                self._market_news = fetch_market_news()
            except Exception:
                self._market_news = None
        bull = confirm_candidate(self.broker, SPY_SYMBOL, min_score=self.config.spy_option_weak_floor, market_news=self._market_news)
        bear = confirm_candidate_bearish(self.broker, SPY_SYMBOL, min_score=self.config.spy_option_weak_floor, market_news=self._market_news)
        return decide_option_direction(bull, bear, self.config, trend=trend_direction(self.broker, SPY_SYMBOL))

    def _list_contracts(self, expiry: str, spot: float) -> list[OptionContract]:
        found: dict[str, OptionContract] = {}
        key = None
        for page in range(20):
            response = self.broker.get_option_contracts(
                SPY_SYMBOL, expiry,
                strike_min=max(0.01, spot - self.config.spy_option_max_strike_distance),
                strike_max=spot + self.config.spy_option_max_strike_distance,
                pagination_key=key,
            )
            if getattr(response, "status_code", 200) != 200:
                raise RuntimeError(f"SPY option chain HTTP {response.status_code}")
            payload = response.json()
            for row in records_from_payload(payload):
                contract = parse_contract(row)
                if contract and contract.expiration_date == expiry:
                    found[contract.option_symbol] = contract
            next_key = pagination_key(payload)
            if not next_key:
                return list(found.values())
            if next_key == key or page == 19:
                raise RuntimeError("SPY option contract pagination did not complete")
            key = next_key
        return list(found.values())

    def _open_position(self, signal: OptionSignal, now: datetime, summary: LoopSummary) -> None:
        underlying = self.broker.get_latest_quote(SPY_SYMBOL)
        if underlying is None or underlying.last <= 0:
            summary.events.append(LoopEvent("spy_underlying_unavailable", SPY_SYMBOL, "SPY quote unavailable"))
            return
        expiry = now.date().isoformat()
        contracts = self._list_contracts(expiry, float(underlying.last))
        if not contracts:
            summary.events.append(LoopEvent("spy_option_chain_empty", SPY_SYMBOL, "no same-day SPY contracts in scan range"))
            return
        quote_map = self.broker.get_option_snapshots([contract.option_symbol for contract in contracts])
        selection = select_option_contract(contracts, quote_map, signal.option_type, self.config, now)
        if selection.contract is None or selection.quote is None:
            summary.events.append(LoopEvent("spy_option_rejected", SPY_SYMBOL, selection.reason))
            return
        buying_power = self.broker.get_buying_power()
        if buying_power is None or buying_power <= 0:
            summary.events.append(LoopEvent("spy_option_sizing_blocked", SPY_SYMBOL, "buying power unavailable"))
            return
        unit_cost = selection.quote.ask * OPTION_MULTIPLIER
        quantity = int((buying_power * Decimal(str(self.config.spy_option_buying_power_fraction)) / unit_cost).to_integral_value(rounding=ROUND_DOWN))
        visible_ask_contracts = int(selection.quote.ask_size.to_integral_value(rounding=ROUND_DOWN))
        quantity = min(quantity, visible_ask_contracts)
        quantity = min(quantity, self.config.spy_option_max_contracts_per_order)
        if quantity < 1:
            summary.events.append(LoopEvent("spy_option_sizing_blocked", SPY_SYMBOL, "buying power does not cover one contract"))
            return
        client_id = uuid.uuid4().hex
        self._state.update({
            "status": "ENTRY_PENDING", "entry_attempted": True, "option_symbol": selection.contract.option_symbol,
            "option_type": selection.contract.option_type, "strike": str(selection.contract.strike),
            "expiration": expiry, "signal_regime": signal.regime,
            "entry_filled_quantity": 0, "exit_filled_quantity": 0,
            "entry_average_price": None, "requested_quantity": quantity,
            "pending_order_id": client_id, "pending_order_role": "ENTRY", "pending_order_polls": 0,
        })
        self._save_state()
        while True:
            try:
                order = self.broker.submit_option_market_order(
                    SPY_SYMBOL, selection.contract.option_symbol, selection.contract.strike,
                    expiry, selection.contract.option_type, quantity,
                    position_intent="BUY_TO_OPEN", client_order_id=client_id,
                )
                break
            except Exception as exc:
                if not _is_quantity_rejection(exc) or quantity <= 1:
                    raise
                quantity = max(1, quantity // 2)
                client_id = uuid.uuid4().hex
                self._state.update({"requested_quantity": quantity, "pending_order_id": client_id})
                self._save_state()
        summary.entries.append(order)
        summary.events.append(LoopEvent(
            "spy_option_entry", selection.contract.option_symbol,
            f"MARKET BUY {quantity} {selection.contract.option_type}; ask {selection.quote.ask}, "
            f"est. ${unit_cost * quantity:,.2f} premium, signal={signal.regime}",
        ))

    def _reconcile_order(self, summary: LoopSummary) -> None:
        client_id = self._state["pending_order_id"]
        record = self.broker.get_order_status(client_id)
        if not record:
            summary.events.append(LoopEvent("spy_option_order_unknown", SPY_SYMBOL, "order detail unavailable; no duplicate submitted"))
            return
        status = str(record.get("status", "")).upper()
        filled = int(Decimal(str(record.get("filled_quantity") or 0)))
        average = _decimal(record, "filled_avg_price", "avg_price")
        role = self._state.get("pending_order_role")
        if role == "ENTRY":
            self._state["entry_filled_quantity"] = max(self._state.get("entry_filled_quantity", 0), filled)
            if average is not None:
                self._state["entry_average_price"] = str(average)
            if self._state["entry_filled_quantity"] and self._state.get("entry_average_price") is None:
                summary.events.append(LoopEvent("spy_option_entry_fill_unpriced", SPY_SYMBOL, "filled quantity has no average price; reconciliation remains pending"))
                self._save_state()
                return
            if status == "FILLED":
                self._state["status"] = "OPEN"
                self._clear_pending()
            elif status in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
                self._state["status"] = "OPEN" if self._state["entry_filled_quantity"] else "NO_FILL"
                self._clear_pending()
            elif filled > 0 or self._state.get("pending_order_polls", 0) >= 1:
                self._cancel_remainder(client_id, summary)
            else:
                self._state["pending_order_polls"] = self._state.get("pending_order_polls", 0) + 1
                summary.events.append(LoopEvent("spy_option_entry_wait", SPY_SYMBOL, f"{status}; duplicate blocked"))
        elif role == "EXIT":
            self._record_exit_fill(record)
            if filled > self._state.get("pending_order_filled_seen", 0):
                summary.events.append(LoopEvent("spy_option_exit_fill_unpriced", SPY_SYMBOL, "filled quantity has no average price; replacement remains blocked"))
                self._save_state()
                return
            remaining = self._state.get("entry_filled_quantity", 0) - self._state["exit_filled_quantity"]
            if status in ("FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
                if remaining <= 0:
                    self._state["status"] = "CLOSED"
                    self._log_realized_pnl(summary)
                else:
                    self._state["status"] = "OPEN"
                self._clear_pending()
            else:
                self._state["pending_order_polls"] = self._state.get("pending_order_polls", 0) + 1
                if self._state["pending_order_polls"] >= 2:
                    self._cancel_remainder(client_id, summary)
                else:
                    summary.events.append(LoopEvent("spy_option_exit_wait", SPY_SYMBOL, f"{status}; no duplicate submitted"))
        self._save_state()

    def _record_exit_fill(self, record: dict[str, Any]) -> None:
        filled = int(Decimal(str(record.get("filled_quantity") or 0)))
        seen = int(self._state.get("pending_order_filled_seen", 0))
        average = _decimal(record, "filled_avg_price", "avg_price")
        if filled > seen and average is not None:
            delta = filled - seen
            previous = Decimal(str(self._state.get("exit_notional", "0")))
            previous_order_average = _decimal(self._state, "pending_order_avg_price") or Decimal("0")
            incremental_notional = (average * filled - previous_order_average * seen) * OPTION_MULTIPLIER
            self._state["exit_notional"] = str(previous + incremental_notional)
            self._state["exit_filled_quantity"] = self._state.get("exit_filled_quantity", 0) + delta
            self._state["pending_order_filled_seen"] = filled
            self._state["pending_order_avg_price"] = str(average)

    def _cancel_remainder(self, client_id: str, summary: LoopSummary) -> None:
        self.broker.cancel_order(client_id)
        record = self.broker.get_order_status(client_id)
        status = str((record or {}).get("status", "")).upper()
        if status in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FILLED"):
            filled = int(Decimal(str((record or {}).get("filled_quantity") or 0)))
            average = _decimal(record or {}, "filled_avg_price", "avg_price")
            if self._state.get("pending_order_role") == "ENTRY":
                self._state["entry_filled_quantity"] = max(self._state.get("entry_filled_quantity", 0), filled)
                if average is not None:
                    self._state["entry_average_price"] = str(average)
                if self._state["entry_filled_quantity"] and self._state.get("entry_average_price") is None:
                    summary.events.append(LoopEvent("spy_option_entry_fill_unpriced", SPY_SYMBOL, "cancelled order fill has no average price; reconciliation remains pending"))
                    self._save_state()
                    return
                self._state["status"] = "OPEN" if self._state["entry_filled_quantity"] else "NO_FILL"
            elif self._state.get("pending_order_role") == "EXIT":
                self._record_exit_fill(record or {})
                if filled > self._state.get("pending_order_filled_seen", 0):
                    summary.events.append(LoopEvent("spy_option_exit_fill_unpriced", SPY_SYMBOL, "cancelled order fill has no average price; replacement remains blocked"))
                    self._save_state()
                    return
                remain = self._state.get("entry_filled_quantity", 0) - self._state["exit_filled_quantity"]
                self._state["status"] = "CLOSED" if remain <= 0 else "OPEN"
                if remain <= 0:
                    self._log_realized_pnl(summary)
            self._clear_pending()
        else:
            summary.events.append(LoopEvent("spy_option_cancel_pending", SPY_SYMBOL, "server cancel unconfirmed; replacement blocked"))
        self._save_state()

    def _manage_position(self, now: datetime, session: TradingSession, summary: LoopSummary) -> None:
        if not session.market_open <= now < session.market_close:
            if now >= session.exit_time:
                summary.events.append(LoopEvent("spy_option_eod_unresolved", SPY_SYMBOL, "CORE closed before exit confirmed; check position/order state"))
            return
        remaining = self._state.get("entry_filled_quantity", 0) - self._state.get("exit_filled_quantity", 0)
        if remaining <= 0:
            self._state["status"] = "CLOSED"
            return
        if self._state.get("pending_order_id"):
            return
        option_symbol = self._state["option_symbol"]
        quote = parse_option_quote(self.broker.get_option_snapshots([option_symbol]).get(option_symbol))
        if quote is None or quote.bid <= 0 or quote.bid_size <= 0 or not _quote_fresh(quote, now, self.config.spy_option_max_quote_delay_minutes):
            summary.events.append(LoopEvent("spy_option_exit_liquidity_wait", option_symbol, "no fresh executable bid; option position remains open"))
            return
        entry = Decimal(str(self._state["entry_average_price"]))
        if quote.bid >= entry * Decimal("1.30"):
            reason = "TAKE_PROFIT"
        elif quote.bid <= entry * Decimal("0.90"):
            reason = "STOP_LOSS"
        elif now >= session.exit_time:
            reason = "EOD_FLATTEN"
        else:
            return
        quantity = remaining
        client_id = uuid.uuid4().hex
        self._state.update({
            "status": "EXIT_PENDING", "pending_order_id": client_id,
            "pending_order_role": "EXIT", "pending_order_polls": 0,
            "pending_order_filled_seen": 0,
            "exit_reason": reason,
        })
        self._save_state()
        order = self.broker.submit_option_market_order(
            SPY_SYMBOL, option_symbol, self._state["strike"], self._state["expiration"],
            self._state["option_type"], quantity, position_intent="SELL_TO_CLOSE", client_order_id=client_id,
        )
        summary.exits.append(order)
        summary.events.append(LoopEvent("spy_option_exit", option_symbol, f"MARKET SELL {quantity} ({reason}) at bid {quote.bid}"))

    def _clear_pending(self) -> None:
        for key in ("pending_order_id", "pending_order_role", "pending_order_polls", "pending_order_filled_seen", "pending_order_avg_price", "pending_base_exit_filled"):
            self._state.pop(key, None)

    def _log_realized_pnl(self, summary: LoopSummary) -> None:
        entry = _decimal(self._state, "entry_average_price")
        total_exit_qty = self._state.get("exit_filled_quantity", 0)
        if entry is None or total_exit_qty <= 0:
            summary.events.append(LoopEvent("spy_option_closed", SPY_SYMBOL, "filled exit; P&L unavailable"))
            return
        exit_avg = Decimal(str(self._state.get("exit_notional", "0"))) / (OPTION_MULTIPLIER * total_exit_qty)
        pnl = (exit_avg - entry) * OPTION_MULTIPLIER * total_exit_qty
        outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
        summary.events.append(LoopEvent("spy_option_closed", SPY_SYMBOL, f"{total_exit_qty} contracts; P&L ${pnl:,.2f} [{outcome}]"))
