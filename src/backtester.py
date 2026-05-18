"""
Backtesting Engine
Tests trading strategies against historical prediction market data.
Tracks P&L, Sharpe ratio, CLV, drawdown, and other performance metrics.
"""
import numpy as np
import pandas as pd
from datetime import datetime


class Backtester:
    """
    Backtesting framework for prediction market strategies.
    Simulates trading on historical data and calculates performance metrics.
    """

    def __init__(self, initial_capital=10000, fee_rate=0.07):
        """
        initial_capital: Starting bankroll
        fee_rate: Kalshi taker fee rate (7% of price * (1-price))
        """
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate
        self.trades = []
        self.equity_curve = [initial_capital]
        self.capital = initial_capital

    def calculate_fee(self, price):
        """Calculate Kalshi taker fee for a trade."""
        # Fee = fee_rate * price * (1 - price)
        # Maximum at midpoint (50 cents): 0.07 * 0.5 * 0.5 = $0.0175
        p = price / 100
        return self.fee_rate * p * (1 - p)

    def execute_trade(self, entry_price, exit_price=None, side="yes",
                      quantity=1, settled=None, ticker="", event="",
                      entry_time=None, exit_time=None):
        """
        Record a trade with entry and exit/settlement.

        entry_price: Price paid (cents, 1-99)
        exit_price: Price sold at (cents) if closed before settlement
        side: 'yes' or 'no'
        quantity: Number of contracts
        settled: Settlement result (1=yes happened, 0=no happened, None=not settled)
        """
        entry_cost = (entry_price / 100) * quantity
        entry_fee = self.calculate_fee(entry_price) * quantity

        if exit_price is not None:
            # Closed before settlement
            exit_revenue = (exit_price / 100) * quantity
            exit_fee = self.calculate_fee(exit_price) * quantity
            pnl = exit_revenue - entry_cost - entry_fee - exit_fee
        elif settled is not None:
            # Settled
            if side == "yes":
                settlement_value = settled * quantity
            else:
                settlement_value = (1 - settled) * quantity
            pnl = settlement_value - entry_cost - entry_fee
        else:
            pnl = 0  # Still open

        self.capital += pnl
        self.equity_curve.append(self.capital)

        # CLV: compare entry price against closing/settlement price
        if exit_price is not None:
            clv = exit_price - entry_price if side == "yes" else entry_price - exit_price
        elif settled is not None:
            closing_price = settled * 100
            clv = closing_price - entry_price if side == "yes" else entry_price - closing_price
        else:
            clv = 0

        trade = {
            "ticker": ticker,
            "event": event,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "settled": settled,
            "pnl": round(pnl, 4),
            "clv": round(clv, 2),
            "fees": round(entry_fee + (self.calculate_fee(exit_price) * quantity if exit_price else 0), 4),
            "capital_after": round(self.capital, 2),
            "entry_time": entry_time or datetime.now(),
            "exit_time": exit_time,
        }
        self.trades.append(trade)
        return trade

    def run_strategy(self, signals, prices, settlements):
        """
        Run a systematic strategy on historical data.

        signals: List of dicts with {ticker, side, model_prob, entry_price, entry_time}
        prices: Dict of {ticker: closing_price}
        settlements: Dict of {ticker: 0 or 1}
        """
        for signal in signals:
            ticker = signal["ticker"]
            settled = settlements.get(ticker)

            self.execute_trade(
                entry_price=signal["entry_price"],
                side=signal["side"],
                quantity=signal.get("quantity", 1),
                settled=settled,
                ticker=ticker,
                event=signal.get("event", ""),
                entry_time=signal.get("entry_time"),
            )

        return self.get_performance_metrics()

    def get_performance_metrics(self):
        """Calculate comprehensive performance metrics."""
        if not self.trades:
            return {"message": "No trades to analyze"}

        df = pd.DataFrame(self.trades)

        total_pnl = df["pnl"].sum()
        total_fees = df["fees"].sum()
        win_rate = len(df[df["pnl"] > 0]) / len(df) if len(df) > 0 else 0

        # CLV metrics
        avg_clv = df["clv"].mean()
        positive_clv_rate = len(df[df["clv"] > 0]) / len(df) if len(df) > 0 else 0

        # Equity curve metrics
        equity = np.array(self.equity_curve)
        returns = np.diff(equity) / equity[:-1]

        # Sharpe ratio (annualized, assuming daily trades)
        if len(returns) > 1 and returns.std() > 0:
            sharpe = (returns.mean() / returns.std()) * np.sqrt(252)
        else:
            sharpe = 0

        # Max drawdown
        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak
        max_drawdown = drawdown.max()

        # Profit factor
        gross_profit = df[df["pnl"] > 0]["pnl"].sum()
        gross_loss = abs(df[df["pnl"] < 0]["pnl"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        return {
            "total_trades": len(df),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "net_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 4),
            "avg_pnl_per_trade": round(total_pnl / len(df), 4),
            "avg_clv": round(avg_clv, 2),
            "positive_clv_rate": round(positive_clv_rate, 4),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown": round(max_drawdown, 4),
            "profit_factor": round(profit_factor, 2),
            "final_capital": round(self.capital, 2),
            "total_return": round((self.capital - self.initial_capital) / self.initial_capital, 4),
        }

    def get_trade_log(self):
        """Get all trades as DataFrame."""
        return pd.DataFrame(self.trades)

    def get_equity_curve(self):
        """Get equity curve as array."""
        return np.array(self.equity_curve)


def demo_backtest():
    """Demonstrate backtesting with simulated trades."""
    print("=" * 60)
    print("BACKTESTING ENGINE DEMO")
    print("=" * 60)

    bt = Backtester(initial_capital=1000)

    # Simulate 50 trades with positive edge
    np.random.seed(42)

    for i in range(50):
        # Model finds edge: true prob 58%, market at 50 cents
        entry_price = 50
        true_prob = 0.58
        settled = 1 if np.random.random() < true_prob else 0

        bt.execute_trade(
            entry_price=entry_price,
            side="yes",
            quantity=5,
            settled=settled,
            ticker=f"SIM-{i:03d}",
            event=f"Simulated Event {i}",
        )

    metrics = bt.get_performance_metrics()

    print(f"\n  --- Strategy Performance ---")
    print(f"  Total Trades:     {metrics['total_trades']}")
    print(f"  Net P&L:          ${metrics['net_pnl']:.2f}")
    print(f"  Win Rate:         {metrics['win_rate']:.1%}")
    print(f"  Avg P&L/Trade:    ${metrics['avg_pnl_per_trade']:.4f}")
    print(f"  Avg CLV:          {metrics['avg_clv']:.1f} cents")
    print(f"  Positive CLV:     {metrics['positive_clv_rate']:.1%}")
    print(f"  Sharpe Ratio:     {metrics['sharpe_ratio']}")
    print(f"  Max Drawdown:     {metrics['max_drawdown']:.1%}")
    print(f"  Profit Factor:    {metrics['profit_factor']}")
    print(f"  Final Capital:    ${metrics['final_capital']:.2f}")
    print(f"  Total Return:     {metrics['total_return']:.1%}")


if __name__ == "__main__":
    demo_backtest()
