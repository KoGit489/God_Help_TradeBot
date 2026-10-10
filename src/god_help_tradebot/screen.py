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


@dataclass(frozen=True)
class ProfiledCandidate:
    """A ranked candidate tagged with the profile that selected it."""

    candidate: RankedCandidate
    profile: str


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


# --- Pre-pop (accumulation) profile ---------------------------------------
# Catches stocks BEFORE the breakout: unusual volume building while price is
# still quiet and coiling near the day's low, rather than already spiked.

PREPOP_MIN_CHANGE_PERCENT = -2.0
PREPOP_MAX_CHANGE_PERCENT = 4.0
PREPOP_MIN_RELATIVE_VOLUME = 2.0
PREPOP_MAX_RANGE_POSITION = 0.55


def screen_prepop_candidates(
    snapshots: list[MarketSnapshot],
    config: BotConfig,
) -> list[RankedCandidate]:
    """Rank stocks showing quiet-accumulation signatures before a move."""
    candidates = [s for s in snapshots if _passes_prepop_filters(s, config)]
    ranked = [RankedCandidate(snapshot, _prepop_score(snapshot)) for snapshot in candidates]
    return sorted(ranked, key=lambda candidate: candidate.score, reverse=True)


def _passes_prepop_filters(snapshot: MarketSnapshot, config: BotConfig) -> bool:
    return (
        bool(snapshot.symbol.strip())
        and 0 < snapshot.price <= config.max_symbol_price
        and PREPOP_MIN_CHANGE_PERCENT <= snapshot.change_percent <= PREPOP_MAX_CHANGE_PERCENT
        and snapshot.volume >= config.min_average_volume
        and snapshot.relative_volume >= PREPOP_MIN_RELATIVE_VOLUME
        and 0 < snapshot.bid <= snapshot.ask
        and snapshot.spread_percent <= config.max_spread_percent
        and 0 <= snapshot.range_position <= PREPOP_MAX_RANGE_POSITION
    )


def _prepop_score(snapshot: MarketSnapshot) -> float:
    """Score quiet accumulation: relative volume is the star, tight coil helps."""
    volume = min(snapshot.relative_volume / 10, 1.0)
    # Coil: lower range position (nearer the low) is better for a pre-pop entry.
    coil = 1.0 - snapshot.range_position
    # Mild positive drift is a small plus; big pops are excluded by the filters.
    drift = min(max(snapshot.change_percent, 0.0) / PREPOP_MAX_CHANGE_PERCENT, 1.0)
    return round((volume * 0.50) + (coil * 0.30) + (drift * 0.20), 4)


def best_candidate(
    snapshots: list[MarketSnapshot],
    config: BotConfig,
) -> ProfiledCandidate | None:
    """Choose the higher-scoring candidate across momentum and pre-pop profiles."""
    momentum = screen_candidates(snapshots, config)
    prepop = screen_prepop_candidates(snapshots, config)

    top_momentum = momentum[0] if momentum else None
    top_prepop = prepop[0] if prepop else None

    if top_momentum is None and top_prepop is None:
        return None
    if top_momentum is None:
        return ProfiledCandidate(top_prepop, "prepop")
    if top_prepop is None:
        return ProfiledCandidate(top_momentum, "momentum")
    if top_momentum.score >= top_prepop.score:
        return ProfiledCandidate(top_momentum, "momentum")
    return ProfiledCandidate(top_prepop, "prepop")


# --- Fade (short) profile ---------------------------------------------------
# Overextended gainers rolling off their highs: popped big, then slid into the
# lower half of the day's range. Classic penny-stock fade setup.

FADE_MIN_CHANGE_PERCENT = 10.0
FADE_MAX_RANGE_POSITION = 0.4


def screen_fade_candidates(
    snapshots: list[MarketSnapshot],
    config: BotConfig,
) -> list[RankedCandidate]:
    """Rank overextended gainers that are fading off their highs (short setups)."""
    candidates = [s for s in snapshots if _passes_fade_filters(s, config)]
    ranked = [RankedCandidate(snapshot, _fade_score(snapshot)) for snapshot in candidates]
    return sorted(ranked, key=lambda candidate: candidate.score, reverse=True)


def _passes_fade_filters(snapshot: MarketSnapshot, config: BotConfig) -> bool:
    return (
        bool(snapshot.symbol.strip())
        and 0 < snapshot.price <= config.max_symbol_price
        and snapshot.change_percent >= FADE_MIN_CHANGE_PERCENT
        and snapshot.volume >= config.min_average_volume
        and 0 < snapshot.bid <= snapshot.ask
        and snapshot.spread_percent <= config.max_spread_percent
        and 0 <= snapshot.range_position <= FADE_MAX_RANGE_POSITION
    )


def _fade_score(snapshot: MarketSnapshot) -> float:
    """Score the fade: bigger prior pop + deeper slide off the high = better short."""
    pop = min(snapshot.change_percent / 50, 1.0)
    slide = 1.0 - snapshot.range_position
    volume = min(snapshot.relative_volume / 10, 1.0)
    return round((pop * 0.4) + (slide * 0.4) + (volume * 0.2), 4)