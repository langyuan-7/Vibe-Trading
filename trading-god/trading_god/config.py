"""Settings and file locations for the Trading God desk.

The user's live settings sit in ``~/.trading-god/desk.toml`` (outside the git
checkout, so a ``git pull`` never touches them). The installer copies
``config/desk.toml`` there once and never overwrites it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from trading_god.risk import RiskLimits

#: The ``trading-god`` folder of the checkout (this package is installed editable).
HUB_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = HUB_ROOT / "config"
SKILLS_DIR = HUB_ROOT / "skills"

_HOME_ENV = "TRADING_GOD_HOME"


def home_dir() -> Path:
    """Return the per-user state folder (``~/.trading-god`` unless overridden)."""
    override = os.environ.get(_HOME_ENV, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".trading-god"


def settings_path() -> Path:
    return home_dir() / "desk.toml"


def database_path() -> Path:
    return home_dir() / "desk.db"


@dataclass(frozen=True)
class MoomooSettings:
    """Where moomoo OpenD listens and which paper account to use."""

    host: str = "127.0.0.1"
    port: int = 11111
    #: moomoo US is FUTUINC; FUTUSG, FUTUAU, FUTUCA, FUTUJP, FUTUMY for other regions.
    security_firm: str = "FUTUINC"
    market: str = "US"
    #: 0 picks the first US paper stock account OpenD reports.
    acc_id: int = 0


@dataclass(frozen=True)
class ProposalSettings:
    #: A proposal nobody decided on within this many hours expires unplaced.
    ttl_hours: float = 12.0


@dataclass(frozen=True)
class ExitSettings:
    """Selling at a stop without asking: the stop was part of the buy a person approved."""

    #: Sell a position at market as soon as its price is at or below its stop, in market hours.
    auto_stop: bool = True
    #: How often the desk checks prices against stops while it runs.
    check_seconds: int = 60
    #: Wait this long before trying again after an automatic sell for the same stock.
    retry_minutes: int = 30

    def __post_init__(self) -> None:
        if self.check_seconds < 10:
            raise ValueError("exits.check_seconds must be at least 10")
        if self.retry_minutes < 1:
            raise ValueError("exits.retry_minutes must be at least 1")


@dataclass(frozen=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class TelegramSettings:
    """Optional phone ping when a proposal arrives. Empty values turn it off."""

    bot_token: str = ""
    chat_id: str = ""


@dataclass(frozen=True)
class ScheduleSettings:
    """When Vibe-Trading runs the pre-market scan."""

    vibe_url: str = "http://127.0.0.1:8899"
    cron: str = "35 8 * * 1-5"
    timezone: str = "America/New_York"
    max_proposals_per_run: int = 3


@dataclass(frozen=True)
class Settings:
    moomoo: MoomooSettings = field(default_factory=MoomooSettings)
    risk: RiskLimits = field(default_factory=RiskLimits)
    proposals: ProposalSettings = field(default_factory=ProposalSettings)
    exits: ExitSettings = field(default_factory=ExitSettings)
    web: WebSettings = field(default_factory=WebSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)


_SECTIONS = {
    "moomoo": MoomooSettings,
    "risk": RiskLimits,
    "proposals": ProposalSettings,
    "exits": ExitSettings,
    "web": WebSettings,
    "telegram": TelegramSettings,
    "schedule": ScheduleSettings,
}


def _build(section: str, cls: type, values: Any) -> Any:
    if not isinstance(values, dict):
        raise ValueError(f"[{section}] must be a table")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValueError(f"unknown setting(s) in [{section}]: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in values.items():
        default = known[name].default
        if isinstance(default, tuple):
            if not isinstance(value, list):
                raise ValueError(f"{section}.{name} must be a list")
            value = tuple(str(item) for item in value)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{section}.{name} must be true or false")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
                raise ValueError(f"{section}.{name} must be a whole number")
            value = int(value)
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{section}.{name} must be a number")
            value = float(value)
        elif isinstance(default, str):
            value = str(value)
        kwargs[name] = value
    return cls(**kwargs)


def parse_settings(data: dict[str, Any]) -> Settings:
    """Build :class:`Settings` from parsed TOML, rejecting unknown keys."""
    unknown = sorted(set(data) - set(_SECTIONS))
    if unknown:
        raise ValueError(f"unknown section(s) in desk.toml: {', '.join(unknown)}")
    built = {name: _build(name, cls, data[name]) for name, cls in _SECTIONS.items() if name in data}
    return Settings(**built)


def load_settings(path: Path | None = None) -> Settings:
    """Read ``desk.toml``; fall back to the bundled template, then to defaults."""
    for candidate in (path, settings_path(), TEMPLATE_DIR / "desk.toml"):
        if candidate is not None and candidate.is_file():
            with candidate.open("rb") as handle:
                try:
                    return parse_settings(tomllib.load(handle))
                except (tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
                    raise ValueError(f"{candidate}: {exc}") from exc
    return Settings()
