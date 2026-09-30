from dataclasses import dataclass

from .config import BotConfig


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: float
    change_percent: float
    volume: int
    average_volume: int
    bid: float
    ask: float
    day_low: float
    day_high: float

    @property
    def relative_volume(self) -> float:
        return self.volume / self.average_volume if self.average_volume else 0.0

    @property
    def spread_percent(self) -> float:
        spread = ((self.ask - self.bid) / self.price) * 100 if self.price else float("inf")
        return round(spread, 4)

    @property
    def range_position(self) -> float:
        day_range = self.day_high - self.day_low
        return (self.price - self.day_low) / day_range if day_range > 0 else 0.0


@dataclass(frozen=True)
class RankedCandidate:
    snapshot: MarketSnapshot
    score: float


def screen_candidates(
    snapshots: list[MarketSnapshot],
    config: BotConfig,
) -> list[RankedCandidate]:
    candidates = [snapshot for snapshot in snapshots if _passes_filters(snapshot, config)]
    ranked = [RankedCandidate(snapshot, _score(snapshot)) for snapshot in candidates]
    return sorted(ranked, key=lambda candidate: candidate.score, reverse=True)


def _passes_filters(snapshot: MarketSnapshot, config: BotConfig) -> bool:
    return (
        bool(snapshot.symbol.strip())
        and 0 < snapshot.price <= config.max_symbol_price
        and config.min_change_percent <= snapshot.change_percent <= config.max_change_percent
        and snapshot.volume >= config.min_average_volume
        and snapshot.relative_volume >= config.min_relative_volume
        and 0 < snapshot.bid <= snapshot.ask
        and snapshot.spread_percent <= config.max_spread_percent
        and 0 <= snapshot.range_position <= 1
    )


def _score(snapshot: MarketSnapshot) -> float:
    momentum = min(snapshot.change_percent / 20, 1.0)
    volume = min(snapshot.relative_volume / 10, 1.0)
    range_position = snapshot.range_position
    return round((momentum * 0.35) + (volume * 0.35) + (range_position * 0.30), 4)