import tomllib

import pytest

from trading_god.config import TEMPLATE_DIR, Settings, load_settings, parse_settings, settings_path


def test_template_matches_the_built_in_defaults():
    with (TEMPLATE_DIR / "desk.toml").open("rb") as handle:
        assert parse_settings(tomllib.load(handle)) == Settings()


def test_defaults_are_moomoo_us_paper_on_localhost():
    settings = Settings()
    assert (settings.moomoo.security_firm, settings.moomoo.market) == ("FUTUINC", "US")
    assert settings.web.host == "127.0.0.1"


def test_user_file_wins_over_the_template():
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text('[risk]\nmax_order_pct = 2.5\nblocked_symbols = ["gme"]\n[web]\nport = 9000\n', encoding="utf-8")
    settings = load_settings()
    assert settings.risk.max_order_pct == 2.5
    assert settings.risk.blocked_symbols == ("US.GME",)
    assert settings.web.port == 9000
    assert settings.risk.max_position_pct == 10.0


@pytest.mark.parametrize(
    "data",
    [
        {"risk": {"max_order_pct": "5"}},
        {"risk": {"max_buys_per_day": 2.5}},
        {"risk": {"allow_market_orders": 1}},
        {"risk": {"blocked_symbols": "GME"}},
        {"risk": {"max_order_pc": 5}},
        {"risky": {}},
        {"risk": {"max_order_pct": 0}},
    ],
)
def test_bad_settings_are_rejected(data):
    with pytest.raises(ValueError):
        parse_settings(data)


def test_bad_file_names_the_file():
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("[risk\n", encoding="utf-8")
    with pytest.raises(ValueError, match="desk.toml"):
        load_settings()
