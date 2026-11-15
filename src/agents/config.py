"""Load and validate roles.toml: the tiers, the leaf pool and the limits."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.harness import HARNESS_NAMES

ENV_CONFIG = "AGENTS_CONFIG"
USER_CONFIG = Path.home() / ".config" / "agents" / "roles.toml"
# The default ships at the top of the repo, two levels above this package (src/agents/).
REPO_CONFIG = Path(__file__).resolve().parents[2] / "roles.toml"


class ConfigError(Exception):
    """roles.toml is missing or doesn't make sense."""


@dataclass(frozen=True)
class PoolMember:
    """One model a leaf task can be routed to."""

    name: str
    harness: str
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class Tier:
    name: str
    can_split: bool
    # Splitter tiers name one harness and model. The leaf tier has a pool instead.
    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    pool: tuple[PoolMember, ...] = ()

    def splitter_agent(self) -> PoolMember:
        """A splitter tier's harness and model, in the same shape as a leaf pool member."""
        if not self.harness or not self.model:
            raise ConfigError(f"tier '{self.name}' has no harness or model")
        return PoolMember(self.name, self.harness, self.model, self.effort)


@dataclass(frozen=True)
class Limits:
    max_depth: int
    run_budget_usd: float
    leaf_max_files: int
    leaf_max_task_words: int
    leaf_max_attempts: int
    node_timeout_s: int = 3600
    test_timeout_s: int = 600


@dataclass(frozen=True)
class Config:
    tiers: tuple[Tier, ...]
    limits: Limits
    source: Path

    @property
    def leaf_tier(self) -> Tier:
        return self.tiers[-1]

    def tier_for_depth(self, depth: int) -> Tier:
        """The tier a node at this depth belongs to. The root is depth 0."""
        last_splitter_depth = len(self.tiers) - 2
        if depth >= self.limits.max_depth or depth > last_splitter_depth:
            return self.leaf_tier
        return self.tiers[depth]


def config_path() -> Path:
    """$AGENTS_CONFIG, then ~/.config/agents/roles.toml, then the repo's default."""
    from_env = os.environ.get(ENV_CONFIG)
    if from_env:
        return Path(from_env)
    if USER_CONFIG.exists():
        return USER_CONFIG
    return REPO_CONFIG


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError as error:
        raise ConfigError(f"config file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error

    try:
        tiers = tuple(_parse_tier(entry) for entry in data["tier"])
        limits = Limits(**data["limits"])
    except (KeyError, TypeError) as error:
        raise ConfigError(f"{path}: {error}") from error

    config = Config(tiers=tiers, limits=limits, source=path)
    _validate(config)
    return config


def _parse_tier(entry: dict[str, Any]) -> Tier:
    pool = tuple(PoolMember(**member) for member in entry.get("pool", []))
    return Tier(
        name=entry["name"],
        can_split=entry["can_split"],
        harness=entry.get("harness"),
        model=entry.get("model"),
        effort=entry.get("effort"),
        pool=pool,
    )


def _validate(config: Config) -> None:
    where = str(config.source)
    if len(config.tiers) < 2:
        raise ConfigError(f"{where}: need at least a splitter tier and a leaf tier")

    *splitters, leaf = config.tiers
    for tier in splitters:
        if not tier.can_split:
            raise ConfigError(f"{where}: only the last tier may have can_split = false")
        if not tier.harness or not tier.model:
            raise ConfigError(f"{where}: tier '{tier.name}' needs a harness and a model")
        _check_harness(where, tier.name, tier.harness)

    if leaf.can_split:
        raise ConfigError(f"{where}: the last tier must be the leaf tier (can_split = false)")
    if not leaf.pool:
        raise ConfigError(f"{where}: the leaf tier needs a pool with at least one member")
    for member in leaf.pool:
        _check_harness(where, f"{leaf.name}/{member.name}", member.harness)

    limits = config.limits
    if limits.max_depth < 1:
        raise ConfigError(f"{where}: max_depth must be at least 1")
    if limits.leaf_max_attempts < 1:
        raise ConfigError(f"{where}: leaf_max_attempts must be at least 1")
    if limits.run_budget_usd <= 0:
        raise ConfigError(f"{where}: run_budget_usd must be more than 0")


def _check_harness(where: str, owner: str, harness: str) -> None:
    if harness not in HARNESS_NAMES:
        known = ", ".join(HARNESS_NAMES)
        raise ConfigError(f"{where}: '{owner}' uses unknown harness '{harness}' (known: {known})")
