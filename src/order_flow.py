"""
Order Flow Analysis — Model-Free Microstructure Signals

Three types of signal, none requiring a price model:

1. REAL ARB — actual bid/ask cross between Kalshi and Polymarket.
   Uses tradeable prices, not mids. If combined cost < $1.00, profit is locked.

2. BOOK IMBALANCE — heavy bid side = someone accumulating.
   Heavy ask side = someone distributing. Tells you where informed money sits.

3. LARGE ORDER / QUOTE WITHDRAWAL — a big resting order signals where
   a player thinks fair value is. When it disappears suddenly, they got
   filled or changed their view — both are information.
"""
import time
import pandas as pd
import numpy as np
from datetime import datetime


# ── Real arbitrage (no model required) ────────────────────────────────────

def check_real_arb(kalshi_book, poly_book, min_profit_cents=1.0):
    """
    Check for a TRADEABLE arb between Kalshi and Polymarket.

    Uses actual bid/ask prices — not mids. A real arb exists when you can
    simultaneously buy YES cheaper on one platform and the combined cost of
    YES + NO across both platforms is less than $1.00 (the payout).

    kalshi_book: dict with yes_bid, yes_ask, no_bid, no_ask (in cents 1-99)
    poly_book:   dict with yes_bid, yes_ask (in cents 1-99, derived from CLOB)

    Returns arb dict if found, None otherwise.
    """
    if not kalshi_book or not poly_book:
        return None

    k_yes_ask = kalshi_book.get("yes_ask")   # cheapest YES on Kalshi
    k_no_ask  = kalshi_book.get("no_ask")    # cheapest NO on Kalshi
    p_yes_bid = poly_book.get("yes_bid")     # best YES buyer on Poly
    p_no_bid  = poly_book.get("no_bid")      # best NO buyer on Poly
    p_yes_ask = poly_book.get("yes_ask")     # cheapest YES on Poly
    p_no_ask  = poly_book.get("no_ask")      # cheapest NO on Poly

    opportunities = []

    # Leg 1: Buy YES on Kalshi, sell YES (buy NO) on Polymarket
    # Cost = k_yes_ask + (100 - p_yes_bid). If < 100 → locked profit.
    if k_yes_ask and p_yes_bid:
        cost = k_yes_ask + (100 - p_yes_bid)
        profit = 100 - cost
        if profit >= min_profit_cents:
            opportunities.append({
                "type":       "PURE_ARB",
                "leg_1":      f"BUY YES Kalshi @ {k_yes_ask}¢",
                "leg_2":      f"BUY NO  Poly   @ {100-p_yes_bid}¢",
                "total_cost": cost,
                "profit":     round(profit, 2),
                "direction":  "YES_K / NO_P",
            })

    # Leg 2: Buy YES on Polymarket, sell YES (buy NO) on Kalshi
    if p_yes_ask and k_no_ask:
        cost = p_yes_ask + k_no_ask
        profit = 100 - cost
        if profit >= min_profit_cents:
            opportunities.append({
                "type":       "PURE_ARB",
                "leg_1":      f"BUY YES Poly   @ {p_yes_ask}¢",
                "leg_2":      f"BUY NO  Kalshi @ {k_no_ask}¢",
                "total_cost": cost,
                "profit":     round(profit, 2),
                "direction":  "YES_P / NO_K",
            })

    return opportunities if opportunities else None


def check_stale_quote(kalshi_price, poly_price, staleness_threshold=5):
    """
    Detect cross-platform staleness — one venue hasn't repriced while the other moved.

    If the gap between mid prices exceeds the combined spread width, one of the
    venues is stale (hasn't updated). This is a model-free directional signal:
    the liquid venue is ground truth, the stale one will catch up.

    Returns dict with direction and magnitude if gap is meaningful.
    """
    if not kalshi_price or not poly_price:
        return None

    gap = kalshi_price - poly_price  # positive = Kalshi priced higher

    if abs(gap) >= staleness_threshold:
        return {
            "gap_cents":    round(abs(gap), 1),
            "direction":    "Kalshi stale HIGH" if gap > 0 else "Kalshi stale LOW",
            "action":       "BUY YES Poly / fade Kalshi" if gap > 0 else "BUY YES Kalshi / fade Poly",
            "kalshi_mid":   kalshi_price,
            "poly_mid":     poly_price,
        }
    return None


# ── Order book microstructure signals ─────────────────────────────────────

def book_imbalance(bids, asks):
    """
    Bid/ask imbalance at the top N levels of the book.

    Range: -1.0 (pure sell pressure) → +1.0 (pure buy pressure)

    Interpretation:
      > +0.3  : significant buy pressure — someone accumulating
      < -0.3  : significant sell pressure — someone distributing
      near 0  : balanced, no directional signal

    bids: list of [price, size] sorted best-first (highest price first)
    asks: list of [price, size] sorted best-first (lowest price first)
    """
    bid_vol = sum(s for _, s in bids) if bids else 0
    ask_vol = sum(s for _, s in asks) if asks else 0
    total   = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return round((bid_vol - ask_vol) / total, 4)


def vwap_mid(bids, asks, levels=5):
    """
    Volume-weighted average price midpoint using top N levels.

    More accurate than simple (best_bid + best_ask) / 2 because it weights
    prices by how much size sits at each level. A thick bid wall at 58¢
    pulls the VWAP mid above a simple mid.
    """
    top_bids = bids[:levels] if bids else []
    top_asks = asks[:levels] if asks else []

    if not top_bids or not top_asks:
        return None

    bid_vwap = (sum(p * s for p, s in top_bids) /
                sum(s for _, s in top_bids))
    ask_vwap = (sum(p * s for p, s in top_asks) /
                sum(s for _, s in top_asks))

    return round((bid_vwap + ask_vwap) / 2, 2)


def detect_large_orders(bids, asks, multiplier=5.0):
    """
    Flag unusually large individual resting orders.

    A resting order that is 5x+ the average order size at that book level
    is likely an informed or institutional participant signaling a price view.

    Returns list of flagged orders with side, price, and size.
    """
    flags = []
    for side, book in [("bid", bids), ("ask", asks)]:
        if not book:
            continue
        sizes = [s for _, s in book]
        if not sizes:
            continue
        avg = np.mean(sizes)
        for price, size in book:
            if size >= avg * multiplier and size > 100:  # ignore tiny books
                flags.append({
                    "side":       side,
                    "price":      price,
                    "size":       size,
                    "vs_avg":     round(size / avg, 1),
                    "signal":     f"Large {side.upper()} @ {price}¢ ({size} contracts, {size/avg:.1f}x avg)",
                })
    return flags


def quote_delta(snap_before, snap_after):
    """
    Compare two book snapshots and return what changed.

    Detects: bid/ask price moves, imbalance shift, large order appearance
    or withdrawal. Quote withdrawal (big order disappearing) is often the
    strongest signal — someone got filled or changed their view.
    """
    if not snap_before or not snap_after:
        return {}

    delta = {
        "bid_move":    round(snap_after["best_bid"] - snap_before["best_bid"], 1),
        "ask_move":    round(snap_after["best_ask"] - snap_before["best_ask"], 1),
        "spread_move": round(snap_after["spread"]   - snap_before["spread"],   1),
        "imb_move":    round(snap_after["imbalance"] - snap_before["imbalance"], 4),
        "mid_move":    round(snap_after["vwap_mid"] - snap_before["vwap_mid"], 2)
                       if snap_before.get("vwap_mid") and snap_after.get("vwap_mid")
                       else 0,
    }

    # Spread widening = market maker pulling back, often precedes a move
    if delta["spread_move"] >= 3:
        delta["alert"] = f"Spread widened +{delta['spread_move']}¢ — liquidity withdrawing"
    elif delta["imb_move"] >= 0.2:
        delta["alert"] = f"Imbalance surged +{delta['imb_move']:.2f} — buying pressure building"
    elif delta["imb_move"] <= -0.2:
        delta["alert"] = f"Imbalance dropped {delta['imb_move']:.2f} — selling pressure building"

    return delta


# ── Snapshot class for time-series tracking ───────────────────────────────

class BookSnapshot:
    """
    One point-in-time capture of an order book with computed microstructure metrics.
    Designed to be compared against the next snapshot to detect flow changes.
    """

    def __init__(self, game_title, bids, asks, platform="poly"):
        self.game      = game_title
        self.platform  = platform
        self.ts        = datetime.now()
        self.bids      = bids   # [[price, size], ...] sorted best-first
        self.asks      = asks

        self.best_bid  = bids[0][0] if bids else None
        self.best_ask  = asks[0][0] if asks else None
        self.spread    = round(self.best_ask - self.best_bid, 1) if (self.best_bid and self.best_ask) else None
        self.imbalance = book_imbalance(bids, asks)
        self.vwap_mid  = vwap_mid(bids, asks)
        self.large     = detect_large_orders(bids, asks)

    def summary(self):
        imb_str = (f"+{self.imbalance:.2f} (BUY)" if self.imbalance > 0.1
                   else f"{self.imbalance:.2f} (SELL)" if self.imbalance < -0.1
                   else f"{self.imbalance:.2f} (neutral)")
        return {
            "game":      self.game,
            "best_bid":  self.best_bid,
            "best_ask":  self.best_ask,
            "spread":    self.spread,
            "vwap_mid":  self.vwap_mid,
            "imbalance": imb_str,
            "large_orders": len(self.large),
            "timestamp": self.ts.strftime("%H:%M:%S"),
        }


# ── Convenience: parse Polymarket CLOB format ─────────────────────────────

def parse_poly_clob(clob_response):
    """
    Convert Polymarket CLOB API response into sorted [[price, size]] lists.
    Polymarket prices are in decimal (0.58 = 58¢) — we convert to cents.
    """
    bids = sorted(
        [[round(float(b["price"]) * 100, 1), float(b["size"])]
         for b in clob_response.get("bids", [])],
        key=lambda x: -x[0]   # highest price first
    )
    asks = sorted(
        [[round(float(a["price"]) * 100, 1), float(a["size"])]
         for a in clob_response.get("asks", [])],
        key=lambda x: x[0]    # lowest price first
    )
    return bids, asks


def parse_kalshi_book(orderbook_fp):
    """
    Convert Kalshi orderbook_fp format into [[price, size]] lists.
    yes_dollars = bids on YES. no_dollars = bids on NO = implied asks on YES.
    Kalshi prices are already in cents (0-100).
    """
    yes_bids_raw = orderbook_fp.get("yes_dollars", [])
    no_bids_raw  = orderbook_fp.get("no_dollars",  [])

    bids = sorted(
        [[float(p[0]) * 100, float(p[1])] for p in yes_bids_raw],
        key=lambda x: -x[0]
    )
    # NO bids at price P = YES asks at price (1 - P)
    asks = sorted(
        [[round((1 - float(p[0])) * 100, 1), float(p[1])] for p in no_bids_raw],
        key=lambda x: x[0]
    )
    return bids, asks
