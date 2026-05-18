"""
Paper Trading Engine
Simulates order execution against real Kalshi order book data — no real account needed.

Why this matters: We pull live order books from Kalshi's public API (no auth required),
then simulate fills realistically based on what's actually available in the book.
This is more honest than most backtests because fill prices reflect real liquidity.

In an interview: "I built a paper trading layer that uses live Kalshi order books to
simulate realistic fills, so I could validate strategy logic without needing live capital."
"""
import uuid
from datetime import datetime
from collections import defaultdict


class PaperOrder:
    """Represents a single paper order with lifecycle tracking."""

    STATUSES = ("open", "filled", "partial", "cancelled")

    def __init__(self, ticker, side, order_type, quantity, price=None):
        self.order_id = str(uuid.uuid4())[:8]
        self.ticker = ticker
        self.side = side           # 'yes' or 'no'
        self.order_type = order_type  # 'limit' or 'market'
        self.quantity = quantity
        self.price = price         # limit price in cents (None for market orders)
        self.filled_qty = 0
        self.avg_fill_price = 0.0
        self.status = "open"
        self.created_at = datetime.now()
        self.filled_at = None

    @property
    def remaining(self):
        return self.quantity - self.filled_qty

    def __repr__(self):
        return (f"PaperOrder({self.order_id} | {self.ticker} | "
                f"{self.side.upper()} {self.quantity}@{self.price}c | {self.status})")


class PaperTradingEngine:
    """
    Simulates order execution against real Kalshi order book snapshots.

    Workflow:
      1. Call place_order() — creates a PaperOrder and attempts to fill it
         against the current order book snapshot.
      2. Limit orders: fill only at or better than limit price, walking the book.
      3. Market orders: fill at best available price, walking the book.
      4. Unfilled limit orders stay open; call try_fill_open_orders() with a
         fresh order book to give them another chance.
      5. cancel_order() removes an open order.

    All state is in-memory — nothing touches Kalshi's servers.
    """

    def __init__(self, starting_balance=10_000):
        """
        starting_balance: Paper dollars to trade with (cents in Kalshi terms: $1 = 100 contracts)
        """
        self.balance = starting_balance          # Available cash (dollars)
        self.reserved = 0.0                      # Cash locked in open orders
        self.positions = defaultdict(int)        # {ticker: net_contracts (+yes / -no)}
        self.open_orders = {}                    # {order_id: PaperOrder}
        self.filled_orders = []                  # Completed orders
        self.trade_log = []                      # Full fill history
        self.pnl_history = [starting_balance]    # Equity curve

    # ==================== ORDER PLACEMENT ====================

    def place_order(self, ticker, side, quantity, price=None,
                    order_type="limit", orderbook=None):
        """
        Place a paper order and attempt immediate fill against the provided order book.

        ticker: Market identifier (e.g. 'NBA-BKNCLE-24556983-T230.5')
        side: 'yes' or 'no'
        quantity: Number of contracts
        price: Limit price in cents (1-99). None triggers market order.
        order_type: 'limit' or 'market'
        orderbook: Current order book dict from KalshiClient.get_orderbook().
                   If None, order is queued as open without attempting fill.

        Returns: PaperOrder object
        """
        order_type = "market" if price is None else order_type
        order = PaperOrder(ticker, side, order_type, quantity, price)

        # Reserve capital: worst-case cost of this order
        cost = self._estimate_cost(side, price, quantity)
        if cost > self.balance:
            print(f"  [Paper] Rejected {order}: insufficient balance "
                  f"(need ${cost:.2f}, have ${self.balance:.2f})")
            order.status = "cancelled"
            return order

        self.balance -= cost
        self.reserved += cost

        if orderbook:
            self._attempt_fill(order, orderbook)

        if order.status == "open":
            self.open_orders[order.order_id] = order
            print(f"  [Paper] Queued:  {order}")
        else:
            print(f"  [Paper] Filled:  {order} @ avg {order.avg_fill_price:.1f}c")

        return order

    def try_fill_open_orders(self, ticker, orderbook):
        """
        Attempt to fill any open limit orders for a given ticker using a fresh order book.
        Call this whenever you fetch a new order book snapshot.
        """
        to_remove = []
        for order_id, order in self.open_orders.items():
            if order.ticker != ticker:
                continue
            self._attempt_fill(order, orderbook)
            if order.status in ("filled", "cancelled"):
                to_remove.append(order_id)

        for order_id in to_remove:
            self.filled_orders.append(self.open_orders.pop(order_id))

    def cancel_order(self, order_id):
        """Cancel an open order and return reserved capital."""
        if order_id not in self.open_orders:
            print(f"  [Paper] Order {order_id} not found or already filled.")
            return False

        order = self.open_orders.pop(order_id)
        order.status = "cancelled"
        self.filled_orders.append(order)

        # Return reserved capital for unfilled portion
        unfilled_cost = self._estimate_cost(order.side, order.price, order.remaining)
        self.reserved -= unfilled_cost
        self.balance += unfilled_cost

        print(f"  [Paper] Cancelled: {order}")
        return True

    # ==================== FILL SIMULATION ====================

    def _attempt_fill(self, order, orderbook):
        """
        Walk the order book and fill as much of the order as available.

        For a YES buy: we're lifting the ask side (no_bids converted to yes_asks).
        For a NO buy: we're lifting the bid side (yes_bids converted to no_asks).
        """
        if order.side == "yes":
            # Buying YES = hitting the ask side = taking no_bids (they're selling YES)
            # no_bid at price P means someone will sell YES at (100 - P)
            no_bids = orderbook.get("no", [])
            available = sorted(
                [{"price": 100 - nb["price"], "delta": nb.get("delta", nb.get("quantity", 0))}
                 for nb in no_bids],
                key=lambda x: x["price"]
            )
            # For limit: only fill at or below limit price
            if order.order_type == "limit" and order.price:
                available = [l for l in available if l["price"] <= order.price]
        else:
            # Buying NO = hitting the ask side of NO = taking yes_bids (they're selling NO)
            # yes_bid at price P means someone will sell NO at (100 - P)
            yes_bids = orderbook.get("yes", [])
            available = sorted(
                [{"price": 100 - yb["price"], "delta": yb.get("delta", yb.get("quantity", 0))}
                 for yb in yes_bids],
                key=lambda x: x["price"]
            )
            if order.order_type == "limit" and order.price:
                available = [l for l in available if l["price"] <= order.price]

        if not available:
            return

        total_cost = 0.0
        total_filled = 0

        for level in available:
            if total_filled >= order.remaining:
                break
            fill_qty = min(level["delta"], order.remaining - total_filled)
            fill_price = level["price"]
            total_cost += fill_qty * fill_price
            total_filled += fill_qty

            self.trade_log.append({
                "order_id": order.order_id,
                "ticker": order.ticker,
                "side": order.side,
                "fill_qty": fill_qty,
                "fill_price": fill_price,
                "timestamp": datetime.now(),
            })

        if total_filled == 0:
            return

        # Update order state
        order.avg_fill_price = (
            (order.avg_fill_price * order.filled_qty + total_cost)
            / (order.filled_qty + total_filled)
        )
        order.filled_qty += total_filled
        order.status = "filled" if order.filled_qty >= order.quantity else "partial"
        if order.status == "filled":
            order.filled_at = datetime.now()

        # Settle capital: return reservation, deduct actual cost
        actual_cost = total_cost / 100  # cents -> dollars
        reserved_for_filled = self._estimate_cost(order.side, order.price, total_filled)
        self.reserved -= reserved_for_filled
        self.balance += reserved_for_filled - actual_cost

        # Update position
        if order.side == "yes":
            self.positions[order.ticker] += total_filled
        else:
            self.positions[order.ticker] -= total_filled

    # ==================== PORTFOLIO ====================

    def get_portfolio_summary(self):
        """Show current paper portfolio state."""
        print("\n" + "=" * 55)
        print("PAPER PORTFOLIO")
        print("=" * 55)
        print(f"  Cash balance:    ${self.balance:.2f}")
        print(f"  Reserved (open): ${self.reserved:.2f}")
        print(f"  Total equity:    ${self.balance + self.reserved:.2f}")
        print(f"  Open orders:     {len(self.open_orders)}")
        print(f"  Filled orders:   {len(self.filled_orders)}")

        if self.positions:
            print(f"\n  Positions:")
            for ticker, qty in self.positions.items():
                if qty != 0:
                    direction = "LONG YES" if qty > 0 else "SHORT YES (LONG NO)"
                    print(f"    {ticker}: {abs(qty)} contracts {direction}")
        else:
            print(f"\n  No open positions.")

        return {
            "balance": self.balance,
            "reserved": self.reserved,
            "equity": self.balance + self.reserved,
            "positions": dict(self.positions),
            "open_orders": len(self.open_orders),
        }

    def mark_to_market(self, market_prices):
        """
        Estimate current P&L by marking positions to current mid prices.

        market_prices: dict of {ticker: mid_price_cents}
        """
        mtm_pnl = 0.0
        for ticker, qty in self.positions.items():
            if ticker in market_prices and qty != 0:
                mid = market_prices[ticker] / 100
                # Long YES: value = qty * mid_prob
                # Short YES (Long NO): value = |qty| * (1 - mid_prob)
                if qty > 0:
                    mtm_pnl += qty * mid
                else:
                    mtm_pnl += abs(qty) * (1 - mid)
        return round(mtm_pnl, 2)

    def _estimate_cost(self, side, price, quantity):
        """Estimate dollar cost to reserve for an order."""
        if price is None:
            # Market order: conservatively reserve worst-case (99c for yes, 99c for no)
            price = 99
        cost_per_contract = price / 100  # cents -> dollars
        return cost_per_contract * quantity


# ==================== Demo ====================
if __name__ == "__main__":
    print("=" * 60)
    print("PAPER TRADING ENGINE DEMO")
    print("=" * 60)

    # Simulate a realistic Kalshi order book snapshot
    # This is what KalshiClient.get_orderbook() returns
    mock_orderbook = {
        "yes": [  # yes bids: people willing to buy YES
            {"price": 58, "delta": 50},
            {"price": 57, "delta": 30},
            {"price": 55, "delta": 100},
        ],
        "no": [   # no bids: people willing to buy NO (= selling YES)
            {"price": 40, "delta": 25},   # implies YES ask at 60
            {"price": 39, "delta": 40},   # implies YES ask at 61
            {"price": 37, "delta": 80},   # implies YES ask at 63
        ],
    }

    engine = PaperTradingEngine(starting_balance=1000)

    print("\n--- Placing a limit buy order for YES at 61c ---")
    order1 = engine.place_order(
        ticker="NBA-EXAMPLE-OVER",
        side="yes",
        quantity=30,
        price=61,
        orderbook=mock_orderbook,
    )

    print("\n--- Placing a market buy order for NO (10 contracts) ---")
    order2 = engine.place_order(
        ticker="NBA-EXAMPLE-OVER",
        side="no",
        quantity=10,
        orderbook=mock_orderbook,
    )

    print("\n--- Placing a limit order that won't fill yet (price too low) ---")
    order3 = engine.place_order(
        ticker="NBA-EXAMPLE-OVER",
        side="yes",
        quantity=20,
        price=55,   # below best ask of 60c — stays open
        orderbook=mock_orderbook,
    )

    engine.get_portfolio_summary()

    print("\n--- New order book: ask dropped to 55c ---")
    updated_orderbook = {
        "yes": [{"price": 54, "delta": 50}],
        "no": [{"price": 45, "delta": 100}],   # implies YES ask at 55c
    }
    engine.try_fill_open_orders("NBA-EXAMPLE-OVER", updated_orderbook)
    engine.get_portfolio_summary()
