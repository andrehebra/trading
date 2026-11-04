import os
import pytest
os.environ.setdefault("APP_MASTER_KEY", "h4c9CJZZy_4FMBz9cU1EpuEwPYUUnva7grXAUDFJIpY=")
os.environ.setdefault("DATABASE_PATH", "/tmp/prediction-market-test.db")

from app.main import KalshiAdapter, fast_kalshi_crypto, fast_kalshi_directional, fee, in_entry_band, kalshi_order_body, normalize_market


def test_polymarket_crypto_taker_fee_formula():
    assert fee("polymarket_us", 100, 0.5, False) == 1.75
    assert fee("polymarket_us", 100, 0.5, True) == 0


def test_kalshi_prices_are_normalized_from_cents():
    market = normalize_market("kalshi", {"ticker": "KXBTC", "title": "Bitcoin", "yes_bid": 42, "yes_ask": 45})
    assert market["yes_bid"] == 0.42
    assert market["yes_ask"] == 0.45
    assert market["no_bid"] == 0.55
    assert market["no_ask"] == 0.58


def test_fast_kalshi_filter_only_accepts_15_minute_crypto_series():
    assert fast_kalshi_crypto({"ticker": "KXBTC15M-26NOV031200-00"})
    assert fast_kalshi_crypto({"series_ticker": "KXSOL15M"})
    assert not fast_kalshi_crypto({"ticker": "KXPRESPARTY-27NOV03"})
    assert not fast_kalshi_crypto({"ticker": "KXSOL-27NOV03"})


def test_directional_filter_adds_exact_15_minute_commodity_series_only():
    assert fast_kalshi_directional({"ticker": "KXGOLD15M-26NOV031200-00"})
    assert fast_kalshi_directional({"series_ticker": "KXWTI15M"})
    assert not fast_kalshi_directional({"ticker": "KXGOLD-27NOV03"})


def test_kalshi_no_order_uses_complementary_yes_ask():
    body = kalshi_order_body("KXGOLD15M-test", "NO", 0.96, 1, "unique-order")
    assert body["side"] == "ask"
    assert body["price"] == "0.0400"
    assert body["ticker"] == "KXGOLD15M-test"


def test_kalshi_key_error_is_actionable_and_secret_free():
    with pytest.raises(ValueError, match="complete unencrypted PEM"):
        KalshiAdapter._headers("GET", "/trade-api/v2/markets/test/orderbook", {"key_id": "key", "secret_key": "not-a-pem"})


def test_entry_band_includes_exact_bounds_only():
    assert not in_entry_band(0.899, 0.90, 0.98)
    assert in_entry_band(0.90, 0.90, 0.98)
    assert in_entry_band(0.98, 0.90, 0.98)
    assert not in_entry_band(0.981, 0.90, 0.98)
