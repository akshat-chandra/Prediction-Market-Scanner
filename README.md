# Prediction Market Scanner

Live WebSocket scanner for Kalshi and Polymarket. Monitors real-time order books on both platforms, fires microstructure signals on arb opportunities and book imbalances, and tracks simulated P&L with closing line value (CLV) tracking. Paper trading only -- no real orders are placed.

---

## Setup

```bash
pip install -r requirements.txt

export KALSHI_API_KEY_ID="your_key_id"
export KALSHI_PRIVATE_KEY_PATH="/path/to/private_key.pem"

python3.11 scanner_loop.py
```

Optional flags:

```bash
python3.11 scanner_loop.py --threshold 2 --bankroll 1000
```

`--threshold`: minimum edge in cents to fire a signal (default: 3)
`--bankroll`: paper bankroll for position sizing (default: 500)

Use `python3.11` explicitly. The live WebSocket connection to Kalshi requires valid API credentials. Pre-game REST polling runs without credentials but Kalshi WS mode will not start without them.

---

## Architecture

The scanner runs two parallel modes depending on whether a game has started:

**Pre-game (upcoming markets):** REST polling every 3 minutes. Fetches fresh prices from both platforms and runs signal checks on each matched pair.

**Live (in-game markets):** Event-driven WebSocket connections to both platforms. Signal checks fire on every order book delta with no fixed poll interval.

Both paths feed into the same signal checker and paper trade tracker.

```
                    +-----------------+
                    |  scanner_loop   |
                    +-----------------+
                     /              \
        Kalshi WS Client       Poly WS Client
        (orderbook_delta)      (price_change)
                     \              /
                    +-----------------+
                    |  SignalChecker  |
                    +-----------------+
                    /   |    |    \
               ARB STALE IMB  LARGE
                    +-----------------+
                    | PaperTradeTracker|
                    +-----------------+
                          |
                    scanner_log.csv
```

---

## File Map

```
scanner_loop.py          -- main entry point
config.py                -- API credentials and all trading params
requirements.txt
src/
    kalshi_client.py     -- Kalshi REST + WebSocket client (RSA-PSS auth)
    polymarket_client.py -- Polymarket CLOB REST + WebSocket client
    order_flow.py        -- microstructure signal functions
    cross_platform.py    -- Kalshi / Polymarket game matcher
```

---

## Modules

### `scanner_loop.py`

Contains all scanner orchestration logic.

**`LocalBook` / `PolyLocalBook`**

In-memory order book state maintained from WebSocket deltas. Kalshi prices arrive as fractions (0.0-1.0) and are converted to cents. Polymarket prices arrive as decimals and are also converted to cents. Both expose `best_bid()`, `best_ask()`, and `mid()`. In Kalshi's binary market format, a NO bid at price P is a YES ask at (100 - P), so both sides of the book are derived from the same feed.

**`KalshiWSClient`**

Async WebSocket client for Kalshi's `orderbook_delta` channel. Auth uses RSA-PSS signing via HTTP upgrade headers (same signing logic as REST). Applies full snapshots on connect and incremental deltas on each message. Reconnects with exponential backoff (1s, 2s, 4s... up to 64s) on any disconnect.

**`PolyWSClient`**

Async WebSocket client for Polymarket's CLOB feed. Subscribes by token ID. Handles `book` events (full snapshot) and `price_change` events (delta). Also reconnects with exponential backoff.

**`GameRegistry`**

Tracks all known active games and their platform identifiers (Kalshi ticker and Polymarket token ID). Classifies games as upcoming (before `game_start_time`) or live (after `game_start_time`).

**`SignalChecker`**

Runs all microstructure checks on a matched Kalshi + Polymarket book pair. Called on every WebSocket delta (live path) or REST poll (pre-game path). Handles all spam filtering internally -- see Filters section below.

**`PaperTradeTracker`**

Logs all fired signals to `scanner_log.csv`. Tracks open paper positions in memory. When a market settles, writes resolution and P&L back to the original signal row in the CSV.

**`Scanner`**

Top-level orchestrator. Runs `KalshiWSClient`, `PolyWSClient`, and the discovery loop concurrently via `asyncio.gather`.

**`discover_games()`**

Synchronous REST discovery called every 3 minutes. Fetches active markets from both platforms, matches them using Jaccard similarity on tokenized team names, deduplicates so each Kalshi market maps to at most one Polymarket market, and returns matched pairs. Filters stale markets: Kalshi filters by status and expiry in the client; Polymarket applies a 24-hour cutoff.

**`rest_scan_upcoming()`**

For each upcoming game pair, fetches a fresh order book from both platforms via REST and runs the full signal check suite. Runs inside the 3-minute discovery loop.

**`snapshot_closing_lines()`**

For upcoming games within 5 minutes of tip-off, fetches the current mid price from both platforms and stores the average as the closing line snapshot for that game. Runs on every discovery loop pass and only snapshots once per game. This is the denominator for CLV -- see Paper Trading section.

**`check_settlements()`**

Polls Kalshi for settlement status on all known live games. A market is settled when `yes_bid > 95c` (YES wins) or `< 5c` (NO wins). Passes the stored pre-game closing line snapshot to the paper tracker for CLV calculation.

---

### `config.py`

All trading parameters and API credentials in one place. No hardcoded values anywhere else.

Key params:

| Parameter | Default | Description |
|---|---|---|
| `MIN_EDGE_THRESHOLD` | 0.03 | Min edge in cents to fire a signal |
| `MAX_POSITION` | 100 | Max contracts per market |
| `MAX_PORTFOLIO_EXPOSURE` | 1000 | Max total dollar exposure |
| `DRAWDOWN_LIMIT` | 0.10 | 10% drawdown triggers position reduction |

Kalshi auth: set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` as environment variables. The private key is an RSA PEM file from your Kalshi account settings.

---

### `src/kalshi_client.py`

Kalshi REST and WebSocket client. Every REST request signs `timestamp + method + path` with RSA-PSS (SHA-256). The same signing logic generates the WebSocket auth headers.

Key methods:
- `get_sports_game_markets()`: fetches all active sports markets, filtered by status and expiry time
- `get_market_price(ticker)`: returns current bid, ask, and mid for a single market
- `ws_auth_headers()`: generates signed HTTP headers for the WebSocket upgrade request

---

### `src/polymarket_client.py`

Polymarket CLOB REST client. No auth required for reading. Uses the Gamma API for game discovery (each event has all markets embedded) and the CLOB API for live order book data.

Key methods:
- `get_game_events(sport, status)`: returns active game markets for a given sport (NBA, NHL, MLB)
- `get_orderbook(token_id)`: returns current bid/ask levels for a token in CLOB format

Prices are in decimal format (0.58 = 58 cents) and converted to cents internally.

---

### `src/order_flow.py`

Microstructure signal functions used by `SignalChecker`.

- `book_imbalance(bids, asks)`: returns `(bid_size - ask_size) / total_size`. Positive = bid-heavy. Range -1 to +1.
- `detect_large_orders(bids, asks)`: flags any resting order that is 5x or more the average level size at that depth.
- `vwap_mid(bids, asks)`: volume-weighted average of best bid and best ask.
- `check_stale_quote(k_mid, p_mid, threshold)`: returns the gap between platform mids and whether it exceeds threshold.

---

### `src/cross_platform.py`

Matches Kalshi markets to Polymarket markets representing the same game. Uses Jaccard similarity on tokenized team name strings to handle naming differences across platforms (e.g. "LA Lakers" vs "Los Angeles Lakers"). Deduplicates so each Kalshi market maps to at most one Polymarket market and vice versa.

- `find_matches(kalshi_markets, poly_markets, min_score)`: returns matched pairs above the similarity threshold
- `deduplicate_matches(matches, ...)`: keeps highest-scoring match when multiple candidates exist
- `compare_prices(kalshi_price, poly_price)`: returns gap in cents and which platform is cheaper

---

## Signals

| Signal | Condition | Sizing |
|---|---|---|
| `REAL_ARB` | Bid/ask cross: `k_ask + (100 - p_bid) < 100` or reverse | 15% of bankroll on both legs |
| `STALE_QUOTE` | Mid divergence >= threshold between platforms | Quarter-Kelly, capped at 10% of bankroll |
| `IMBALANCE` | `(bid_size - ask_size) / total >= 0.35` | Logged only, no sizing |
| `LARGE_ORDER` | Single level >= 5x average level size | Logged only, no sizing |

Real arb uses bid/ask crosses, never mids. Both legs are sized so profit is locked regardless of outcome. Directional sizing uses `bankroll * (edge / (1 - price)) * 0.25`.

---

## Filters

- **Near-resolution**: any price below 10c or above 90c on either platform skips all checks. Markets that close to resolution have no actionable edge.
- **Dedup**: REAL_ARB and STALE_QUOTE suppressed if the same signal fired on the same market within 5 minutes.
- **Imbalance cooldown**: IMBALANCE fires at most once per market per 5 minutes regardless of book movement.
- **Pre-game LARGE_ORDER suppression**: LARGE_ORDER only fires for live in-game markets. Extreme-price resting orders before tip-off are normal market-maker behavior and not informative.
- **Closed market filter**: discovery filters markets by status and expiry. Kalshi uses a status check plus `expiration_time > now - 1h`. Polymarket uses a 24-hour cutoff on `end_date` or `game_start_time + 24h`.

---

## Paper Trading and CLV Tracking

All signals are paper traded. No real orders are submitted.

On signal fire, `PaperTradeTracker` logs the trade to `scanner_log.csv` with the entry price, contracts, and action. Every 5 minutes the scanner polls Kalshi for settlement. On resolution, it writes `resolution` and `pnl` back to the original row.

**Closing Line Value (CLV):**

CLV = `closing_line_at_entry - entry_price`

The closing line is the market mid approximately 2-5 minutes before tip-off, captured by `snapshot_closing_lines()`. This is distinct from the settlement price (100c or 0c) and distinct from the entry price. Positive CLV means the position was on the right side of where the market settled before the game started -- the standard metric for evaluating whether a model is finding real edge vs. just winning on variance.

If the scanner was not running during the pre-game window, `closing_line_at_entry` is left blank rather than filled with a misleading value.

---

## CSV Schema

`scanner_log.csv` columns:

| Column | Description |
|---|---|
| `timestamp` | Signal fire time |
| `event` | Game name |
| `signal_type` | REAL_ARB / STALE_QUOTE / IMBALANCE / LARGE_ORDER |
| `kalshi_bid` / `kalshi_ask` | Kalshi book top at signal time (cents) |
| `poly_bid` / `poly_ask` | Polymarket book top at signal time (cents) |
| `imbalance` | Book imbalance ratio at signal time |
| `edge_size` | Edge in cents (ARB/STALE) or imbalance ratio (IMBALANCE) |
| `action` | Description of the paper trade taken |
| `contracts` | Paper position size |
| `entry_price` | Fill price in cents |
| `prices_updated_at` | Last book update timestamp |
| `resolution` | YES or NO on settlement |
| `pnl` | Simulated P&L in dollars |
| `closing_line_at_entry` | Pre-game mid snapshot used for CLV calculation |
