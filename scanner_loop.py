#!/usr/bin/env python3
"""
Kalshi × Polymarket — WebSocket + REST Hybrid Scanner

Architecture:
  Pre-game (upcoming):  REST poll every 3 minutes for fresh prices + signals
  Live (in-game):       WebSocket event-driven — Kalshi orderbook_delta channel
                        + Polymarket CLOB WS. Signal fires on every delta.

PAPER TRADING ONLY. No real orders are placed on either platform.
All signals are logged to scanner_log.csv with simulated P&L tracked at settlement.

Usage:
    python3 scanner_loop.py                  # default: 3c threshold, $500 bankroll
    python3 scanner_loop.py --threshold 2 --bankroll 1000
"""

import sys
import os
import asyncio
import json
import time
import csv
import argparse
import subprocess
import base64
import requests
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("'websockets' not installed. Run: pip install websockets")
    sys.exit(1)

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from cross_platform import find_matches, deduplicate_matches, compare_prices
from order_flow import (
    check_stale_quote, book_imbalance, detect_large_orders, vwap_mid
)
from config import KALSHI_WS_URL, POLY_WS_URL


# ── CSV schema ─────────────────────────────────────────────────────────────

LOG_FILE = "scanner_log.csv"
LOG_FIELDS = [
    "timestamp", "event", "signal_type",
    "kalshi_bid", "kalshi_ask", "poly_bid", "poly_ask",
    "imbalance", "edge_size", "action", "contracts", "entry_price",
    "prices_updated_at", "resolution", "pnl", "closing_line_at_entry",
]


def write_signal(row: dict):
    """Append a signal row to the CSV. Fills missing columns with empty string."""
    write_header = not os.path.exists(LOG_FILE)
    full_row = {f: row.get(f, "") for f in LOG_FIELDS}
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(full_row)


def update_resolution(event: str, signal_type: str, entry_ts: str,
                      resolution: str, pnl: float, closing_line: float):
    """
    Write resolution + P&L back to the original signal row in the CSV.
    Matches on (event, signal_type, timestamp). Only updates unresolved rows.
    """
    if not os.path.exists(LOG_FILE):
        return
    rows = []
    updated = False
    with open(LOG_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if (r.get("event") == event
                    and r.get("signal_type") == signal_type
                    and r.get("timestamp") == entry_ts
                    and not r.get("resolution")):
                r["resolution"] = resolution
                r["pnl"] = f"{pnl:.4f}"
                r["closing_line_at_entry"] = f"{closing_line:.4f}"
                updated = True
            rows.append(r)
    if updated:
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
            writer.writerows(rows)


# ── Notification ───────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """Mac desktop notification. Best-effort; silently drops on failure."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


# ── Position sizing (paper) ────────────────────────────────────────────────

def kelly_size(edge: float, price: float, bankroll: float,
               fraction: float = 0.25) -> float:
    """
    Quarter-Kelly bet size in dollars, capped at 10% of bankroll.
    edge: probability edge (0-1), price: entry probability (0-1).
    """
    if price <= 0 or price >= 1 or edge <= 0:
        return 0
    kelly_full = edge / (1 - price)
    return round(min(bankroll * kelly_full * fraction, bankroll * 0.10), 2)


# ── Exponential backoff for REST 429 ──────────────────────────────────────

def with_backoff(fn, max_retries: int = 6, base_delay: float = 1.0):
    """
    Call fn(). On HTTP 429 (rate limit), retry with exponential backoff.
    Delays: 1s, 2s, 4s, 8s, 16s, 32s. Raises after max_retries.
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                delay = base_delay * (2 ** attempt)
                print(f"  Rate limited (429). Retry in {delay:.0f}s...")
                time.sleep(delay)
            else:
                raise
    raise RuntimeError(f"Still rate-limited after {max_retries} retries")


# ── Local order book (delta-based) ────────────────────────────────────────

class LocalBook:
    """
    Kalshi order book maintained from WebSocket deltas.

    Kalshi WS format: yes = YES bids [[price_frac, size]], no = NO bids [[price_frac, size]].
    YES bid at P → bids[P*100].
    NO bid at P  → implied YES ask at (1-P)*100.
    Size = 0 means remove that level.
    """

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.bids: dict[float, float] = {}   # price_cents → size
        self.asks: dict[float, float] = {}
        self.seq: int = -1
        self.updated_at: datetime | None = None

    def apply_snapshot(self, yes_levels: list, no_levels: list):
        self.bids.clear()
        self.asks.clear()
        for item in yes_levels:
            p, s = float(item[0]) * 100, float(item[1])
            if s > 0:
                self.bids[round(p, 1)] = s
        for item in no_levels:
            p, s = round((1.0 - float(item[0])) * 100, 1), float(item[1])
            if s > 0:
                self.asks[p] = s
        self.updated_at = datetime.now(timezone.utc)

    def apply_delta(self, yes_deltas: list, no_deltas: list, seq: int = -1):
        if seq > 0:
            self.seq = seq
        for item in yes_deltas:
            p, s = round(float(item[0]) * 100, 1), float(item[1])
            if s == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = s
        for item in no_deltas:
            p, s = round((1.0 - float(item[0])) * 100, 1), float(item[1])
            if s == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = s
        self.updated_at = datetime.now(timezone.utc)

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is not None and a is not None:
            return (b + a) / 2
        return b or a

    def as_lists(self) -> tuple[list, list]:
        bids = sorted([[p, s] for p, s in self.bids.items()], key=lambda x: -x[0])
        asks = sorted([[p, s] for p, s in self.asks.items()], key=lambda x:  x[0])
        return bids, asks


class PolyLocalBook:
    """
    Polymarket CLOB order book maintained from WebSocket events.
    Prices stored in cents (converted from Poly's decimal 0.0–1.0 format).
    """

    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.updated_at: datetime | None = None

    def apply_snapshot(self, bids: list, asks: list):
        self.bids = {
            round(float(b["price"]) * 100, 1): float(b["size"])
            for b in bids if float(b.get("size", 0)) > 0
        }
        self.asks = {
            round(float(a["price"]) * 100, 1): float(a["size"])
            for a in asks if float(a.get("size", 0)) > 0
        }
        self.updated_at = datetime.now(timezone.utc)

    def apply_changes(self, changes: list):
        """
        Apply Polymarket price_change events.
        side=BUY → bid side, side=SELL → ask side. size=0 → remove level.
        """
        for c in changes:
            p = round(float(c["price"]) * 100, 1)
            size = float(c.get("size", 0))
            side = c.get("side", "").upper()
            book = self.bids if side == "BUY" else self.asks
            if size == 0:
                book.pop(p, None)
            else:
                book[p] = size
        self.updated_at = datetime.now(timezone.utc)

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is not None and a is not None:
            return (b + a) / 2
        return b or a

    def as_lists(self) -> tuple[list, list]:
        bids = sorted([[p, s] for p, s in self.bids.items()], key=lambda x: -x[0])
        asks = sorted([[p, s] for p, s in self.asks.items()], key=lambda x:  x[0])
        return bids, asks


# ── Paper trade tracker ────────────────────────────────────────────────────

class PaperTradeTracker:
    """
    PAPER TRADING ONLY — logs what would have been traded and tracks simulated P&L.

    No real orders are submitted to Kalshi or Polymarket.
    Each signal is written to scanner_log.csv as an entry with entry_price.
    When a market resolves, resolution + P&L are written back to the original row.
    """

    # {(event, signal_type, timestamp) → {entry_price, contracts, action}}
    _open: dict[tuple, dict] = {}

    @classmethod
    def record(cls, row: dict, tag: str = ""):
        """Log a paper trade signal. Called by SignalChecker on every signal fire."""
        key = (row.get("event", ""), row.get("signal_type", ""), row.get("timestamp", ""))
        ep = row.get("entry_price", "")
        if ep:
            try:
                cls._open[key] = {
                    "entry_price": float(ep),
                    "contracts":   float(row.get("contracts", 0) or 0),
                    "action":      row.get("action", ""),
                }
            except ValueError:
                pass
        write_signal(row)
        tag_str = f" {tag}" if tag else ""
        print(f"  [PAPER LOG]{tag_str} {row.get('signal_type')} "
              f"| {row.get('event','')[:40]} | {row.get('action','')[:60]}")

    @classmethod
    def resolve(cls, event: str, yes_settles: bool, closing_mid_cents: float):
        """
        Call when a market settles. Computes simulated P&L for all open paper
        trades on that event and writes resolution back to the CSV.
        """
        to_close = [(k, v) for k, v in list(cls._open.items()) if k[0] == event]
        for key, trade in to_close:
            ep = trade["entry_price"]       # cents (e.g. 55.0)
            contracts = trade["contracts"]  # number of contracts
            action = trade["action"].upper()

            # P&L per contract: bought at ep cents, settles at 100¢ or 0¢
            if "YES" in action:
                settle_price = 100.0 if yes_settles else 0.0
                pnl = (settle_price - ep) * contracts / 100
            else:
                settle_price = 0.0 if yes_settles else 100.0
                pnl = (settle_price - ep) * contracts / 100

            resolution = "YES" if yes_settles else "NO"
            update_resolution(
                event=key[0], signal_type=key[1], entry_ts=key[2],
                resolution=resolution, pnl=pnl,
                closing_line=closing_mid_cents / 100,
            )
            del cls._open[key]
            print(f"  [RESOLVED] {event} → {resolution} | Paper P&L: ${pnl:+.2f}")


# ── Microstructure signal checker ──────────────────────────────────────────

class SignalChecker:
    """
    Runs all microstructure checks on a matched Kalshi + Polymarket book pair.
    Called on every WebSocket delta (live) or REST poll (pre-game).
    Fires signals through PaperTradeTracker — no real order execution.

    Spam filters applied here (all state is per-instance):
      1. Near-resolution filter: drop any check when best_bid < 3¢ or best_ask > 97¢.
      2. Large-order dedup: once per (market, price_level) per 5 minutes.
      3. Pre-game large-order suppression: LARGE_ORDER only fires for live markets.
      4. Imbalance cooldown: hard 2-minute floor, no VWAP-move bypass.
      5. [PRE-GAME] / [LIVE] tag on every log line and notification.
      6. Mac notifications: suppressed for LARGE_ORDER; only REAL_ARB and STALE_QUOTE.
    """

    LARGE_ORDER_COOLDOWN = 300   # seconds — one alert per price level per 5 min
    IMBALANCE_COOLDOWN   = 300   # seconds — must exceed REST poll interval (180s)
    SIGNAL_COOLDOWN      = 300   # seconds — dedup for REAL_ARB and STALE_QUOTE

    def __init__(self, threshold_cents: float, bankroll: float):
        self.threshold = threshold_cents
        self.bankroll  = bankroll
        # (event, price_level) → last fired timestamp
        self._large_order_seen: dict[tuple, float] = {}
        # event → last_fired_ts  (time-only cooldown, no VWAP condition)
        self._imbalance_last: dict[str, float] = {}
        # (event, signal_type) → last fired timestamp
        self._signal_last: dict[tuple, float] = {}

    def check(self, event_name: str,
              k_book: LocalBook, p_book: PolyLocalBook,
              is_live: bool = True):
        """
        Entry point. is_live=True when the game has started (WS path);
        is_live=False for pre-game REST polls.
        """
        k_bid = k_book.best_bid()
        k_ask = k_book.best_ask()
        p_bid = p_book.best_bid()
        p_ask = p_book.best_ask()

        if not any([k_bid, k_ask, p_bid, p_ask]):
            return

        # ── Filter 1: near-resolution ──────────────────────────────────────
        # Any visible price below 3¢ or above 97¢ means the market is
        # effectively decided. This catches:
        #   - near-NO: bids at 1-2¢ (YES not worth buying)
        #   - near-YES: asks at 98-99¢ (YES already priced in)
        #   - one-sided books: only asks at 1-2¢, no bids (previous filter
        #     missed this because it only checked min(bids), which was empty)
        all_prices = [p for p in [k_bid, k_ask, p_bid, p_ask] if p is not None]
        if min(all_prices) < 10 or max(all_prices) > 90:
            return

        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        k_upd = k_book.updated_at
        p_upd = p_book.updated_at
        prices_ts = max(
            k_upd if k_upd else datetime.min.replace(tzinfo=timezone.utc),
            p_upd if p_upd else datetime.min.replace(tzinfo=timezone.utc),
        ).strftime("%H:%M:%S.%f")[:-3]

        p_bids, p_asks = p_book.as_lists()
        imb  = book_imbalance(p_bids, p_asks)
        tag  = "[LIVE]" if is_live else "[PRE-GAME]"

        base = {
            "timestamp":         now_ts,
            "event":             event_name,
            "kalshi_bid":        f"{k_bid:.1f}" if k_bid is not None else "",
            "kalshi_ask":        f"{k_ask:.1f}" if k_ask is not None else "",
            "poly_bid":          f"{p_bid:.1f}" if p_bid is not None else "",
            "poly_ask":          f"{p_ask:.1f}" if p_ask is not None else "",
            "imbalance":         f"{imb:.4f}",
            "prices_updated_at": prices_ts,
        }

        self._check_real_arb(event_name, k_bid, k_ask, p_bid, p_ask, base, tag)
        self._check_stale_quote(event_name, k_book.mid(), p_book.mid(), base, tag)
        self._check_imbalance(event_name, imb, p_bids, p_asks, base, tag)
        if is_live:
            # ── Filter 3: suppress LARGE_ORDER for pre-game ────────────────
            # Market-maker quotes at extreme prices are normal before tip-off.
            # Only meaningful once two-sided live trading is underway.
            self._check_large_orders(event_name, p_bids, p_asks, base, tag)

    def _check_real_arb(self, event, k_bid, k_ask, p_bid, p_ask, base, tag):
        """
        Real arb: tradeable bid/ask cross.
        Leg 1: BUY YES Kalshi @ k_ask + BUY NO Poly @ (100-p_bid) < 100¢ → locked profit.
        Leg 2: BUY YES Poly @ p_ask + BUY NO Kalshi @ (100-k_bid) < 100¢ → locked profit.
        """
        if k_ask is not None and p_bid is not None:
            cost = k_ask + (100 - p_bid)
            profit = 100 - cost
            if profit >= self.threshold:
                contracts = round((self.bankroll * 0.15) / (k_ask / 100), 0)
                self._fire("REAL_ARB", event, base, tag, notify_user=True, extra={
                    "edge_size":   f"{profit:.2f}",
                    "action":      f"BUY YES Kalshi@{k_ask:.0f}¢ + BUY NO Poly@{100-p_bid:.0f}¢",
                    "contracts":   str(int(contracts)),
                    "entry_price": f"{k_ask:.1f}",
                })

        if p_ask is not None and k_bid is not None:
            cost = p_ask + (100 - k_bid)
            profit = 100 - cost
            if profit >= self.threshold:
                contracts = round((self.bankroll * 0.15) / (p_ask / 100), 0)
                self._fire("REAL_ARB", event, base, tag, notify_user=True, extra={
                    "edge_size":   f"{profit:.2f}",
                    "action":      f"BUY YES Poly@{p_ask:.1f}¢ + BUY NO Kalshi@{100-k_bid:.0f}¢",
                    "contracts":   str(int(contracts)),
                    "entry_price": f"{p_ask:.1f}",
                })

    def _check_stale_quote(self, event, k_mid, p_mid, base, tag):
        """Stale quote: mid divergence exceeds threshold. Fade the lagging venue."""
        if k_mid is None or p_mid is None:
            return
        gap = abs(k_mid - p_mid)
        if gap >= self.threshold:
            cheaper = "Kalshi" if k_mid < p_mid else "Polymarket"
            entry = min(k_mid, p_mid)
            edge  = gap / 100
            price = entry / 100
            size  = kelly_size(edge, price, self.bankroll)
            self._fire("STALE_QUOTE", event, base, tag, notify_user=True, extra={
                "edge_size":   f"{gap:.1f}",
                "action":      f"BUY YES on {cheaper} (gap={gap:.1f}¢)",
                "contracts":   str(int(size * 100 / entry)) if entry > 0 else "",
                "entry_price": f"{entry:.1f}",
            })

    def _check_imbalance(self, event, imb, p_bids, p_asks, base, tag):
        """
        Book imbalance ≥ 0.35 = strong directional pressure.

        Cooldown: hard 2-minute floor between alerts for the same market.
        No VWAP-move bypass — that condition re-triggered on every delta
        in volatile markets (84→80→83→88¢ all firing within seconds).
        """
        if abs(imb) < 0.35:
            return

        now = time.monotonic()
        if now - self._imbalance_last.get(event, 0.0) < self.IMBALANCE_COOLDOWN:
            return

        self._imbalance_last[event] = now
        vmid = vwap_mid(p_bids, p_asks)
        side = "BUY" if imb > 0 else "SELL"
        self._fire("IMBALANCE", event, base, tag, notify_user=False, extra={
            "edge_size":   f"{abs(imb):.4f}",
            "action":      f"{side} pressure | VWAP mid {vmid}¢",
            "entry_price": f"{vmid:.1f}" if vmid else "",
        })

    def _check_large_orders(self, event, p_bids, p_asks, base, tag):
        """
        Large resting orders (5× avg). Only called for live markets (pre-game suppressed).

        Dedup: fires at most once per (event, price_level) per 5 minutes.
        No Mac notification — informational only, not directly actionable.

        Price-level filter: skip orders at ≤3¢ or ≥97¢ regardless of best_bid/ask.
        The near-resolution filter checks the TOP of book, but a large resting order
        can sit deep in the book at an extreme price while best_ask is still mid-range.
        """
        now = time.monotonic()
        for lg in detect_large_orders(p_bids, p_asks):
            level = round(lg["price"])   # bucket to nearest cent for dedup key

            # Skip orders at near-resolution price levels — these are market-maker
            # limit orders placed at extreme prices, not genuine informed signals.
            if level <= 10 or level >= 90:
                continue

            key = (event, level)
            if now - self._large_order_seen.get(key, 0.0) < self.LARGE_ORDER_COOLDOWN:
                continue
            self._large_order_seen[key] = now
            self._fire("LARGE_ORDER", event, base, tag, notify_user=False, extra={
                "edge_size":   f"{lg['size']:.0f}",
                "action":      lg["signal"],
                "entry_price": f"{lg['price']:.1f}",
            })

    def _fire(self, signal_type: str, event: str, base: dict,
              tag: str, notify_user: bool, extra: dict):
        """Log the signal and optionally send a Mac notification."""
        if signal_type in ("REAL_ARB", "STALE_QUOTE"):
            now = time.monotonic()
            key = (event, signal_type)
            if now - self._signal_last.get(key, 0.0) < self.SIGNAL_COOLDOWN:
                return
            self._signal_last[key] = now

        row = {**base, "signal_type": signal_type, **extra}
        PaperTradeTracker.record(row, tag=tag)
        if notify_user:
            notify(f"{signal_type} — {event[:30]}",
                   extra.get("action", "")[:80])


# ── Kalshi WebSocket client ────────────────────────────────────────────────

class KalshiWSClient:
    """
    Async WebSocket client for Kalshi's orderbook_delta channel.

    Auth: sends RSA-PSS signed auth params in the initial subscribe message.
    Applies snapshots and deltas to LocalBook instances.
    Calls on_update(ticker, book) on every change for the signal checker.
    Reconnects automatically with exponential backoff.
    """

    def __init__(self, kalshi_client: KalshiClient,
                 on_update,  # callable(ticker: str, book: LocalBook)
                 ):
        self.kc = kalshi_client
        self.on_update = on_update
        self.books: dict[str, LocalBook] = {}
        self.subscribed: set[str] = set()
        self._running = False
        self._send_q: asyncio.Queue | None = None

    def subscribe_tickers(self, tickers: list[str]):
        """Queue additional tickers for subscription. Safe to call at any time."""
        new = [t for t in tickers if t not in self.subscribed]
        if not new:
            return
        for t in new:
            if t not in self.books:
                self.books[t] = LocalBook(t)
            self.subscribed.add(t)
        if self._running and self._send_q is not None:
            msg = json.dumps({
                "id": int(time.time() * 1000),
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": new,
                }
            })
            self._send_q.put_nowait(msg)

    async def run(self):
        """
        Main WS loop. Auth headers are generated fresh on every (re)connect
        via KalshiClient.ws_auth_headers() — same RSA-PSS signing as REST.

        If no private key is loaded, logs an error and exits immediately.
        """
        if not self.kc.private_key:
            print("  [Kalshi WS] No private key — cannot connect. "
                  "Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
            return

        self._running = True
        self._send_q = asyncio.Queue()
        backoff = 1

        while self._running:
            try:
                # Generate fresh headers each attempt (timestamp must be current).
                # debug=True prints the exact signed message and header values.
                auth_headers = self.kc.ws_auth_headers(debug=True)
                async with websockets.connect(
                    KALSHI_WS_URL,
                    additional_headers=auth_headers,
                    ping_interval=30,
                    ping_timeout=15,
                    open_timeout=20,
                ) as ws:
                    backoff = 1
                    print("  [Kalshi WS] Connected")

                    # Subscribe to orderbook_delta for all known tickers.
                    # Auth is in the HTTP upgrade headers above — not in this message.
                    sub_params: dict = {
                        "channels": ["orderbook_delta"],
                        "market_tickers": list(self.subscribed),
                    }
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": sub_params}))

                    async def _sender():
                        while True:
                            msg = await self._send_q.get()
                            await ws.send(msg)

                    sender = asyncio.create_task(_sender())
                    try:
                        async for raw in ws:
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                continue

                            msg_type = data.get("type", "")
                            msg = data.get("msg", {})
                            ticker = msg.get("market_ticker", "")
                            if not ticker or ticker not in self.books:
                                continue

                            if msg_type == "orderbook_snapshot":
                                self.books[ticker].apply_snapshot(
                                    msg.get("yes", []), msg.get("no", []))
                            elif msg_type == "orderbook_delta":
                                self.books[ticker].apply_delta(
                                    msg.get("yes", []), msg.get("no", []),
                                    seq=msg.get("seq", -1))
                            else:
                                continue

                            self.on_update(ticker, self.books[ticker])
                    finally:
                        sender.cancel()

            except (websockets.exceptions.ConnectionClosed,
                    ConnectionResetError, OSError, TimeoutError) as e:
                print(f"  [Kalshi WS] Disconnected ({e}). Retry in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 64)
            except Exception as e:
                print(f"  [Kalshi WS] Error: {e}. Retry in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 64)


# ── Polymarket WebSocket client ────────────────────────────────────────────

class PolyWSClient:
    """
    Async WebSocket client for Polymarket's CLOB subscription feed.

    Subscribe: {"assets_ids": [...], "type": "Market"}
    Receives:  "book" events (full snapshot) + "price_change" events (delta).
    Calls on_update(token_id, book) on every change.
    Reconnects automatically with exponential backoff.
    """

    def __init__(self, on_update):  # callable(token_id: str, book: PolyLocalBook)
        self.on_update = on_update
        self.books: dict[str, PolyLocalBook] = {}
        self.subscribed: set[str] = set()
        self._running = False
        self._send_q: asyncio.Queue | None = None

    def subscribe_tokens(self, token_ids: list[str]):
        """Queue additional token IDs for subscription."""
        new = [t for t in token_ids if t not in self.subscribed]
        if not new:
            return
        for t in new:
            if t not in self.books:
                self.books[t] = PolyLocalBook(t)
            self.subscribed.add(t)
        if self._running and self._send_q is not None:
            self._send_q.put_nowait(
                json.dumps({"assets_ids": new, "type": "Market"})
            )

    async def run(self):
        self._running = True
        self._send_q = asyncio.Queue()
        backoff = 1

        while self._running:
            try:
                async with websockets.connect(
                    POLY_WS_URL,
                    ping_interval=30,
                    ping_timeout=15,
                    open_timeout=20,
                ) as ws:
                    backoff = 1
                    print("  [Poly WS] Connected")

                    if self.subscribed:
                        await ws.send(json.dumps({
                            "assets_ids": list(self.subscribed),
                            "type": "Market",
                        }))

                    async def _sender():
                        while True:
                            msg = await self._send_q.get()
                            await ws.send(msg)

                    sender = asyncio.create_task(_sender())
                    try:
                        async for raw in ws:
                            try:
                                events = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(events, dict):
                                events = [events]

                            for ev in events:
                                etype = ev.get("event_type", "")
                                asset = ev.get("asset_id", "")
                                if asset not in self.books:
                                    continue

                                if etype == "book":
                                    self.books[asset].apply_snapshot(
                                        ev.get("bids", []), ev.get("asks", []))
                                elif etype == "price_change":
                                    self.books[asset].apply_changes(
                                        ev.get("changes", []))
                                else:
                                    continue

                                self.on_update(asset, self.books[asset])
                    finally:
                        sender.cancel()

            except (websockets.exceptions.ConnectionClosed,
                    ConnectionResetError, OSError, TimeoutError) as e:
                print(f"  [Poly WS] Disconnected ({e}). Retry in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 64)
            except Exception as e:
                print(f"  [Poly WS] Error: {e}. Retry in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 64)


# ── Game registry ──────────────────────────────────────────────────────────

class GameRegistry:
    """
    Tracks all known active games and their platform identifiers.
    Classifies games as 'upcoming' or 'live' based on game_start_time vs now.
    """

    def __init__(self):
        # event_name → {kalshi_ticker, poly_token_id, game_start_time, match_score}
        self.games: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def update(self, event_name: str, **kwargs):
        if event_name not in self.games:
            self.games[event_name] = {}
        self.games[event_name].update(kwargs)

    @staticmethod
    def _parse_gst(gst) -> datetime | None:
        if gst is None:
            return None
        if isinstance(gst, datetime):
            return gst if gst.tzinfo else gst.replace(tzinfo=timezone.utc)
        try:
            s = (str(gst).strip()
                 .replace(" ", "T")
                 .replace("+00:00:00", "+00:00")
                 .replace("+00", "+00:00"))
            if not any(c in s for c in ("+", "-", "Z")):
                s += "+00:00"
            return datetime.fromisoformat(s)
        except Exception:
            return None

    def live_games(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [
            {"name": n, **g}
            for n, g in self.games.items()
            if (t := self._parse_gst(g.get("game_start_time"))) is not None and t <= now
        ]

    def upcoming_games(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        result = []
        for n, g in self.games.items():
            t = self._parse_gst(g.get("game_start_time"))
            if t is not None and t > now:
                result.append({"name": n, **g})
        return result


# ── REST discovery ─────────────────────────────────────────────────────────

def _parse_iso(s: str | None):
    """Parse an ISO-8601 datetime string (with or without Z) to a UTC datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def discover_games(kalshi: KalshiClient, poly: PolymarketClient,
                   registry: GameRegistry) -> list[dict]:
    """
    Synchronous REST discovery. Fetches active games from both platforms,
    matches them, populates the GameRegistry.

    Returns a list of matched pairs with both platforms' identifiers and prices.
    Called every 3 minutes from the async discovery loop (via run_in_executor).

    Logs how many markets were filtered at each stage so you can verify
    closed/stale games are being excluded.
    """
    from datetime import timedelta
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # Filter counters — printed at end of discovery
    n_poly_raw = n_poly_status = n_poly_date = n_poly_ok = 0
    n_kalshi_raw = n_kalshi_date = n_kalshi_ok = 0

    # ── Polymarket ──
    poly_markets: list[dict] = []
    for sport in ["nba", "nhl", "baseball"]:
        try:
            games = with_backoff(
                lambda s=sport: poly.get_game_events(s, status="open")
            )
            for game in games:
                n_poly_raw += 1
                ml = game.get("moneyline")
                if not ml:
                    n_poly_status += 1
                    continue

                # 24h date filter: use end_date if available, else game_start_time + 24h.
                # Rejects games that ended more than a day ago regardless of API status field.
                end_dt  = _parse_iso(game.get("end_date"))
                start_dt = _parse_iso(game.get("game_start_time"))
                if end_dt and end_dt < cutoff:
                    n_poly_date += 1
                    continue
                if not end_dt and start_dt and (start_dt + timedelta(hours=24)) < now:
                    n_poly_date += 1
                    continue

                n_poly_ok += 1
                poly_markets.append({
                    "event_name":      game["title"],
                    "market_id":       ml.get("market_id") or ml.get("condition_id"),
                    "yes_token":       ml.get("yes_token"),
                    "no_token":        ml.get("no_token"),
                    "yes_price":       ml.get("yes_price") or ml.get("mid"),
                    "best_bid":        ml.get("best_bid"),
                    "best_ask":        ml.get("best_ask"),
                    "question":        ml.get("question") or game["title"],
                    "game_start_time": game.get("game_start_time"),
                    "volume":          float(ml.get("volume", 0) or 0),
                })
        except Exception as e:
            print(f"  Polymarket {sport} fetch error: {e}")

    # ── Kalshi ──
    # get_sports_game_markets() already filters status != "active" and
    # expected_expiration_time > 24h ago. We count here for the log.
    try:
        kalshi_raw_all = with_backoff(kalshi.get_sports_game_markets)
    except Exception as e:
        print(f"  Kalshi fetch error: {e}")
        kalshi_raw_all = []

    kalshi_raw  = kalshi_raw_all   # already filtered inside the client
    n_kalshi_ok = len(kalshi_raw)

    if not poly_markets or not kalshi_raw:
        print(f"  [filter] Poly: {n_poly_raw} raw → "
              f"{n_poly_status} no-moneyline, {n_poly_date} stale-date, "
              f"{n_poly_ok} accepted")
        print(f"  [filter] Kalshi: {n_kalshi_ok} accepted (status+date filtered in client)")
        return []

    kalshi_for_match = [{
        "ticker":          g["ticker"],
        "title":           g.get("event_title") or g.get("title", ""),
        "subtitle":        "",
        "yes_bid":         g.get("yes_bid"),
        "yes_ask":         g.get("yes_ask"),
        "mid_cents":       g.get("mid_cents"),
        "expiration_time": g.get("expiration_time"),
    } for g in kalshi_raw]

    poly_for_match = [{
        "market_id":    p["market_id"],
        "question":     p["question"],
        "yes_price":    p.get("yes_price"),
        "mid":          p.get("yes_price"),
        "condition_id": p["market_id"],
    } for p in poly_markets]

    matches = find_matches(kalshi_for_match, poly_for_match, min_score=0.25)
    k_by_ticker = {m["ticker"]: m for m in kalshi_for_match}
    p_by_id = {p["market_id"]: p for p in poly_markets}

    # deduplicate needs poly_by_id in the old format
    p_compat = {
        pid: {"market_id": pid, "condition_id": pid,
              "yes_price": p_by_id[pid].get("yes_price"),
              "mid":       p_by_id[pid].get("yes_price")}
        for pid in p_by_id
    }
    matches = deduplicate_matches(matches, k_by_ticker, p_compat)

    paired: list[dict] = []
    for match in matches:
        km = k_by_ticker.get(match["kalshi_ticker"])
        pm = p_by_id.get(match["poly_id"])
        if not km or not pm:
            continue

        # Date guard: reject pairs where Kalshi expiry and Polymarket game_start_time
        # differ by more than 2 days — same teams but different games.
        k_exp  = _parse_iso(km.get("expiration_time"))
        p_start = _parse_iso(pm.get("game_start_time"))
        if k_exp and p_start:
            from datetime import timedelta
            if abs((k_exp - p_start).total_seconds()) > 2 * 86400:
                continue

        name = pm.get("event_name") or match.get("poly_question", match["kalshi_ticker"])
        registry.update(
            name,
            kalshi_ticker=km["ticker"],
            poly_token_id=pm.get("yes_token"),
            game_start_time=pm.get("game_start_time"),
            match_score=match["match_score"],
        )
        paired.append({
            "name":           name,
            "kalshi_ticker":  km["ticker"],
            "poly_token_id":  pm.get("yes_token"),
            "kalshi_bid":     km.get("yes_bid"),
            "kalshi_ask":     km.get("yes_ask"),
            "poly_bid":       pm.get("best_bid"),
            "poly_ask":       pm.get("best_ask"),
            "game_start_time": pm.get("game_start_time"),
            "match_score":    match["match_score"],
        })

    print(f"  [filter] Poly:   {n_poly_raw} raw → "
          f"{n_poly_status} no-moneyline, {n_poly_date} stale (>24h), "
          f"{n_poly_ok} accepted")
    print(f"  [filter] Kalshi: {n_kalshi_ok} accepted (status+expiry filtered in client)")
    print(f"  [filter] Matched pairs after cross-platform join: {len(paired)}")

    return paired


def rest_scan_upcoming(pairs: list[dict], kalshi: KalshiClient,
                       poly: PolymarketClient, checker: SignalChecker):
    """
    REST-based scan for pre-game (upcoming) markets.
    Fetches fresh prices for each pair inside this call — prices always up-to-date.
    Runs signal checks just like the WS path does.
    """
    for pair in pairs:
        name = pair["name"]
        try:
            # Fresh Kalshi price via REST
            kp = with_backoff(
                lambda t=pair["kalshi_ticker"]: kalshi.get_market_price(t)
            )
            if not kp:
                continue

            k_book = LocalBook(pair["kalshi_ticker"])
            if kp.get("yes_bid"):
                k_book.bids[float(kp["yes_bid"])] = 100.0
            if kp.get("yes_ask"):
                k_book.asks[float(kp["yes_ask"])] = 100.0
            k_book.updated_at = datetime.now(timezone.utc)

            # Fresh Polymarket CLOB via REST
            token_id = pair.get("poly_token_id")
            if not token_id:
                continue
            clob = with_backoff(lambda t=token_id: poly.get_orderbook(t))
            if not clob or clob.get("error"):
                continue

            p_book = PolyLocalBook(token_id)
            p_book.apply_snapshot(clob.get("bids", []), clob.get("asks", []))

            checker.check(name, k_book, p_book, is_live=False)

        except Exception as e:
            print(f"  REST scan error [{name}]: {e}")


def check_settlements(registry: GameRegistry, kalshi: KalshiClient):
    """
    Poll Kalshi for settlement status of known live games.
    A market is settled when yes_bid > 95¢ (YES wins) or < 5¢ (NO wins).
    Calls PaperTradeTracker.resolve() to finalize simulated P&L.
    """
    for g in registry.live_games():
        ticker = g.get("kalshi_ticker")
        if not ticker:
            continue
        try:
            price = with_backoff(lambda t=ticker: kalshi.get_market_price(t))
            if not price:
                continue
            bid = price.get("yes_bid", 50)
            mid = price.get("mid_cents", 50)
            if bid is not None and bid > 95:
                PaperTradeTracker.resolve(g["name"], yes_settles=True,
                                          closing_mid_cents=float(mid or bid))
                del registry.games[g["name"]]
            elif bid is not None and bid < 5:
                PaperTradeTracker.resolve(g["name"], yes_settles=False,
                                          closing_mid_cents=float(mid or bid))
                del registry.games[g["name"]]
        except Exception:
            pass


# ── Main scanner (orchestrator) ────────────────────────────────────────────

class Scanner:
    """
    Orchestrates Kalshi WS + Polymarket WS + REST polling.

    - Live games → WS subscriptions (event-driven, signal fires on every delta)
    - Upcoming games → REST poll every 3 minutes for pre-game signals
    - Settlement check every 5 minutes
    """

    PRE_GAME_INTERVAL = 180   # seconds between REST polls for upcoming games
    SETTLEMENT_INTERVAL = 300  # seconds between settlement polls

    def __init__(self, kalshi: KalshiClient, poly: PolymarketClient,
                 threshold: float, bankroll: float):
        self.kalshi = kalshi
        self.poly = poly
        self.registry = GameRegistry()
        self.checker = SignalChecker(threshold_cents=threshold, bankroll=bankroll)

        self._kalshi_ws = KalshiWSClient(kalshi, self._on_kalshi_update)
        self._poly_ws = PolyWSClient(self._on_poly_update)

        # ticker ↔ event_name, token_id ↔ event_name
        self._ticker_to_event: dict[str, str] = {}
        self._token_to_event:  dict[str, str] = {}

    # ── WS callbacks ──

    def _on_kalshi_update(self, ticker: str, book: LocalBook):
        """Called on every Kalshi orderbook_delta. Triggers signal check."""
        name = self._ticker_to_event.get(ticker)
        if not name:
            return
        game = self.registry.games.get(name, {})
        token_id = game.get("poly_token_id")
        if not token_id:
            return
        p_book = self._poly_ws.books.get(token_id)
        if not p_book or p_book.updated_at is None:
            return   # Poly book not yet populated
        self.checker.check(name, book, p_book, is_live=True)

    def _on_poly_update(self, token_id: str, book: PolyLocalBook):
        """Called on every Polymarket price_change. Triggers signal check."""
        name = self._token_to_event.get(token_id)
        if not name:
            return
        game = self.registry.games.get(name, {})
        ticker = game.get("kalshi_ticker")
        if not ticker:
            return
        k_book = self._kalshi_ws.books.get(ticker)
        if not k_book or k_book.updated_at is None:
            return   # Kalshi book not yet populated
        self.checker.check(name, k_book, book, is_live=True)

    # ── Discovery loop ──

    async def _discovery_loop(self):
        """
        Runs every 3 minutes:
        1. REST-discover all active games on both platforms
        2. Subscribe live games to WS channels
        3. REST-scan upcoming games for pre-game signals
        4. Periodically check for settlements
        """
        loop = asyncio.get_running_loop()
        last_settlement_check = 0.0

        while True:
            now = time.time()
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ts}] Discovery scan...")

            try:
                pairs = await loop.run_in_executor(
                    None,
                    lambda: discover_games(self.kalshi, self.poly, self.registry)
                )
            except Exception as e:
                print(f"  Discovery error: {e}")
                pairs = []

            live = self.registry.live_games()
            upcoming = self.registry.upcoming_games()
            print(f"  Matched {len(pairs)} game pairs. "
                  f"Live: {len(live)}  Upcoming: {len(upcoming)}")

            # Subscribe WS for live games
            live_tickers = []
            live_tokens = []
            for g in live:
                t = g.get("kalshi_ticker")
                tok = g.get("poly_token_id")
                if t:
                    live_tickers.append(t)
                    self._ticker_to_event[t] = g["name"]
                if tok:
                    live_tokens.append(tok)
                    self._token_to_event[tok] = g["name"]

            if live_tickers:
                self._kalshi_ws.subscribe_tickers(live_tickers)
                print(f"  [WS] Subscribed {len(live_tickers)} Kalshi tickers")
            if live_tokens:
                self._poly_ws.subscribe_tokens(live_tokens)
                print(f"  [WS] Subscribed {len(live_tokens)} Polymarket tokens")

            # REST scan for upcoming (pre-game) games.
            # Exclude anything already on WS — the live book is authoritative.
            up_names = {g["name"] for g in upcoming}
            ws_names = set(self._ticker_to_event.values())
            upcoming_pairs = [
                p for p in pairs
                if p["name"] in up_names and p["name"] not in ws_names
            ]
            if upcoming_pairs:
                print(f"  REST pre-game scan: {len(upcoming_pairs)} upcoming games...")
                await loop.run_in_executor(
                    None,
                    lambda up=upcoming_pairs:
                        rest_scan_upcoming(up, self.kalshi, self.poly, self.checker)
                )

            # Periodic settlement check
            if now - last_settlement_check >= self.SETTLEMENT_INTERVAL:
                await loop.run_in_executor(
                    None,
                    lambda: check_settlements(self.registry, self.kalshi)
                )
                last_settlement_check = now

            await asyncio.sleep(self.PRE_GAME_INTERVAL)

    # ── Run ──

    async def run(self):
        print(f"\n{'═'*62}")
        print(f"  KALSHI × POLYMARKET — WS + REST HYBRID SCANNER")
        print(f"  Pre-game: REST every {self.PRE_GAME_INTERVAL}s  "
              f"|  Live: WebSocket event-driven")
        print(f"  PAPER TRADING ONLY — no real orders placed")
        print(f"  Log: {LOG_FILE}")
        print(f"  Ctrl+C to stop")
        print(f"{'═'*62}")

        await asyncio.gather(
            self._kalshi_ws.run(),
            self._poly_ws.run(),
            self._discovery_loop(),
        )


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Kalshi × Polymarket WS + REST hybrid scanner (paper trading)"
    )
    parser.add_argument("--threshold", type=float, default=3.0,
                        help="Min edge in cents to alert on (default: 3)")
    parser.add_argument("--bankroll",  type=float, default=500.0,
                        help="Paper bankroll for sizing (default: 500)")
    args = parser.parse_args()

    kalshi = KalshiClient()
    poly   = PolymarketClient()

    if not kalshi.private_key:
        print("\n  ⚠  No Kalshi credentials found.")
        print("     Kalshi WebSocket requires auth — WS client will not start.")
        print("     Pre-game REST polling will still run for price discovery.")
        print("     Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for live mode.\n")

    scanner = Scanner(
        kalshi=kalshi,
        poly=poly,
        threshold=args.threshold,
        bankroll=args.bankroll,
    )

    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        print(f"\n\n  Scanner stopped.")


if __name__ == "__main__":
    main()
