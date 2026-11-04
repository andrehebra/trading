import os
os.environ.setdefault("APP_MASTER_KEY", "h4c9CJZZy_4FMBz9cU1EpuEwPYUUnva7grXAUDFJIpY=")
os.environ.setdefault("DATABASE_PATH", "/tmp/prediction-market-test.db")

from app.main import fee, normalize_market


def test_polymarket_crypto_taker_fee_formula():
    assert fee("polymarket_us", 100, 0.5, False) == 1.75
    assert fee("polymarket_us", 100, 0.5, True) == 0


def test_kalshi_prices_are_normalized_from_cents():
    market = normalize_market("kalshi", {"ticker": "KXBTC", "title": "Bitcoin", "yes_bid": 42, "yes_ask": 45})
    assert market["yes_bid"] == 0.42
    assert market["yes_ask"] == 0.45
