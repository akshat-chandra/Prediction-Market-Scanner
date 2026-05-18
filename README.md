# Prediction Market WebSocket Scanner

Live microstructure scanner for Kalshi and Polymarket. Detects cross-platform arbitrage, book imbalance, stale quotes, and large orders in real time using a WebSocket + REST hybrid architecture.

---

## Quick Start

```bash
pip install -r requirements.txt

export KALSHI_API_KEY_ID="your_key_id"
export KALSHI_PRIVATE_KEY_PATH="path/to/private_key.pem"

python3 scanner_loop.py
```

Optional flags:
```bash
python3 scanner_loop.py --threshold 2 --bankroll 500
```

Output:
- Mac desktop notifications on signal events
- Terminal log of every scan and WebSocket event
- `scanner_log.csv` with bid/ask, signal type, entry price, and resolved P&L

---

## Architecture

```
├── scanner_loop.py          -- WebSocket + REST hybrid scanner entry point
├── config.py                -- API credentials and trading params
├── requirements.txt
└── src/
    ├── kalshi_client.py     -- Kalshi REST + WebSocket client (RSA-PSS auth)
    ├── polymarket_client.py -- Polymarket CLOB REST + WebSocket client
    ├── order_book.py        -- Local book state, spread, depth, imbalance
    ├── order_flow.py        -- Microstructure signal engine
    ├── cross_platform.py    -- Kalshi / Polymarket event matcher
    ├── arbitrage.py         -- Cross-platform arb detector
    ├── fair_value.py        -- Probability estimation and Kelly sizing
    ├── market_maker.py      -- Dynamic spread and inventory skew quoting
    ├── risk_manager.py      -- Position limits and drawdown controls
    ├── backtester.py        -- CLV tracking and P&L attribution
    ├── closing_line_predictor.py -- Closing line drift model
    └── paper_trader.py      -- Paper trade execution layer
```

---

## Scanner Modes

**Pre-game (REST, every 3 min)**

For each upcoming game pair, fetches fresh Kalshi and Polymarket order books via REST and runs all signal checks.

**Live / in-game (WebSocket, event-driven)**

Maintains persistent connections to both platforms:
- Kalshi: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, subscribes to `orderbook_delta`
- Polymarket: `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribes to market events

Each delta updates a local order book (`{price_cents: size}` dict). Signal checks fire on every delta from either platform with no polling delay.

---

## Signals

All signals are model-free and purely microstructure-based.

| Signal | Trigger |
|--------|---------|
| `REAL_ARB` | Bid/ask cross between platforms: `k_ask + (100 - p_bid) < 100` |
| `STALE_QUOTE` | Cross-platform mid divergence >= threshold (default 5 cents) |
| `IMBALANCE` | `(bid_vol - ask_vol) / total >= 0.35` |
| `LARGE_ORDER` | Single resting order >= 5x average size at that level |

**Active filters:**
- Near-resolution suppress: skip markets at 3 cents or 97 cents
- Dedup: same signal suppressed within a 5-minute window
- Imbalance cooldown: 2-minute per-market cooldown after IMBALANCE fires
- Closed market filter: skip resolved markets (status check + expiration > now - 1h)

---

## Modules

### `src/kalshi_client.py`

RSA-PSS signed requests to `https://api.elections.kalshi.com/trade-api/v2`. Every request signs `timestamp + method + path` with RSA-PSS (SHA-256).

Key methods:
- `get_markets(sport)` -- fetch open markets for a sport
- `get_orderbook(ticker)` -- raw order book (yes_bids, no_bids)
- `place_order(ticker, side, price, quantity)` -- blocked when `PAPER_TRADING_ONLY=True`
- `get_positions()` -- current open positions

Rate limit: 10 req/sec. Built-in token bucket stays at 8.

---

### `src/polymarket_client.py`

Read-only, no auth required. Uses the Events endpoint so each event has all markets embedded.

Key methods:
- `get_games(sport)` -- all open game events with prices
- `get_orderbook(condition_id)` -- CLOB order book (decimal prices)

Price format: decimal (0.58 = 58 cents).

---

### `src/order_book.py`

Parses Kalshi's `yes_bids` / `no_bids` format. In binary markets, a NO bid at price P is a YES ask at (100 - P).

Key functions:
- `analyze(orderbook)` -- best bid, best ask, spread, midpoint, depth, imbalance
- `detect_liquidity_gap(orderbook, gap_threshold=10)` -- gaps in the book
- `estimate_slippage(orderbook, side, quantity)` -- avg fill price vs best price

Imbalance: `(bid_vol - ask_vol) / (bid_vol + ask_vol)`. Range: -1 to +1.

---

### `src/order_flow.py`

Four signal types:

**Real Arbitrage**
```
Leg 1: BUY YES Kalshi @ k_ask + BUY NO Poly @ (100 - p_bid) < 100 = locked profit
Leg 2: BUY YES Poly @ p_ask + BUY NO Kalshi @ k_no_ask < 100 = locked profit
```

**Book Imbalance**
```python
imb = (bid_vol - ask_vol) / (bid_vol + ask_vol)
```
Threshold of +/-0.35 flags significant directional pressure.

**Large Order Detection**
Resting orders 5x+ average size at that level. Quote withdrawal is equally informative.

**Stale Quote**
Cross-platform mid divergence above threshold. The venue that moved is ground truth; the stale venue will reprice.

Supporting utilities:
- `vwap_mid(bids, asks, levels=5)` -- volume-weighted mid
- `quote_delta(snap_before, snap_after)` -- spread widening, imbalance shift, mid move
- `BookSnapshot` -- point-in-time capture for time-series comparison

---

### `src/cross_platform.py`

Matches Kalshi markets to Polymarket markets representing the same game using Jaccard token similarity on event titles. Threshold: 0.5.

- `find_matches(kalshi_markets, poly_markets)` -- list of matched pairs
- `compare_prices(kalshi_price, poly_price)` -- gap in cents and direction

---

### `src/market_maker.py`

Posts symmetric quotes around fair value with inventory skew.

```
mid = fair_value + inventory_skew   (skew = -position * skew_factor)
bid = mid - adjusted_spread
ask = mid + adjusted_spread
adjusted_spread = base_spread * confidence_mult  (HIGH=0.7, MEDIUM=1.0, LOW=1.5)
```

When long, both bid and ask shift down to attract sellers and discourage more buying.

Key methods:
- `calculate_quotes(ticker, fair_value, confidence)` -- bid/ask in cents
- `record_fill(ticker, side, price, quantity)` -- update position and P&L
- `get_portfolio_summary()` -- all open positions with current marks

---

### `src/arbitrage.py`

Odds conversion utilities and scanner for price gaps between platforms.

- `american_to_probability`, `decimal_to_probability`, `kalshi_price_to_probability`
- `compare_markets(kalshi_price_cents, sportsbook_odds, odds_format)` -- edge in probability terms, recommended action, Kelly-sized bet

---

### `src/risk_manager.py`

- Max 100 contracts per market
- Max $1,000 total portfolio exposure
- Max $300 correlated exposure (same team/game)
- 10% drawdown from peak triggers position reduction

Key methods:
- `check_position_limit(ticker, proposed_quantity)` -- pre-trade check
- `check_correlated_exposure(sport, market_type)` -- exposure by sport/type
- `update_equity(current_equity)` -- updates peak, triggers drawdown alert

---

### `src/backtester.py`

Primary metric: CLV (Closing Line Value). Entry price vs where the market settled. Positive CLV consistently means you are finding real edge.

Fee model: `fee = 0.07 * price * (1 - price)`. Max at 50 cents = $0.0175/contract.

Key methods:
- `execute_trade(entry_price, exit_price, settled, quantity)` -- records P&L
- `calculate_clv(entry_price, close_price)` -- edge vs closing line
- `get_performance_metrics()` -- ROI, Sharpe, max drawdown, CLV mean/std, win rate

---

### `src/closing_line_predictor.py`

Predicts remaining closing line drift from current price using resolved Polymarket markets.

- Source: `data/polymarket_historical.csv` (177 resolved markets)
- Target: `closing_price - current_price`
- Features: price drift, cross-platform gap, volume, imbalance, hours_to_close, category
- Model: linear regression (baseline; needs more data to improve)

---

## Paper Trading

All signals route to `PaperTradeTracker` in `src/paper_trader.py`. `PAPER_TRADING_ONLY = True` in `config.py` blocks real order submission at the API level.

Every 5 minutes the scanner polls Kalshi for settlement. On resolution, it writes `resolution`, `pnl`, and `closing_line_at_entry` back to the original CSV row for CLV analysis.

**Position sizing (paper only):**
- Arb legs: 15% of bankroll, sized equal profit on both outcomes
- Directional signals: quarter Kelly, capped at 10% of bankroll

Log schema:
```
timestamp, event, signal_type, kalshi_bid, kalshi_ask, poly_bid, poly_ask,
imbalance, edge_size, action, contracts, entry_price, prices_updated_at,
resolution, pnl, closing_line_at_entry
```

---

## Configuration

All params in `config.py` or set as environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_API_KEY_ID` | `""` | API key ID from Kalshi settings |
| `KALSHI_PRIVATE_KEY_PATH` | `""` | Path to RSA private key PEM |
| `KALSHI_WS_URL` | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | Kalshi production WebSocket |
| `POLY_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Polymarket CLOB WebSocket |
| `PAPER_TRADING_ONLY` | `True` | Blocks all real order submission |
| `DEFAULT_SPREAD_WIDTH` | `0.05` | Market maker half-spread |
| `MIN_EDGE_THRESHOLD` | `0.03` | Min edge to trigger alert |
| `MAX_POSITION_PER_CONTRACT` | `100` | Max contracts per market |
| `MAX_PORTFOLIO_EXPOSURE` | `1000` | Max total dollar exposure |
| `DRAWDOWN_LIMIT` | `0.10` | Drawdown fraction triggering position reduction |

---

## Known Issues

- **WebSocket cold start**: when a live game is first detected, both books start empty. Signal checks are gated on both books having at least one update. The first delta on one platform before the other has data is silently skipped until both books are populated.
- **Kalshi WS auth format**: WS auth uses RSA-PSS signed params in the subscribe message. Field names (`auth.key_id`, `auth.timestamp`, `auth.signature`) should be verified against the live WS spec. If auth is rejected, the scanner retries with exponential backoff.
