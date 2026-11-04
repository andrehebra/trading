# Market Guard — self-hosted prediction-market bot

Market Guard is a paper-first crypto prediction-market trading dashboard for **Kalshi** and **Polymarket US**. For Kalshi, discovery is intentionally focused on active 15-minute Up/Down crypto windows: Bitcoin, Ethereum, Solana, XRP, and Dogecoin. It shows order books, persists orders and fills in SQLite, and can automate a capped YES-entry strategy. It is designed as a small, inspectable deployment—not investment advice or a promise of profitability.

## Safety posture

- Starts in **paper mode**, paused. Live execution requires: live environment, live mode, saved credentials, and entering `ENABLE LIVE TRADING` to arm it.
- Credentials are encrypted at rest with a mandatory `APP_MASTER_KEY`; secret values are accepted only by the server and are never returned by any API or rendered in the browser.
- Every decision is persisted before any order attempt. A provider/market/outcome/mode record with an `open` or `filled` status blocks a duplicate after a restart.
- Dollar sizing, daily and lifetime allocated-spend caps, and maximum open exposure are checked before submission. Limit price is always capped.
- The worker stops on the kill switch and catches data/API failures without trading. It only acts on prices fetched in the current polling pass—there is no stale cached-price execution.
- “Maker-only” prices one tick below the displayed ask and will not submit a buy that meets or exceeds that ask. Polymarket US additionally receives its documented `participateDontInitiate` flag. In live mode the limit order uses GTC; confirm each venue’s current post-only semantics before enabling it.

The kill switch halts new automated orders immediately and disarms live mode. It intentionally does **not** blindly cancel existing live exchange orders, so a network/API mismatch cannot turn an emergency stop into an unexpected cancellation. Inspect and cancel them in the venue or extend the reviewed adapter cancellation path for your operating policy.

## Quick start

1. Install Docker Desktop.
2. Copy the example environment and generate a unique key:

   ```sh
   cp .env.example .env
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Paste the generated value after `APP_MASTER_KEY=` in `.env`. Do not change this key after saving credentials; it is what decrypts them.

3. Start the app:

   ```sh
   docker compose up --build -d
   ```

4. On the server itself, open [http://localhost:8000](http://localhost:8000). From another device on your trusted home network, open `http://<server-LAN-IP>:8000` (for example, `http://192.168.1.50:8000`). The included `.env` binds to the LAN by default; set `DASHBOARD_BIND=127.0.0.1` to make it server-only again. Do not expose it to the public Internet without an authenticated reverse proxy and TLS.

5. Use the dashboard in paper mode first. Save API credentials only when you need authenticated order books or live execution. The browser clears the secret form after a successful submission.

6. To use live trading, select the exchange, choose **Live** environment and **Live trading** mode, save the appropriate live credential, save controls, enter the exact arm phrase, then press Start. Begin with a small cap.

To stop:

```sh
docker compose down
```

Persistent encrypted credentials, orders, fills, and audit logs live in `./data/trader.db`. Back it up together with the `APP_MASTER_KEY`; neither can recover the credentials without the other.

## Exchange integration notes (verified 2026-11-03)

| Venue | Market data | Live authentication and order path | Test environment |
| --- | --- | --- | --- |
| Kalshi | Most market-list calls are public; order book may require credentials. | RSA-PSS request signature with key ID + downloaded PEM. V2 order writes go to `/portfolio/events/orders` and require a client order ID for idempotency. | Separate demo account/keys at `demo.kalshi.co`; demo base: `https://demo-api.kalshi.co/trade-api/v2`. |
| Polymarket US | Public market and book methods are exposed in the official `polymarket-us` SDK. | Ed25519 API credentials; official SDK creates a limit buy with `ORDER_INTENT_BUY_LONG`. API access requires application, integration testing, and approval. | The official developer page says qualification includes sandbox integration testing; its public SDK does not document a general self-serve sandbox base URL, so this adapter uses paper mode until approved credentials/access are provided. |

The Polymarket adapter uses the official SDK to avoid reimplementing the signed protocol. The Kalshi adapter signs the currently documented V2 REST shape directly. APIs change: run paper mode and test a single demo/API order after an upgrade, and review exchange responses before arming production.

If a Kalshi order book asks for authentication, paste the full **unencrypted PEM private-key file** downloaded with that production API key into Credential Vault—not an API secret string. The app first attempts a public book and will only sign a request when Kalshi returns an authentication challenge.

### Fees

The bot estimates Polymarket crypto taker fees as `contracts × 0.07 × price × (1 − price)`, rounded to 5 decimals, and estimates maker fees as zero. This follows Polymarket’s current published crypto fee schedule; the exchange remains authoritative at match time. Kalshi’s fee schedule is market/series dependent, so the bot records zero *estimate* for Kalshi rather than inventing a fee. Set a conservative lower spend cap to cover actual fees, and reconcile fills against the exchange before relying on P&L.

## What the strategy does

On each poll, the selected venue is queried for open crypto markets. For each market, it considers buying YES only when the current displayed YES ask is no higher than both the entry threshold and the limit cap. It sizes contracts as `floor(position dollars / limit price)`, estimates fees, runs caps and duplicate checks, then either records a paper fill at the observed ask or submits a live capped limit order. In maker-only paper mode, it records a resting order rather than inventing an immediate fill.

Market discovery and order-book representation vary by venue. A displayed book is raw exchange data to preserve depth/side fields. The dashboard records orders, fills, estimated fees, spend, exposure, and safety events. It marks bot-owned open/filled YES positions to the latest discovered YES bid and displays that as unrealized P&L; it shows unavailable rather than using an old mark on data failure. Do not treat paper fills or marked P&L as an executable price guarantee.

## Local development and checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export APP_MASTER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
uvicorn app.main:app --reload
pytest -q
```

## Sources

- [Kalshi API help](https://help.kalshi.com/en/articles/13823854-kalshi-api) and [current SDK quickstart](https://github.com/TexasCoding/kalshi-python-sdk/blob/main/docs/getting-started.md)
- [Polymarket US official Python SDK](https://github.com/Polymarket/polymarket-us-python) and [developer access requirements](https://polymarketexchange.com/developers.html)
- [Polymarket fee schedule](https://help.polymarket.com/en/articles/13364478-trading-fees)
