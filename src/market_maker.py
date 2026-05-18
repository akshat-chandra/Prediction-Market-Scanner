"""
Market Making Strategy Engine
Simulates and executes a market-making strategy on prediction market contracts.
Posts symmetric bids/asks around fair value, manages inventory, and adjusts to flow.
"""
import numpy as np
import pandas as pd
from datetime import datetime


class MarketMaker:
    """
    Market making engine for sports prediction contracts.
    Posts bids and asks around model fair value, adjusts based on fills and flow.
    """

    def __init__(self, spread_width=0.05, max_position=100, skew_factor=0.01):
        """
        spread_width: Half-spread in probability terms (0.05 = 5 cents each side)
        max_position: Maximum contracts in either direction
        skew_factor: How much to skew prices per unit of inventory
        """
        self.spread_width = spread_width
        self.max_position = max_position
        self.skew_factor = skew_factor
        self.positions = {}  # {ticker: net_position}
        self.fills = []      # Trade history
        self.pnl = 0.0

    def calculate_quotes(self, ticker, fair_value, confidence="MEDIUM"):
        """
        Calculate bid and ask prices given fair value and current position.

        fair_value: Model's estimated probability (0-1)
        confidence: HIGH/MEDIUM/LOW affects spread width

        Returns: dict with bid_price, ask_price, and reasoning
        """
        # Adjust spread based on confidence
        spread_mult = {"HIGH": 0.7, "MEDIUM": 1.0, "LOW": 1.5}
        adjusted_spread = self.spread_width * spread_mult.get(confidence, 1.0)

        # Skew based on current inventory
        position = self.positions.get(ticker, 0)
        inventory_skew = -position * self.skew_factor

        # Calculate quotes (in probability / dollar terms, 0-1)
        mid = fair_value + inventory_skew
        bid = max(0.01, mid - adjusted_spread)
        ask = min(0.99, mid + adjusted_spread)

        # Convert to cents for Kalshi (1-99)
        bid_cents = int(bid * 100)
        ask_cents = int(ask * 100)

        return {
            "ticker": ticker,
            "fair_value": round(fair_value, 4),
            "bid": bid_cents,
            "ask": ask_cents,
            "spread": ask_cents - bid_cents,
            "midpoint": round(mid, 4),
            "position": position,
            "inventory_skew": round(inventory_skew, 4),
            "confidence": confidence,
        }

    def process_fill(self, ticker, side, price, quantity):
        """
        Process a fill (trade execution).

        side: 'buy' or 'sell'
        price: Fill price in cents (1-99)
        quantity: Number of contracts
        """
        if ticker not in self.positions:
            self.positions[ticker] = 0

        if side == "buy":
            self.positions[ticker] += quantity
            self.pnl -= (price / 100) * quantity
        else:
            self.positions[ticker] -= quantity
            self.pnl += (price / 100) * quantity

        fill = {
            "timestamp": datetime.now(),
            "ticker": ticker,
            "side": side,
            "price": price,
            "quantity": quantity,
            "position_after": self.positions[ticker],
            "cumulative_pnl": round(self.pnl, 4),
        }
        self.fills.append(fill)
        return fill

    def should_requote(self, ticker, current_fair_value, last_quoted_fair_value,
                        threshold=0.02):
        """
        Determine if quotes should be updated based on fair value change.
        """
        if last_quoted_fair_value is None:
            return True
        return abs(current_fair_value - last_quoted_fair_value) >= threshold

    def get_position_summary(self):
        """Get summary of all positions."""
        summary = []
        for ticker, pos in self.positions.items():
            if pos != 0:
                summary.append({
                    "ticker": ticker,
                    "position": pos,
                    "direction": "LONG" if pos > 0 else "SHORT",
                    "abs_position": abs(pos),
                })
        return pd.DataFrame(summary) if summary else pd.DataFrame()

    def get_fill_history(self):
        """Get trade history as DataFrame."""
        return pd.DataFrame(self.fills) if self.fills else pd.DataFrame()

    def check_position_limit(self, ticker, proposed_quantity):
        """Check if a proposed trade would exceed position limits."""
        current = self.positions.get(ticker, 0)
        new_position = current + proposed_quantity
        return abs(new_position) <= self.max_position


class MarketMakerSimulator:
    """
    Backtest market making strategies on historical data.
    Simulates order posting, fills, and P&L tracking.
    """

    def __init__(self, spread_width=0.05, max_position=50):
        self.mm = MarketMaker(spread_width=spread_width, max_position=max_position)
        self.results = []

    def simulate(self, price_series, fair_values, fill_probability=0.3, ticker="SIM"):
        """
        Run market making simulation on historical price data.

        price_series: List of market prices over time (0-100 cents)
        fair_values: List of model fair values over time (0-1)
        fill_probability: Probability of getting filled at each timestep
        ticker: Contract identifier (used for position tracking)
        """
        for i, (market_price, fv) in enumerate(zip(price_series, fair_values)):
            quotes = self.mm.calculate_quotes(ticker, fv)
            market_cents = int(market_price)

            # Check if market price would fill our bid or ask
            filled = False

            # Market trades at or below our bid -> we buy
            if market_cents <= quotes["bid"] and np.random.random() < fill_probability:
                if self.mm.check_position_limit(ticker, 1):
                    self.mm.process_fill(ticker, "buy", quotes["bid"], 1)
                    filled = True

            # Market trades at or above our ask -> we sell
            if market_cents >= quotes["ask"] and np.random.random() < fill_probability:
                if self.mm.check_position_limit(ticker, -1):
                    self.mm.process_fill(ticker, "sell", quotes["ask"], 1)
                    filled = True

            self.results.append({
                "step": i,
                "market_price": market_cents,
                "fair_value": round(fv, 4),
                "bid": quotes["bid"],
                "ask": quotes["ask"],
                "position": self.mm.positions.get(ticker, 0),
                "pnl": round(self.mm.pnl, 4),
                "filled": filled,
            })

        return pd.DataFrame(self.results)

    def calculate_metrics(self, settlement_price):
        """
        Calculate performance metrics after simulation.

        settlement_price: Final settlement (0 or 1 for binary contracts)
        """
        df = pd.DataFrame(self.results)
        fills = self.mm.get_fill_history()

        # Mark-to-market P&L including settlement
        # Sum across all positions since a simulation may use a real ticker name
        final_position = sum(self.mm.positions.values())
        settlement_pnl = final_position * settlement_price
        total_pnl = self.mm.pnl + settlement_pnl

        # Trading metrics
        n_fills = len(fills) if not fills.empty else 0
        n_buys = len(fills[fills["side"] == "buy"]) if not fills.empty else 0
        n_sells = len(fills[fills["side"] == "sell"]) if not fills.empty else 0

        # Spread capture
        if n_buys > 0 and n_sells > 0:
            avg_buy = fills[fills["side"] == "buy"]["price"].mean()
            avg_sell = fills[fills["side"] == "sell"]["price"].mean()
            avg_spread_captured = avg_sell - avg_buy
        else:
            avg_spread_captured = 0

        return {
            "total_pnl": round(total_pnl, 4),
            "trading_pnl": round(self.mm.pnl, 4),
            "settlement_pnl": round(settlement_pnl, 4),
            "total_fills": n_fills,
            "buys": n_buys,
            "sells": n_sells,
            "avg_spread_captured": round(avg_spread_captured, 2),
            "final_position": final_position,
            "max_position": df["position"].abs().max() if not df.empty else 0,
        }


def demo_market_maker():
    """Demonstrate market making strategy."""
    print("=" * 60)
    print("MARKET MAKER SIMULATION")
    print("=" * 60)

    # Generate synthetic price data
    np.random.seed(42)
    n_steps = 200
    true_prob = 0.55  # True probability of event

    # Market price random walks around true value
    prices = np.clip(
        true_prob * 100 + np.cumsum(np.random.normal(0, 1.5, n_steps)),
        5, 95
    )

    # Our model's fair value (slightly noisy estimate of true prob)
    fair_values = np.clip(
        true_prob + np.random.normal(0, 0.02, n_steps),
        0.05, 0.95
    )

    # Run simulation
    sim = MarketMakerSimulator(spread_width=0.04, max_position=20)
    results = sim.simulate(prices, fair_values, fill_probability=0.25)

    # Settlement: event happens (yes = 1)
    settlement = 1.0
    metrics = sim.calculate_metrics(settlement)

    print(f"\n  Simulation: {n_steps} time steps")
    print(f"  True Probability: {true_prob:.0%}")
    print(f"  Settlement: {'YES' if settlement == 1 else 'NO'}")
    print(f"\n  --- Results ---")
    print(f"  Total P&L:       ${metrics['total_pnl']:.2f}")
    print(f"  Trading P&L:     ${metrics['trading_pnl']:.2f}")
    print(f"  Settlement P&L:  ${metrics['settlement_pnl']:.2f}")
    print(f"  Total Fills:     {metrics['total_fills']}")
    print(f"  Buys / Sells:    {metrics['buys']} / {metrics['sells']}")
    print(f"  Avg Spread:      {metrics['avg_spread_captured']}c")
    print(f"  Max Position:    {metrics['max_position']}")


if __name__ == "__main__":
    demo_market_maker()
