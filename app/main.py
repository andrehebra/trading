import asyncio
import base64
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("market-bot")
DATA = Path(os.getenv("DATABASE_PATH", "data/trader.db"))
POLL_SECONDS = max(5, int(os.getenv("POLL_SECONDS", "15")))
KALSHI = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "live": "https://api.elections.kalshi.com/trade-api/v2",
}
POLYMARKET_US = "https://api.polymarket.us"
CRYPTO_TERMS = ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "dogecoin", "doge", "xrp")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def db() -> sqlite3.Connection:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA)
    conn.row_factory = sqlite3.Row
    return conn


def setup_db() -> None:
    with db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS credentials (provider TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, market_id TEXT NOT NULL, event_id TEXT,
          outcome TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, price REAL NOT NULL,
          quantity INTEGER NOT NULL, estimated_fee REAL NOT NULL, exchange_order_id TEXT,
          client_order_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          error TEXT, UNIQUE(provider, market_id, outcome, mode, status)
        );
        CREATE TABLE IF NOT EXISTS fills (
          id TEXT PRIMARY KEY, order_id TEXT NOT NULL, price REAL NOT NULL, quantity INTEGER NOT NULL,
          fee REAL NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(order_id) REFERENCES orders(id)
        );
        CREATE TABLE IF NOT EXISTS bot_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        defaults = {
          "mode": "paper", "armed": False, "running": False, "kill_switch": False,
          "provider": "kalshi", "environment": "demo", "threshold": 0.45,
          "position_dollars": 10, "max_limit": 0.45, "maker_only": False,
          "daily_cap": 25, "total_cap": 100, "max_open_exposure": 50,
          "stale_seconds": 45,
        }
        for key, value in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, json.dumps(value)))


def get_setting(key: str) -> Any:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def settings() -> dict[str, Any]:
    with db() as c:
        rows = c.execute("SELECT key,value FROM settings").fetchall()
    return {row["key"]: json.loads(row["value"]) for row in rows}


def set_settings(values: dict[str, Any]) -> None:
    with db() as c:
        for key, value in values.items():
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, json.dumps(value)))


def event(level: str, name: str, detail: str) -> None:
    log.log({"error": logging.ERROR, "warning": logging.WARNING}.get(level, logging.INFO), "%s: %s", name, detail)
    with db() as c:
        c.execute("INSERT INTO bot_events(level,event,detail,created_at) VALUES (?,?,?,?)", (level, name, detail[:1000], utc_now()))


class Vault:
    def __init__(self) -> None:
        raw = os.getenv("APP_MASTER_KEY", "")
        if not raw:
            raise RuntimeError("APP_MASTER_KEY is required; refusing to start without encrypted credential storage")
        try:
            self.fernet = Fernet(raw.encode())
        except Exception as exc:
            raise RuntimeError("APP_MASTER_KEY must be a valid Fernet key") from exc

    def save(self, provider: str, values: dict[str, str]) -> None:
        token = self.fernet.encrypt(json.dumps(values).encode())
        with db() as c:
            c.execute("INSERT OR REPLACE INTO credentials(provider,ciphertext,updated_at) VALUES (?,?,?)", (provider, token, utc_now()))

    def read(self, provider: str) -> dict[str, str] | None:
        with db() as c:
            row = c.execute("SELECT ciphertext FROM credentials WHERE provider=?", (provider,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(self.fernet.decrypt(row["ciphertext"]).decode())
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("credential cannot be decrypted; check APP_MASTER_KEY") from exc

    def configured(self) -> list[str]:
        with db() as c:
            return [r["provider"] for r in c.execute("SELECT provider FROM credentials").fetchall()]


vault: Vault


class Credentials(BaseModel):
    provider: Literal["kalshi", "polymarket_us"]
    key_id: str = Field(min_length=3, max_length=300)
    secret_key: str = Field(min_length=20, max_length=20000, description="Kalshi PEM or Polymarket US base64 Ed25519 secret")


class Config(BaseModel):
    provider: Literal["kalshi", "polymarket_us"] = "kalshi"
    environment: Literal["demo", "live"] = "demo"
    mode: Literal["paper", "live"] = "paper"
    threshold: float = Field(0.45, gt=0.01, lt=0.99)
    position_dollars: float = Field(10, gt=0, le=10000)
    max_limit: float = Field(0.45, gt=0.01, lt=0.99)
    maker_only: bool = False
    daily_cap: float = Field(25, gt=0, le=100000)
    total_cap: float = Field(100, gt=0, le=1000000)
    max_open_exposure: float = Field(50, gt=0, le=100000)
    stale_seconds: int = Field(45, ge=10, le=300)


class ArmRequest(BaseModel):
    phrase: str


def normalize_market(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    if provider == "kalshi":
        return {
            "provider": provider, "id": raw.get("ticker", ""), "event_id": raw.get("event_ticker", ""),
            "title": raw.get("title") or raw.get("subtitle") or raw.get("ticker", ""),
            "close_time": raw.get("close_time") or raw.get("expiration_time"),
            "yes_bid": cents(raw.get("yes_bid") or raw.get("yes_bid_dollars")),
            "yes_ask": cents(raw.get("yes_ask") or raw.get("yes_ask_dollars")), "raw": raw,
        }
    return {
        "provider": provider, "id": raw.get("slug") or str(raw.get("id", "")), "event_id": str(raw.get("eventId", "")),
        "title": raw.get("title") or raw.get("question") or raw.get("slug", ""),
        "close_time": raw.get("closeTime") or raw.get("endDate"),
        "yes_bid": number(raw.get("bestBid")), "yes_ask": number(raw.get("bestAsk")), "raw": raw,
    }


def number(value: Any) -> float | None:
    try:
        if isinstance(value, dict):
            value = value.get("value")
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def cents(value: Any) -> float | None:
    val = number(value)
    return None if val is None else (val / 100 if val > 1 else val)


class KalshiAdapter:
    provider = "kalshi"

    async def markets(self, environment: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(f"{KALSHI[environment]}/markets", params={"status": "open", "limit": 200})
            response.raise_for_status()
        values = response.json().get("markets", [])
        return [normalize_market(self.provider, v) for v in values if crypto(v)]

    async def book(self, market_id: str, environment: str, credentials: dict[str, str] | None = None) -> dict[str, Any]:
        headers = self._headers("GET", f"/trade-api/v2/markets/{market_id}/orderbook", credentials) if credentials else {}
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(f"{KALSHI[environment]}/markets/{market_id}/orderbook", headers=headers)
            response.raise_for_status()
            return response.json().get("orderbook", response.json())

    async def order(self, market: dict[str, Any], price: float, quantity: int, credentials: dict[str, str], environment: str, client_order_id: str, maker_only: bool) -> str:
        path = "/trade-api/v2/portfolio/events/orders"
        body = {"ticker": market["id"], "client_order_id": client_order_id, "side": "bid", "count": quantity,
                "price": f"{price:.4f}", "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross"}
        headers = self._headers("POST", path, credentials)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(KALSHI[environment] + "/portfolio/events/orders", headers=headers, json=body)
            response.raise_for_status()
        return str(response.json().get("order_id", client_order_id))

    @staticmethod
    def _headers(method: str, path: str, credentials: dict[str, str] | None) -> dict[str, str]:
        if not credentials:
            return {}
        timestamp = str(int(time.time() * 1000))
        key = serialization.load_pem_private_key(credentials["secret_key"].encode(), password=None)
        signature = key.sign(f"{timestamp}{method}{path}".encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": credentials["key_id"], "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}


class PolymarketUSAdapter:
    provider = "polymarket_us"

    async def markets(self, environment: str) -> list[dict[str, Any]]:
        # The official SDK uses the production public API; no public US sandbox is documented.
        from polymarket_us import PolymarketUS
        def fetch() -> list[dict[str, Any]]:
            client = PolymarketUS(timeout=12)
            try:
                data = client.markets.list({"limit": 200, "active": True})
                values = data.get("markets", [])
                # List responses contain metadata, not BBO. Enrich the filtered set before
                # strategy evaluation so the threshold always sees a contemporaneous quote.
                out = []
                for value in (v for v in values if crypto(v)):
                    slug = value.get("slug")
                    if not slug:
                        continue
                    try:
                        value = {**value, **client.markets.bbo(slug).get("marketData", {})}
                    except Exception:
                        continue
                    out.append(normalize_market(self.provider, value))
                return out
            finally:
                client.close()
        return await asyncio.to_thread(fetch)

    async def book(self, market_id: str, environment: str, credentials: dict[str, str] | None = None) -> dict[str, Any]:
        from polymarket_us import PolymarketUS
        client = PolymarketUS(timeout=12)
        try:
            return (await asyncio.to_thread(client.markets.book, market_id)).get("marketData", {})
        finally:
            client.close()

    async def order(self, market: dict[str, Any], price: float, quantity: int, credentials: dict[str, str], environment: str, client_order_id: str, maker_only: bool) -> str:
        from polymarket_us import PolymarketUS
        client = PolymarketUS(key_id=credentials["key_id"], secret_key=credentials["secret_key"], timeout=15, max_retries=0)
        body = {"marketSlug": market["id"], "intent": "ORDER_INTENT_BUY_LONG", "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{price:.4f}", "currency": "USD"}, "quantity": quantity,
                "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL", "participateDontInitiate": maker_only}
        try:
            result = await asyncio.to_thread(client.orders.create, body)
        finally:
            client.close()
        return str(result.get("id", client_order_id))


def crypto(value: dict[str, Any]) -> bool:
    text = " ".join(str(value.get(k, "")) for k in ("title", "subtitle", "ticker", "category", "tags")).lower()
    return any(term in text for term in CRYPTO_TERMS)


def adapter(provider: str):
    return KalshiAdapter() if provider == "kalshi" else PolymarketUSAdapter()


def fee(provider: str, contracts: int, price: float, maker_only: bool) -> float:
    # Polymarket crypto: current published taker formula. Maker orders are zero-fee.
    if maker_only:
        return 0.0
    return round(contracts * 0.07 * price * (1 - price), 5) if provider == "polymarket_us" else 0.0


def spend(kind: str) -> float:
    day_clause = "AND substr(created_at,1,10)=?" if kind == "daily" else ""
    args: tuple[Any, ...] = (datetime.now(UTC).date().isoformat(),) if kind == "daily" else ()
    with db() as c:
        row = c.execute(f"SELECT COALESCE(SUM(price*quantity+estimated_fee),0) AS total FROM orders WHERE status IN ('open','filled') {day_clause}", args).fetchone()
    return float(row["total"])


def open_exposure() -> float:
    with db() as c:
        row = c.execute("SELECT COALESCE(SUM(price*quantity),0) AS total FROM orders WHERE status IN ('open','filled')").fetchone()
    return float(row["total"])


def duplicate(provider: str, market_id: str, mode: str) -> bool:
    with db() as c:
        return c.execute("SELECT 1 FROM orders WHERE provider=? AND market_id=? AND outcome='YES' AND mode=? AND status IN ('open','filled')", (provider, market_id, mode)).fetchone() is not None


async def place(market: dict[str, Any], observed_ask: float | None) -> str:
    cfg = settings()
    if cfg["kill_switch"] or not cfg["running"]:
        return "disabled"
    if observed_ask is None or observed_ask > cfg["threshold"] or observed_ask > cfg["max_limit"]:
        return "not eligible"
    if duplicate(cfg["provider"], market["id"], cfg["mode"]):
        return "duplicate blocked"
    cap = min(cfg["max_limit"], cfg["threshold"])
    # A maker price rests one tick below the displayed ask instead of crossing it.
    price = min(cap, round(observed_ask - 0.01, 4)) if cfg["maker_only"] else cap
    if price <= 0:
        return "maker price unavailable"
    quantity = int((Decimal(str(cfg["position_dollars"])) / Decimal(str(price))).to_integral_value(rounding=ROUND_DOWN))
    if quantity < 1:
        return "size too small"
    estimated_fee = fee(cfg["provider"], quantity, price, cfg["maker_only"])
    total = quantity * price + estimated_fee
    if spend("daily") + total > cfg["daily_cap"] or spend("total") + total > cfg["total_cap"] or open_exposure() + quantity * price > cfg["max_open_exposure"]:
        event("warning", "risk_cap_block", f"{market['id']} blocked by cap")
        return "risk cap blocked"
    oid, coid = str(uuid.uuid4()), str(uuid.uuid4())
    now = utc_now()
    with db() as c:
        c.execute("INSERT INTO orders(id,provider,market_id,event_id,outcome,mode,status,price,quantity,estimated_fee,client_order_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (oid, cfg["provider"], market["id"], market.get("event_id"), "YES", cfg["mode"], "pending", price, quantity, estimated_fee, coid, now, now))
    try:
        if cfg["mode"] == "live":
            cred = vault.read(cfg["provider"])
            if not cred:
                raise RuntimeError("no credentials for selected provider")
            exchange_id = await adapter(cfg["provider"]).order(market, price, quantity, cred, cfg["environment"], coid, cfg["maker_only"])
            status = "open"
        else:
            exchange_id = f"paper-{oid[:8]}"
            # Never pretend a resting maker quote immediately filled in paper mode.
            status = "open" if cfg["maker_only"] else "filled"
            if status == "filled":
                with db() as c:
                    c.execute("INSERT INTO fills(id,order_id,price,quantity,fee,created_at) VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), oid, observed_ask, quantity, fee(cfg["provider"], quantity, observed_ask, False), now))
        with db() as c:
            c.execute("UPDATE orders SET status=?,exchange_order_id=?,updated_at=? WHERE id=?", (status, exchange_id, utc_now(), oid))
        event("info", "order_placed", f"{cfg['mode']} {cfg['provider']} {market['id']} x{quantity}")
        return "placed"
    except Exception as exc:
        with db() as c:
            c.execute("UPDATE orders SET status='error',error=?,updated_at=? WHERE id=?", (str(exc)[:500], utc_now(), oid))
        event("error", "order_error", f"{market['id']}: {type(exc).__name__}")
        return "error"


async def worker() -> None:
    while True:
        try:
            cfg = settings()
            if cfg["running"] and not cfg["kill_switch"]:
                markets = await adapter(cfg["provider"]).markets(cfg["environment"])
                for market in markets[:100]:
                    # Market listing BBO is time-stamped by this poll; never use stale cached data.
                    await place(market, market.get("yes_ask"))
        except Exception as exc:
            event("error", "poll_error", type(exc).__name__)
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global vault
    setup_db()
    vault = Vault()
    task = asyncio.create_task(worker())
    yield
    task.cancel()


app = FastAPI(title="Prediction Market Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def home(): return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
async def status():
    cfg = settings()
    return {"config": cfg, "credentials": vault.configured(), "daily_spend": spend("daily"), "total_spend": spend("total"), "open_exposure": open_exposure(), "poll_seconds": POLL_SECONDS}


@app.put("/api/config")
async def update_config(config: Config):
    values = config.model_dump()
    if values["mode"] == "live" and values["environment"] != "live":
        raise HTTPException(400, "Live mode requires the live environment")
    set_settings(values)
    return {"ok": True, "config": settings()}


@app.post("/api/credentials")
async def save_credentials(value: Credentials):
    vault.save(value.provider, value.model_dump(exclude={"provider"}))
    event("info", "credentials_saved", value.provider)
    return {"ok": True, "provider": value.provider, "stored": True}


@app.post("/api/arm")
async def arm(value: ArmRequest):
    cfg = settings()
    if value.phrase != "ENABLE LIVE TRADING":
        raise HTTPException(400, "Enter the exact phrase to arm live trading")
    if cfg["mode"] != "live" or cfg["environment"] != "live" or cfg["provider"] not in vault.configured():
        raise HTTPException(400, "Select live mode/environment and save credentials first")
    set_settings({"armed": True, "kill_switch": False})
    event("warning", "live_armed", cfg["provider"])
    return {"ok": True}


@app.post("/api/start")
async def start():
    cfg = settings()
    if cfg["mode"] == "live" and not cfg["armed"]:
        raise HTTPException(400, "Live trading is not armed")
    set_settings({"running": True, "kill_switch": False})
    event("info", "bot_started", cfg["mode"])
    return {"ok": True}


@app.post("/api/kill")
async def kill():
    # Stops all automation immediately. Existing live orders are preserved for manual review; no blind cancellation.
    set_settings({"running": False, "kill_switch": True, "armed": False})
    event("warning", "kill_switch", "automation stopped; inspect existing exchange orders")
    return {"ok": True}


@app.get("/api/markets")
async def markets(provider: Literal["kalshi", "polymarket_us"] = "kalshi", environment: Literal["demo", "live"] = "demo"):
    try:
        return {"markets": await adapter(provider).markets(environment), "fetched_at": utc_now()}
    except Exception as exc:
        raise HTTPException(502, f"market data unavailable: {type(exc).__name__}")


@app.get("/api/book/{provider}/{market_id}")
async def book(provider: Literal["kalshi", "polymarket_us"], market_id: str, environment: Literal["demo", "live"] = "demo"):
    try:
        cred = vault.read(provider)
        return {"book": await adapter(provider).book(market_id, environment, cred), "fetched_at": utc_now()}
    except Exception as exc:
        raise HTTPException(502, f"order book unavailable: {type(exc).__name__}")


@app.get("/api/orders")
async def orders():
    with db() as c:
        rows = c.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"orders": [dict(r) for r in rows]}


@app.get("/api/portfolio")
async def portfolio():
    """Mark bot-owned positions to the newest visible YES bid; never use a cached mark."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM orders WHERE status IN ('open','filled') ORDER BY created_at DESC").fetchall()]
    cfg = settings()
    marks: dict[tuple[str, str], float | None] = {}
    failures: list[str] = []
    providers = {row["provider"] for row in rows}
    for provider in providers:
        try:
            listed = await adapter(provider).markets(cfg["environment"])
            by_id = {market["id"]: market for market in listed}
            for row in rows:
                if row["provider"] == provider:
                    marks[(provider, row["market_id"])] = by_id.get(row["market_id"], {}).get("yes_bid")
        except Exception:
            failures.append(provider)
    total_cost = total_mark = total_fees = 0.0
    positions = []
    for row in rows:
        cost = row["price"] * row["quantity"]
        mark = marks.get((row["provider"], row["market_id"]))
        total_cost += cost
        total_fees += row["estimated_fee"]
        if mark is not None:
            total_mark += mark * row["quantity"]
        positions.append({"provider": row["provider"], "market_id": row["market_id"], "quantity": row["quantity"], "cost_basis": cost, "mark": mark,
                          "unrealized_pnl": None if mark is None else round(mark * row["quantity"] - cost - row["estimated_fee"], 4)})
    return {"positions": positions, "cost_basis": round(total_cost, 4), "marked_value": round(total_mark, 4), "estimated_fees": round(total_fees, 4),
            "unrealized_pnl": None if failures else round(total_mark - total_cost - total_fees, 4), "mark_failures": failures}


@app.get("/api/activity")
async def activity():
    with db() as c:
        rows = c.execute("SELECT * FROM bot_events ORDER BY id DESC LIMIT 100").fetchall()
    return {"events": [dict(r) for r in rows]}
