"""
Risk Management Module
Monitors portfolio exposure, correlation, drawdowns, and position limits.
"""
import numpy as np
import pandas as pd
from datetime import datetime


class RiskManager:
    """Portfolio-level risk management for prediction market trading."""

    def __init__(self, max_position_per_contract=100,
                 max_portfolio_exposure=1000,
                 max_correlated_exposure=300,
                 drawdown_limit=0.10):
        self.max_position_per_contract = max_position_per_contract
        self.max_portfolio_exposure = max_portfolio_exposure
        self.max_correlated_exposure = max_correlated_exposure
        self.drawdown_limit = drawdown_limit

        self.positions = {}          # {ticker: {side, quantity, avg_price, sport, market_type}}
        self.peak_equity = 0
        self.current_equity = 0
        self.pnl_history = []
        self.alerts = []

    def add_position(self, ticker, side, quantity, price, sport="unknown",
                     market_type="unknown"):
        """Record a new position or update existing one."""
        if ticker in self.positions:
            existing = self.positions[ticker]
            total_qty = existing["quantity"] + quantity
            if total_qty > 0:
                avg_price = ((existing["avg_price"] * existing["quantity"] +
                            price * quantity) / total_qty)
            else:
                avg_price = price
            existing["quantity"] = total_qty
            existing["avg_price"] = round(avg_price, 2)
            # Remove flat positions so portfolio summary stays clean
            if total_qty == 0:
                del self.positions[ticker]
        else:
            self.positions[ticker] = {
                "side": side,
                "quantity": quantity,
                "avg_price": price,
                "sport": sport,
                "market_type": market_type,
                "entry_time": datetime.now(),
            }

    def check_position_limit(self, ticker, proposed_quantity):
        """Check if a trade would violate position limits."""
        current = self.positions.get(ticker, {}).get("quantity", 0)
        new_total = current + proposed_quantity

        if abs(new_total) > self.max_position_per_contract:
            self.alerts.append({
                "type": "POSITION_LIMIT",
                "ticker": ticker,
                "message": f"Would exceed {self.max_position_per_contract} contract limit",
                "timestamp": datetime.now(),
            })
            return False
        return True

    def check_portfolio_exposure(self):
        """Calculate and check total portfolio exposure."""
        total_exposure = sum(
            abs(p["quantity"]) * (p["avg_price"] / 100)
            for p in self.positions.values()
        )

        if total_exposure > self.max_portfolio_exposure:
            self.alerts.append({
                "type": "PORTFOLIO_EXPOSURE",
                "message": f"Total exposure ${total_exposure:.0f} exceeds ${self.max_portfolio_exposure} limit",
                "timestamp": datetime.now(),
            })
            return False, total_exposure
        return True, total_exposure

    def check_correlation_risk(self):
        """
        Check for concentrated directional exposure in correlated positions.
        E.g., multiple NBA overs = correlated risk.
        """
        # Group by sport and market type
        sport_exposure = {}
        for ticker, pos in self.positions.items():
            key = f"{pos['sport']}_{pos['market_type']}"
            if key not in sport_exposure:
                sport_exposure[key] = {"long": 0, "short": 0, "tickers": []}

            if pos["side"] == "yes":
                sport_exposure[key]["long"] += pos["quantity"]
            else:
                sport_exposure[key]["short"] += pos["quantity"]
            sport_exposure[key]["tickers"].append(ticker)

        warnings = []
        for group, exposure in sport_exposure.items():
            net = exposure["long"] - exposure["short"]
            if abs(net) > self.max_correlated_exposure / 10:  # Simplified threshold
                direction = "LONG" if net > 0 else "SHORT"
                warnings.append({
                    "group": group,
                    "direction": direction,
                    "net_exposure": net,
                    "tickers": exposure["tickers"],
                    "message": f"Concentrated {direction} in {group}: {abs(net)} net contracts",
                })

        if warnings:
            for w in warnings:
                self.alerts.append({
                    "type": "CORRELATION",
                    "message": w["message"],
                    "timestamp": datetime.now(),
                })

        return warnings

    def check_drawdown(self, current_equity):
        """Monitor drawdown from peak equity."""
        self.current_equity = current_equity

        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        if self.peak_equity > 0:
            drawdown = (self.peak_equity - current_equity) / self.peak_equity
        else:
            drawdown = 0

        self.pnl_history.append({
            "timestamp": datetime.now(),
            "equity": current_equity,
            "peak": self.peak_equity,
            "drawdown": round(drawdown, 4),
        })

        if drawdown >= self.drawdown_limit:
            self.alerts.append({
                "type": "DRAWDOWN",
                "message": f"Drawdown {drawdown:.1%} exceeds {self.drawdown_limit:.0%} limit. Reduce positions.",
                "timestamp": datetime.now(),
            })
            return True, drawdown

        return False, drawdown

    def get_portfolio_summary(self):
        """Generate comprehensive portfolio risk summary."""
        if not self.positions:
            return {"message": "No positions"}

        total_long = sum(p["quantity"] for p in self.positions.values()
                        if p["side"] == "yes")
        total_short = sum(p["quantity"] for p in self.positions.values()
                         if p["side"] == "no")

        # Exposure by sport
        sport_summary = {}
        for ticker, pos in self.positions.items():
            sport = pos["sport"]
            if sport not in sport_summary:
                sport_summary[sport] = {"contracts": 0, "tickers": 0}
            sport_summary[sport]["contracts"] += pos["quantity"]
            sport_summary[sport]["tickers"] += 1

        _, total_exposure = self.check_portfolio_exposure()
        correlation_warnings = self.check_correlation_risk()

        return {
            "total_positions": len(self.positions),
            "total_long_contracts": total_long,
            "total_short_contracts": total_short,
            "net_exposure": total_long - total_short,
            "total_dollar_exposure": round(total_exposure, 2),
            "by_sport": sport_summary,
            "correlation_warnings": len(correlation_warnings),
            "active_alerts": len(self.alerts),
        }

    def get_alerts(self, clear=False):
        """Get and optionally clear active alerts."""
        alerts = self.alerts.copy()
        if clear:
            self.alerts = []
        return alerts


def demo_risk_manager():
    """Demonstrate risk management."""
    print("=" * 60)
    print("RISK MANAGEMENT DEMO")
    print("=" * 60)

    rm = RiskManager(
        max_position_per_contract=50,
        max_portfolio_exposure=500,
        drawdown_limit=0.10,
    )

    # Add some positions
    rm.add_position("NBA-CEL-NYK-OVER", "yes", 20, 55, sport="nba", market_type="total")
    rm.add_position("NBA-MIA-BOS-OVER", "yes", 15, 48, sport="nba", market_type="total")
    rm.add_position("NBA-LAL-GSW-OVER", "yes", 25, 52, sport="nba", market_type="total")
    rm.add_position("NHL-BOS-TOR-UNDER", "no", 10, 45, sport="nhl", market_type="total")

    # Check portfolio
    summary = rm.get_portfolio_summary()
    print(f"\n  Total Positions: {summary['total_positions']}")
    print(f"  Long Contracts:  {summary['total_long_contracts']}")
    print(f"  Short Contracts: {summary['total_short_contracts']}")
    print(f"  Net Exposure:    {summary['net_exposure']}")
    print(f"  Dollar Exposure: ${summary['total_dollar_exposure']:.2f}")
    print(f"  By Sport:        {summary['by_sport']}")

    # Check correlation
    print(f"\n  Correlation Warnings: {summary['correlation_warnings']}")

    # Check alerts
    alerts = rm.get_alerts()
    if alerts:
        print(f"\n  --- ALERTS ---")
        for alert in alerts:
            print(f"  [{alert['type']}] {alert['message']}")
    else:
        print(f"\n  No alerts. Portfolio within limits.")

    # Simulate drawdown
    print(f"\n  --- Drawdown Check ---")
    rm.check_drawdown(1000)   # Peak
    rm.check_drawdown(950)    # Small dip
    breached, dd = rm.check_drawdown(880)  # Bigger dip
    print(f"  Current Drawdown: {dd:.1%}")
    print(f"  Limit Breached: {breached}")


if __name__ == "__main__":
    demo_risk_manager()
