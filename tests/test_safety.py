import os
import base64
import sqlite3
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
os.environ.setdefault("APP_MASTER_KEY", "h4c9CJZZy_4FMBz9cU1EpuEwPYUUnva7grXAUDFJIpY=")
os.environ.setdefault("DATABASE_PATH", "/tmp/prediction-market-test.db")

from app.main import KalshiAdapter, fast_kalshi_crypto, fast_kalshi_directional, fee, in_entry_band, kalshi_order_body, migrate_orders_schema, normalize_market


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
    body = kalshi_order_body("KXGOLD15M-test", "NO", 0.96, 1, "unique-order", maker_only=True)
    assert body["side"] == "ask"
    assert body["price"] == "0.0400"
    assert body["ticker"] == "KXGOLD15M-test"
    assert body["count"] == "1.00"
    assert body["post_only"] is True


def test_kalshi_key_error_is_actionable_and_secret_free():
    with pytest.raises(ValueError, match="complete unencrypted PEM"):
        KalshiAdapter._headers("GET", "/trade-api/v2/markets/test/orderbook", {"key_id": "key", "secret_key": "not-a-pem"})


def test_kalshi_ed25519_pem_signs_the_raw_authentication_payload():
    private_key = ed25519.Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    path = "/trade-api/v2/portfolio/events/orders"
    headers = KalshiAdapter._headers("POST", path, {"key_id": "key-id", "secret_key": pem})
    payload = f"{headers['KALSHI-ACCESS-TIMESTAMP']}POST{path}".encode()
    private_key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), payload)


def test_legacy_status_unique_constraint_migrates_without_losing_attempt_history():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE orders (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, market_id TEXT NOT NULL, event_id TEXT,
          outcome TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, price REAL NOT NULL,
          quantity INTEGER NOT NULL, estimated_fee REAL NOT NULL, exchange_order_id TEXT,
          client_order_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          error TEXT, UNIQUE(provider, market_id, outcome, mode, status)
        );
        INSERT INTO orders VALUES ('one','kalshi','market','event','YES','live','error',.9,1,0,NULL,'client-one','now','now','rejected');
    """)
    migrate_orders_schema(conn)
    conn.execute("INSERT INTO orders VALUES ('two','kalshi','market','event','YES','live','error',.9,1,0,NULL,'client-two','now','now','rejected again')")
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2


def test_entry_band_includes_exact_bounds_only():
    assert not in_entry_band(0.899, 0.90, 0.98)
    assert in_entry_band(0.90, 0.90, 0.98)
    assert in_entry_band(0.98, 0.90, 0.98)
    assert not in_entry_band(0.981, 0.90, 0.98)
