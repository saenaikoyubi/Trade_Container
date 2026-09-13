import json

from trade_common.config import Settings


def test_settings_are_mainnet_only(tmp_path):
    config_file = tmp_path / "settings.json"
    config_file.write_text(
        json.dumps(
            {
                "exchanges": {
                    "binance": {
                        "adapter": "ccxt",
                        "symbols": ["BTC/USDT"],
                        "fees": {"maker": "0.0025", "taker": "0.004"},
                    },
                    "dydx": {
                        "adapter": "dydx",
                        "symbols": ["BTC-USD"],
                        "fees": {"maker": "0.001", "taker": "0.002"},
                    },
                },
                "poll_interval_seconds": 1,
                "market_data_max_age_seconds": 10,
                "risk": {
                    "max_order_quantity": "0.1",
                    "max_order_notional": "1000",
                    "max_position_notional": "2000",
                    "max_daily_loss": "100",
                    "max_price_deviation_pct": "0.05",
                    "max_orders_per_minute": 20,
                },
            }
        ),
        encoding="utf-8",
    )

    config = Settings.from_file(config_file)

    assert config.exchange_network == "mainnet"
    assert config.exchanges["binance"].adapter == "ccxt"
    assert config.exchanges["dydx"].symbols == ("BTC-USD",)
    assert not hasattr(config, "sandbox")
    assert not hasattr(config, "dydx_indexer_url")


def test_settings_parse_exchange_options_and_paper_account(tmp_path):
    config_file = tmp_path / "settings.json"
    config_file.write_text(
        json.dumps(
            {
                "exchanges": {
                    "bybit": {
                        "adapter": "ccxt",
                        "options": {"defaultType": "linear"},
                        "symbols": ["BTCUSDT"],
                        "fees": {"maker": "0.0002", "taker": "0.00055"},
                    }
                },
                "account": {
                    "currency": "USDT",
                    "initial_balance": "25000",
                    "default_leverage": "5",
                },
                "risk": {
                    "max_order_quantity": "10",
                    "max_order_notional": "50000",
                    "max_position_notional": "100000",
                    "max_daily_loss": "5000",
                    "max_price_deviation_pct": "0.05",
                    "max_orders_per_minute": 60,
                },
            }
        ),
        encoding="utf-8",
    )

    config = Settings.from_file(config_file)

    assert config.exchanges["bybit"].options == {"defaultType": "linear"}
    assert config.account.currency == "USDT"
    assert str(config.account.initial_balance) == "25000"
    assert str(config.account.default_leverage) == "5"
