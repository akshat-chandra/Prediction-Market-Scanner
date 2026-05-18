"""
Order Book Analysis Module
Analyzes market microstructure: spreads, depth, liquidity, and imbalances.
"""
import pandas as pd
import numpy as np


class OrderBookAnalyzer:
    """Analyze prediction market order books for trading signals."""

    def __init__(self):
        self.snapshots = []

    def analyze(self, orderbook, ticker=""):
        """
        Analyze an order book snapshot and return microstructure metrics.
        Kalshi returns yes_bids and no_bids (no asks -- in binary markets,
        a yes bid at X is equivalent to a no ask at 100-X).
        """
        yes_bids = orderbook.get("yes", [])
        no_bids = orderbook.get("no", [])

        # Convert no_bids to implied yes_asks
        # A no bid at price P implies willingness to sell yes at (100 - P)
        yes_asks = [{"price": 100 - nb["price"], "quantity": nb["quantity"]}
                    for nb in no_bids]

        best_bid = max([b["price"] for b in yes_bids]) if yes_bids else 0
        best_ask = min([a["price"] for a in yes_asks]) if yes_asks else 100

        spread = best_ask - best_bid
        midpoint = (best_bid + best_ask) / 2

        # Depth: total quantity within 5 cents of best bid/ask
        bid_depth = sum(b["quantity"] for b in yes_bids
                       if b["price"] >= best_bid - 5)
        ask_depth = sum(a["quantity"] for a in yes_asks
                       if a["price"] <= best_ask + 5)

        # Order imbalance: positive = more buy pressure, negative = more sell
        total_bid_qty = sum(b["quantity"] for b in yes_bids)
        total_ask_qty = sum(a["quantity"] for a in yes_asks)
        imbalance = (total_bid_qty - total_ask_qty) / max(total_bid_qty + total_ask_qty, 1)

        metrics = {
            "ticker": ticker,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "midpoint": midpoint / 100,  # Convert to probability
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "imbalance": round(imbalance, 4),
            "total_bid_qty": total_bid_qty,
            "total_ask_qty": total_ask_qty,
            "timestamp": pd.Timestamp.now(),
        }

        self.snapshots.append(metrics)
        return metrics

    def get_spread_history(self):
        """Return spread history as DataFrame for analysis."""
        return pd.DataFrame(self.snapshots)

    def detect_liquidity_gap(self, orderbook, gap_threshold=10):
        """
        Detect gaps in the order book where no orders exist.
        Large gaps can indicate thin markets vulnerable to price jumps.

        Checks both the bid side (yes orders) and ask side (no orders converted to
        implied yes asks), since a gap on either side affects execution quality.
        """
        # Bid side: yes orders descending by price
        yes_bids = sorted(orderbook.get("yes", []),
                         key=lambda x: x["price"], reverse=True)

        # Ask side: no orders converted to implied yes asks, ascending by price
        no_bids = orderbook.get("no", [])
        yes_asks = sorted(
            [{"price": 100 - nb["price"], "quantity": nb["quantity"]} for nb in no_bids],
            key=lambda x: x["price"]
        )

        gaps = []

        for i in range(len(yes_bids) - 1):
            gap = yes_bids[i]["price"] - yes_bids[i + 1]["price"]
            if gap >= gap_threshold:
                gaps.append({
                    "side": "bid",
                    "upper_price": yes_bids[i]["price"],
                    "lower_price": yes_bids[i + 1]["price"],
                    "gap_size": gap,
                })

        for i in range(len(yes_asks) - 1):
            gap = yes_asks[i + 1]["price"] - yes_asks[i]["price"]
            if gap >= gap_threshold:
                gaps.append({
                    "side": "ask",
                    "upper_price": yes_asks[i + 1]["price"],
                    "lower_price": yes_asks[i]["price"],
                    "gap_size": gap,
                })

        return gaps

    def estimate_slippage(self, orderbook, side, quantity):
        """
        Estimate price slippage for a given order size.
        How much worse than midpoint would you fill at?
        """
        if side == "buy":
            # Walking up the ask side
            no_bids = sorted(orderbook.get("no", []),
                           key=lambda x: x["price"], reverse=True)
            asks = [{"price": 100 - nb["price"], "quantity": nb["quantity"]}
                    for nb in no_bids]
            asks.sort(key=lambda x: x["price"])
        else:
            asks = sorted(orderbook.get("yes", []),
                         key=lambda x: x["price"], reverse=True)

        if not asks:
            return None

        filled = 0
        total_cost = 0

        for level in asks:
            fill_qty = min(level["quantity"], quantity - filled)
            total_cost += fill_qty * level["price"]
            filled += fill_qty
            if filled >= quantity:
                break

        if filled == 0:
            return None

        avg_price = total_cost / filled
        best_price = asks[0]["price"]
        slippage = abs(avg_price - best_price)

        return {
            "quantity_requested": quantity,
            "quantity_filled": filled,
            "best_price": best_price,
            "avg_fill_price": round(avg_price, 2),
            "slippage_cents": round(slippage, 2),
            "fully_filled": filled >= quantity,
        }


def print_orderbook_summary(metrics):
    """Pretty print order book metrics."""
    print(f"\n{'='*50}")
    print(f"Order Book: {metrics['ticker']}")
    print(f"{'='*50}")
    print(f"  Best Bid:    {metrics['best_bid']}c")
    print(f"  Best Ask:    {metrics['best_ask']}c")
    print(f"  Spread:      {metrics['spread']}c")
    print(f"  Midpoint:    {metrics['midpoint']:.1%}")
    print(f"  Bid Depth:   {metrics['bid_depth']} contracts")
    print(f"  Ask Depth:   {metrics['ask_depth']} contracts")
    print(f"  Imbalance:   {metrics['imbalance']:+.2%}")
    print(f"  Signal:      {'BUY pressure' if metrics['imbalance'] > 0.1 else 'SELL pressure' if metrics['imbalance'] < -0.1 else 'Balanced'}")
