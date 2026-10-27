"""Wire the desk together from ``desk.toml``."""

from __future__ import annotations

from trading_god.broker import MoomooPaperBroker
from trading_god.config import Settings, database_path, load_settings
from trading_god.desk import Desk
from trading_god.notify import telegram_notifier
from trading_god.prices import first_price, yahoo_last_price
from trading_god.store import ProposalStore


def build_desk(settings: Settings | None = None) -> Desk:
    settings = settings or load_settings()
    broker = MoomooPaperBroker(settings.moomoo)
    return Desk(
        settings,
        ProposalStore(database_path()),
        broker,
        first_price(broker.last_price, yahoo_last_price),
        notifier=telegram_notifier(settings.telegram),
    )
