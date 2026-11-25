from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .backtest import ExecutionStress, load_timeline, run_research_backtest
from .config import BotConfig


BOUNDS = {"max_limit_slippage_percent": (0.1, 0.5), "entry_chase_polls": (1, 4), "exit_chase_polls": (1, 4)}
MINIMUMS = {"training": 100, "validation": 50, "holdout": 30, "shadow": 30}
POLICY = {"version": 1, "approved": True, "bounds": {key: list(value) for key, value in BOUNDS.items()},
          "minimum_trades": MINIMUMS, "requires_positive_net_pnl": True, "no_worse_drawdown": True,
          "execution_only": True, "requires_flat_promotion": True}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def valid_parameters(parameters: Any) -> bool:
    return isinstance(parameters, dict) and set(parameters) == set(BOUNDS) and all(
        type(value) in (int, float) and math.isfinite(value) and BOUNDS[key][0] <= value <= BOUNDS[key][1]
        and (key == "max_limit_slippage_percent" or type(value) is int) for key, value in parameters.items()
    )


def fixed_fingerprint(config: BotConfig) -> str:
    fixed = {key: value for key, value in asdict(config).items() if key not in BOUNDS}
    return hashlib.sha256(json.dumps(fixed, sort_keys=True).encode()).hexdigest()


def merge_steps(paths: list[Path], *, now: datetime, max_bytes: int = 128_000_000) -> list[dict[str, Any]]:
    if sum(path.stat().st_size for path in paths) > max_bytes:
        raise ValueError("dataset exceeds bounded replay memory budget; partition it before research")
    grouped: dict[datetime, dict[str, Any]] = {}
    for path in paths:
        for step in load_timeline(path):
            stamp = datetime.fromisoformat(str(step["time"]))
            if stamp.tzinfo is None or stamp > now:
                raise ValueError("naive or future historical timestamp")
            merged = grouped.setdefault(stamp, {"time": stamp.isoformat(), "snapshots": [], "quotes": [], "confirmations": {}, "shortable": {}, "corporate_actions": [], "halted": []})
            for name in ("snapshots", "quotes"):
                by_symbol = {item["symbol"]: item for item in merged[name]}
                by_symbol.update({item["symbol"]: item for item in step.get(name, [])})
                merged[name] = list(by_symbol.values())
            for symbol, reports in step.get("confirmations", {}).items():
                merged["confirmations"].setdefault(symbol, {}).update(reports)
            merged["shortable"].update(step.get("shortable", {}))
            merged["halted"] = sorted(set(merged["halted"]) | set(step.get("halted", [])))
            for action in step.get("corporate_actions", []):
                if action not in merged["corporate_actions"]:
                    merged["corporate_actions"].append(action)
    return [grouped[stamp] for stamp in sorted(grouped)]


def audit_dataset(directory: Path, now: datetime, config: BotConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = read_json(directory / "manifest.json", {})
    reasons = []
    for key in ("dataset_id", "source", "point_in_time", "includes_delisted", "corporate_actions_complete"):
        valid = isinstance(manifest.get(key), str) and bool(manifest[key].strip()) if key in ("dataset_id", "source") else manifest.get(key) is True
        if not valid:
            reasons.append(f"missing verified manifest declaration: {key}")
    paths = []
    for name in manifest.get("files", []):
        path = (directory / name).resolve()
        if directory.resolve() not in path.parents or path.suffix not in (".jsonl", ".json") or not path.is_file():
            raise ValueError("history file must be an existing dataset-local JSON/JSONL file")
        paths.append(path)
    steps = merge_steps(paths, now=now)
    from .historical_inputs import HistoricalInputArchive
    archive = HistoricalInputArchive(directory.parent / "webull" / "inputs" / "archive")
    augmented = archive.overlay_steps(steps, directory.parent / "webull" / "reconstructed-features.jsonl")
    if not steps:
        reasons.append("no historical timeline")
    days = sorted({datetime.fromisoformat(step["time"]).date() for step in steps})
    maximum_available = manifest.get("coverage_mode") == "maximum_available"
    coverage_sufficient = False
    if maximum_available:
        coverage_sufficient = (len(days) >= 90 and manifest.get("available_window_verified") is True
                               and manifest.get("available_start") == str(days[0])
                               and manifest.get("available_end") == str(days[-1])
                               and days[-1] >= (now - timedelta(days=7)).date())
        if not coverage_sufficient:
            reasons.append("maximum-available window is unverified or has fewer than 90 trading dates")
    else:
        import calendar
        cutoff = now.date().replace(year=now.year - 5, day=min(now.day, calendar.monthrange(now.year - 5, now.month)[1]))
        coverage_sufficient = bool(len(days) >= 1000 and days[0] <= cutoff and days[-1] >= (now - timedelta(days=7)).date())
        if not coverage_sufficient:
            reasons.append("five-year trading-day coverage unavailable")
    universe = manifest.get("universe", [])
    symbols = {item.get("symbol") for item in universe if isinstance(item, dict)}
    by_symbol = {item["symbol"]: item for item in universe if isinstance(item, dict) and item.get("symbol")}
    if not any(isinstance(item, dict) and item.get("delisted_at") for item in universe):
        reasons.append("point-in-time delisted universe unavailable")
    signal_counts = {name: 0 for name in ("capital_flow", "analysts", "news", "patterns", "orderflow")}
    fields = {"capital_flow": "large_flow_ratio", "analysts": "analyst_buy_ratio", "news": "news_article_count", "patterns": "pattern_score", "orderflow": "orderflow_score"}
    executable_quotes = 0
    for step in steps:
        when = datetime.fromisoformat(step["time"])
        for snapshot in step["snapshots"]:
            if snapshot["symbol"] not in symbols:
                reasons.append("historical candidate absent from point-in-time universe")
            else:
                member = by_symbol[snapshot["symbol"]]
                listed = member.get("listed_at")
                delisted = member.get("delisted_at")
                if not listed or when.date().isoformat() < listed or (delisted and when.date().isoformat() > delisted):
                    reasons.append("candidate outside verified listing interval")
        for quote in step["quotes"]:
            values = [float(quote.get(key, 0)) for key in ("bid", "ask", "last")]
            if not all(math.isfinite(value) and value > 0 for value in values) or values[0] > values[1]:
                reasons.append("invalid quote prices")
            if quote.get("bid_volume") is not None and quote.get("ask_volume") is not None:
                executable_quotes += 1
        for reports in step["confirmations"].values():
            for report in reports.values():
                if not report.get("asof"):
                    reasons.append("historical signal timestamp unavailable")
                    continue
                asof = datetime.fromisoformat(str(report["asof"]))
                if asof.tzinfo is None or asof > when:
                    reasons.append("future or unverified signal timestamp")
                if not isinstance(report.get("signals_present"), int) or not isinstance(report.get("passed"), bool) or not math.isfinite(float(report.get("score", float("nan")))):
                    reasons.append("invalid historical confirmation")
                for group, field in fields.items():
                    signal_counts[group] += int(report.get(field) is not None and (field != "news_article_count" or report[field] > 0))
    if not executable_quotes:
        reasons.append("executable quote-depth data unavailable")
    if not all(signal_counts.values()):
        reasons.append("one or more historical signal groups unavailable; never fabricated")
    if config.allow_shorts and not any(step["shortable"] for step in steps):
        reasons.append("point-in-time borrow availability unavailable")
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode())
    for path in paths:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                fingerprint.update(chunk)
    for chunk in json.JSONEncoder(sort_keys=True, allow_nan=False).iterencode(steps):
        fingerprint.update(chunk.encode())
    return {"ready": not reasons, "reasons": sorted(set(reasons)), "dataset_id": manifest.get("dataset_id"),
            "source": manifest.get("source"), "first_day": str(days[0]) if days else None,
            "last_day": str(days[-1]) if days else None, "trading_days": len(days), "symbols": len(symbols),
            "signal_observations": signal_counts, "executable_quotes": executable_quotes,
            "historical_fields_augmented": augmented,
            "coverage_mode": "maximum_available" if maximum_available else "five_years", "coverage_sufficient": coverage_sufficient,
            "fingerprint": fingerprint.hexdigest()}, steps


def acceptable(baseline: dict[str, Any], candidate: dict[str, Any], minimum: int, *, improvement: bool = True) -> bool:
    keys = ("net_pnl", "drawdown", "loss_severity")
    if any(not isinstance(result, dict) or any(not math.isfinite(float(result.get(key, float("nan")))) for key in keys)
           or result.get("errors") or result.get("open_exposure") or result.get("trades", 0) < minimum for result in (baseline, candidate)):
        return False
    return candidate["net_pnl"] > 0 and (candidate["net_pnl"] > baseline["net_pnl"] if improvement else candidate["net_pnl"] >= baseline["net_pnl"]) and candidate["drawdown"] <= baseline["drawdown"] and candidate["loss_severity"] <= baseline["loss_severity"]


class ResearchPipeline:
    def __init__(self, root: str | Path, live_data: str | Path,
                 evaluator: Callable = run_research_backtest) -> None:
        self.root = Path(root).resolve()
        self.live_data = Path(live_data).resolve()
        if self.root == Path.cwd().resolve() or (self.root / "equity_performance.json").exists():
            raise ValueError("research state must be isolated from the account execution directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self.evaluator = evaluator

    def initialize(self, config: BotConfig) -> None:
        if not (self.root / "policy.json").exists():
            write_json(self.root / "policy.json", POLICY)
        if not (self.root / "baseline.json").exists():
            write_json(self.root / "baseline.json", asdict(config))

    def evaluate(self, steps: list[dict[str, Any]], config: BotConfig, stress: ExecutionStress | None = None) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="replay-", dir=self.root) as directory:
            return self.evaluator(steps, config, Path(directory), stress)

    def save(self, state: dict[str, Any], status: str, **details: Any) -> dict[str, Any]:
        if not status.startswith("blocked_"):
            state["stage"] = status
        state.update(status=status, **details)
        write_json(self.root / "state.json", state)
        write_json(self.root / "report.json", state)
        return state

    def run(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        lock = self.root / ".worker-lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return {"status": "worker_already_running"}
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        try:
            return self._run(now)
        finally:
            lock.unlink(missing_ok=True)

    def _run(self, now: datetime) -> dict[str, Any]:
        config = BotConfig(**read_json(self.root / "baseline.json"))
        state = read_json(self.root / "state.json", {"version": 1, "experiments": []})
        if read_json(self.root / "policy.json") != POLICY:
            return self.save(state, "blocked_policy", reason="approved execution bounds/gates do not match")
        from .historical_inputs import HistoricalInputArchive
        webull = self.root / "webull"
        archive = HistoricalInputArchive(webull / "inputs" / "archive")
        archive.export_bar_features(webull / "bars", webull / "reconstructed-features.jsonl", now=now)
        audit, steps = audit_dataset(self.root / "history", now, config)
        state["audit"] = audit
        if not audit["ready"]:
            return self.save(state, "blocked_data")
        selected = state.get("candidate")
        if selected and selected["dataset_fingerprint"] != audit["fingerprint"]:
            terminal = state.get("stage", state["status"]) in ("promoted", "rolled_back", "rejected_validation", "rejected_holdout", "rejected_stress")
            if not terminal or audit["dataset_id"] in state["experiments"] or not state.get("tested_through") or audit["last_day"] <= state["tested_through"]:
                return self.save(state, "blocked_dataset_changed", reason="freeze dataset through holdout/shadow validation; new experiments need new holdout data")
            write_json(self.root / "reports" / f"{selected['version']}.json", state)
            previous_holdout = state["tested_through"]
            config = BotConfig(**read_json(self.root / "active-baseline.json", asdict(config)))
            write_json(self.root / "baseline.json", asdict(config))
            state = {"version": 1, "experiments": state["experiments"], "audit": audit, "prior_holdout_end": previous_holdout}
            selected = None
        elif selected and state["status"].startswith("blocked_") and state.get("stage"):
            state["status"] = state["stage"]
        if selected is None:
            if audit["dataset_id"] in state["experiments"]:
                return self.save(state, "blocked_repeated_holdout")
            days = sorted({datetime.fromisoformat(step["time"]).date() for step in steps})
            holdout_day = days[int(len(days) * 0.8)]
            if state.get("prior_holdout_end"):
                fresh = [day for day in days if day.isoformat() > state["prior_holdout_end"]]
                if not fresh:
                    return self.save(state, "blocked_new_holdout")
                holdout_day = fresh[0]
            development = [step for step in steps if datetime.fromisoformat(step["time"]).date() < holdout_day]
            holdout = [step for step in steps if datetime.fromisoformat(step["time"]).date() >= holdout_day]
            development_days = sorted({datetime.fromisoformat(step["time"]).date() for step in development})
            validation_start = development_days[int(len(development_days) * 0.6)]
            training = [step for step in development if datetime.fromisoformat(step["time"]).date() < validation_start]
            state["benchmark"] = self.evaluate(steps, config)
            baseline_training = self.evaluate(training, config)
            candidates = []
            for name, values in (("max_limit_slippage_percent", (0.1, 0.3, 0.5)), ("entry_chase_polls", (1, 2, 4)), ("exit_chase_polls", (1, 2, 4))):
                for value in values:
                    parameters = {key: getattr(config, key) for key in BOUNDS}
                    parameters[name] = value
                    if parameters == {key: getattr(config, key) for key in BOUNDS}:
                        continue
                    result = self.evaluate(training, replace(config, **parameters))
                    if acceptable(baseline_training, result, MINIMUMS["training"]):
                        candidates.append((result["net_pnl"], parameters, result))
            state["baseline_training"] = baseline_training
            if not candidates:
                return self.save(state, "no_supported_refinement")
            _, parameters, training_result = max(candidates, key=lambda item: item[0])
            selected = {"parameters": parameters, "selected_at": now.isoformat(), "dataset_fingerprint": audit["fingerprint"],
                        "version": hashlib.sha256(json.dumps(parameters, sort_keys=True).encode() + audit["fingerprint"].encode()).hexdigest()[:16],
                        "training": training_result, "holdout_start": str(holdout_day), "validation_start": str(validation_start)}
            state["candidate"] = selected
            state["tested_through"] = audit["last_day"]
            self.save(state, "validating_walk_forward")
        if state["status"] in ("validating_walk_forward", "validating_holdout", "validating_stress"):
            if not self.validate_candidate(state, steps, config):
                return state
        if state["status"] not in ("awaiting_forward_shadow", "promoted"):
            return state
        feedback = read_json(self.root / "rollback.json", {})
        if feedback.get("version") == selected["version"]:
            return self.save(state, "rolled_back", rollback=feedback)
        if state["status"] == "promoted":
            return state
        forward = merge_steps(sorted(self.live_data.glob("*.jsonl")), now=now)
        forward = [step for step in forward if datetime.fromisoformat(step["time"]) > datetime.fromisoformat(selected["selected_at"])]
        if not forward:
            return self.save(state, "awaiting_forward_shadow", shadow={"paired_trades": 0, "reason": "no post-selection observations"})
        base = self.evaluate(forward, config)
        candidate = self.evaluate(forward, replace(config, **selected["parameters"]))
        paired = len(set(base.get("trade_keys", [])) & set(candidate.get("trade_keys", [])))
        state["shadow"] = {"baseline": base, "candidate": candidate, "paired_trades": paired}
        if paired < MINIMUMS["shadow"] or not acceptable(base, candidate, MINIMUMS["shadow"]):
            return self.save(state, "awaiting_forward_shadow")
        promotion = {"version": selected["version"], "parameters": selected["parameters"], "previous": {key: getattr(config, key) for key in BOUNDS},
                     "promoted_at": now.isoformat(), "policy": POLICY, "fixed_fingerprint": fixed_fingerprint(config),
                     "training": {"baseline": state["baseline_training"], "candidate": selected["training"]},
                     "evidence": {name: state[name] for name in ("audit", "walk_forward", "holdout", "stress", "shadow")}}
        write_json(self.root / "promotion.json", promotion)
        return self.save(state, "promoted")

    def validate_candidate(self, state: dict[str, Any], steps: list[dict[str, Any]], config: BotConfig) -> bool:
        selected = state["candidate"]
        candidate_config = replace(config, **selected["parameters"])
        validation_steps = [step for step in steps if selected["validation_start"] <= datetime.fromisoformat(step["time"]).date().isoformat() < selected["holdout_start"]]
        validation_days = sorted({datetime.fromisoformat(step["time"]).date() for step in validation_steps})
        state.setdefault("walk_forward", [])
        for index in range(len(state["walk_forward"]), 3):
            selected_days = set(validation_days[index * len(validation_days) // 3:(index + 1) * len(validation_days) // 3])
            fold = [step for step in validation_steps if datetime.fromisoformat(step["time"]).date() in selected_days]
            base = self.evaluate(fold, config)
            candidate = self.evaluate(fold, candidate_config)
            state["walk_forward"].append({"baseline": base, "candidate": candidate})
            if not acceptable(base, candidate, MINIMUMS["validation"]):
                state["experiments"].append(state["audit"]["dataset_id"])
                self.save(state, "rejected_validation")
                return False
            self.save(state, "validating_walk_forward")
        holdout = [step for step in steps if datetime.fromisoformat(step["time"]).date().isoformat() >= selected["holdout_start"]]
        self.save(state, "validating_holdout")
        if "holdout" not in state:
            state["tested_through"] = max(datetime.fromisoformat(step["time"]).date().isoformat() for step in holdout)
            state["experiments"].append(state["audit"]["dataset_id"])
            state["holdout"] = {"baseline": self.evaluate(holdout, config), "candidate": self.evaluate(holdout, candidate_config)}
            self.save(state, "validating_holdout")
        if not acceptable(state["holdout"]["baseline"], state["holdout"]["candidate"], MINIMUMS["holdout"]):
            self.save(state, "rejected_holdout")
            return False
        state.setdefault("stress", [])
        scenarios = (ExecutionStress(spread_multiplier=2, slippage_bps=10), ExecutionStress(partial_fraction=0.5, reject_every=7),
                     ExecutionStress(outage_every=5, disable_borrow=True))
        for scenario in scenarios[len(state["stress"]):]:
            base = self.evaluate(holdout, config, scenario)
            candidate = self.evaluate(holdout, candidate_config, scenario)
            state["stress"].append({"scenario": asdict(scenario), "baseline": base, "candidate": candidate})
            if not acceptable(base, candidate, 1, improvement=False):
                self.save(state, "rejected_stress")
                return False
            self.save(state, "validating_stress")
        self.save(state, "awaiting_forward_shadow")
        return True


def valid_promotion(promotion: Any) -> bool:
    try:
        if not isinstance(promotion, dict) or promotion["policy"] != POLICY or not valid_parameters(promotion["parameters"]) or not valid_parameters(promotion["previous"]):
            return False
        evidence = promotion["evidence"]
        audit = evidence["audit"]
        minimum_days = 90 if audit.get("coverage_mode") == "maximum_available" else 1000
        if audit["ready"] is not True or audit["trading_days"] < minimum_days or audit.get("coverage_sufficient", minimum_days == 1000) is not True:
            return False
        expected = hashlib.sha256(json.dumps(promotion["parameters"], sort_keys=True).encode() + evidence["audit"]["fingerprint"].encode()).hexdigest()[:16]
        if promotion["version"] != expected or not acceptable(**promotion["training"], minimum=MINIMUMS["training"]):
            return False
        if len(evidence["walk_forward"]) != 3 or not all(acceptable(**fold, minimum=MINIMUMS["validation"]) for fold in evidence["walk_forward"]):
            return False
        if not acceptable(**evidence["holdout"], minimum=MINIMUMS["holdout"]):
            return False
        if len(evidence["stress"]) != 3 or not all(acceptable(item["baseline"], item["candidate"], 1, improvement=False) for item in evidence["stress"]):
            return False
        shadow = evidence["shadow"]
        actual_pairs = len(set(shadow["baseline"].get("trade_keys", [])) & set(shadow["candidate"].get("trade_keys", [])))
        if actual_pairs < MINIMUMS["shadow"] or shadow["paired_trades"] != actual_pairs or not acceptable(shadow["baseline"], shadow["candidate"], MINIMUMS["shadow"]):
            return False
        return math.isfinite(float(shadow["baseline"]["closed_drawdown"]))
    except (KeyError, TypeError, ValueError):
        return False


class ResearchWorker:
    def __init__(self, root: Path, data: Path) -> None:
        self.root = root
        self.data = data
        self.process = None
        self.last_launch = 0.0

    def tick(self, config: BotConfig) -> None:
        import subprocess
        import sys
        import time

        if self.process is not None and self.process.poll() is None:
            return
        clock = time.monotonic()
        if self.last_launch and clock - self.last_launch < 3600:
            return
        self.last_launch = clock
        pipeline = ResearchPipeline(self.root, self.data)
        pipeline.initialize(config)
        write_json(self.root / "active-baseline.json", asdict(config))
        environment = {key: value for key, value in os.environ.items() if key.upper() in
                       ("PATH", "PYTHONPATH", "SYSTEMROOT", "WINDIR", "HOME", "LANG", "LC_ALL", "TMP", "TEMP")}
        with (self.root / "worker.log").open("a", encoding="utf-8") as output:
            self.process = subprocess.Popen([sys.executable, "-m", "god_help_tradebot.research", "--root", str(self.root), "--data", str(self.data)],
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=environment)

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
                self.process.wait()
        lock = self.root / ".worker-lock"
        if self.process is not None and lock.exists() and lock.read_text() == str(self.process.pid):
            lock.unlink(missing_ok=True)


class ExecutionController:
    def __init__(self, root: Path) -> None:
        self.root = root

    def restore(self, loop: Any) -> None:
        refinements = loop.performance_store.data["execution_refinements"]
        active = refinements["active"]
        if active is not None:
            if not valid_parameters(active["parameters"]):
                raise ValueError("invalid persisted execution refinement")
            loop._base_config = replace(loop._base_config, **active["parameters"])
            loop.config = loop.performance_store.effective_config(loop._base_config)

    def tick(self, loop: Any, summary: Any) -> None:
        from .monitor import LoopEvent

        ledger = loop.performance_store
        if ledger is None:
            return
        refinements = ledger.data["execution_refinements"]
        if ledger.data["paused"] or ledger.data["active"] or ledger.data["orders"] or ledger.data.get("allocation") or loop._pending_limit or loop._pending_exits:
            return
        if any(position.quantity for position in loop.broker.get_positions().values()):
            return
        active = refinements["active"]
        if active is not None:
            trades = ledger.data["trades"][active["through_trade"]:]
            if len(trades) >= MINIMUMS["shadow"]:
                equity = peak = 1.0
                drawdown = 0.0
                net = 0.0
                for trade in trades[-MINIMUMS["shadow"]:]:
                    pnl = float(trade["realized_pnl_usd"]) - 0.005 * (trade["entry_quantity"] + trade["exit_quantity"])
                    net += pnl
                    equity *= 1 + pnl / float(trade["entry_notional"])
                    peak = max(peak, equity)
                    drawdown = max(drawdown, (peak - equity) / peak)
                if net < 0 or drawdown > active["baseline_drawdown"]:
                    loop._base_config = replace(loop._base_config, **active["previous"])
                    loop.config = ledger.effective_config(loop._base_config)
                    refinements["rejected"].append(active["version"])
                    refinements["history"].append({"action": "rollback", "version": active["version"], "at": loop._now().isoformat(),
                                                   "net_pnl_after_modeled_fees": net, "closed_drawdown": drawdown})
                    refinements["active"] = None
                    ledger.save()
                    write_json(self.root / "rollback.json", refinements["history"][-1])
                    summary.events.append(LoopEvent("research_rollback", "account", "execution refinement reverted after forward deterioration"))
            return
        promotion = read_json(self.root / "promotion.json")
        if promotion is None or promotion.get("version") in refinements["rejected"]:
            return
        if read_json(self.root / "policy.json") != POLICY or not valid_promotion(promotion) or promotion.get("fixed_fingerprint") != fixed_fingerprint(loop._base_config):
            summary.events.append(LoopEvent("research_promotion_blocked", "account", "approved execution bounds or validation evidence failed"))
            return
        if hasattr(loop.broker, "assert_no_remote_open_orders"):
            loop.broker.assert_no_remote_open_orders()
        previous = {key: getattr(loop._base_config, key) for key in BOUNDS}
        refinements["active"] = {"version": promotion["version"], "parameters": promotion["parameters"], "previous": previous,
                                  "through_trade": len(ledger.data["trades"]), "baseline_drawdown": promotion["evidence"]["shadow"]["baseline"]["closed_drawdown"]}
        refinements["history"].append({"action": "promotion", "version": promotion["version"], "at": loop._now().isoformat(),
                                       "parameters": promotion["parameters"], "previous": previous})
        ledger.save()
        loop._base_config = replace(loop._base_config, **promotion["parameters"])
        loop.config = ledger.effective_config(loop._base_config)
        summary.events.append(LoopEvent("research_promotion", "account", f"applied approved execution version {promotion['version']} while flat"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated, credential-free strategy research")
    parser.add_argument("--root", default="research")
    parser.add_argument("--data", default="data")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    if hasattr(os, "nice"):
        os.nice(10)
    if os.name == "posix":
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = 384 * 1024 * 1024
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        if soft != resource.RLIM_INFINITY:
            limit = min(limit, soft)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    pipeline = ResearchPipeline(args.root, args.data)
    from .botconfig import load_bot_config
    default_config = replace(load_bot_config(), daily_peak_mode=True, highest_score_priority=True, full_account_allocation=True, research_enabled=True)
    config = BotConfig(**read_json(pipeline.root / "baseline.json", asdict(default_config)))
    pipeline.initialize(config)
    try:
        if args.audit:
            report, _ = audit_dataset(pipeline.root / "history", datetime.now(timezone.utc), config)
            write_json(pipeline.root / "audit.json", report)
        else:
            report = pipeline.run()
        print(json.dumps(report, indent=2, allow_nan=False))
    except Exception as error:
        write_json(pipeline.root / "error.json", {"error": type(error).__name__, "detail": str(error)})
        raise


if __name__ == "__main__":
    main()