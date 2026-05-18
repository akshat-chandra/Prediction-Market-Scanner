"""
Cross-Platform Arbitrage Detection
Compares Kalshi prediction market prices against sportsbook lines
to identify mispricings and arbitrage opportunities.
"""
import pandas as pd
import numpy as np
from datetime import datetime


class ArbitrageScanner:
    """
    Detect price discrepancies between prediction markets and sportsbooks.
    Converts between different odds formats and identifies exploitable gaps.
    """

    def __init__(self, min_edge=0.03):
        self.min_edge = min_edge
        self.opportunities = []

    @staticmethod
    def american_to_probability(american_odds):
        """Convert American odds to implied probability."""
        if american_odds > 0:
            return 100 / (american_odds + 100)
        else:
            return abs(american_odds) / (abs(american_odds) + 100)

    @staticmethod
    def decimal_to_probability(decimal_odds):
        """Convert decimal odds to implied probability."""
        return 1 / decimal_odds

    @staticmethod
    def probability_to_american(prob):
        """Convert probability to American odds."""
        if prob >= 0.5:
            return -round(prob / (1 - prob) * 100)
        else:
            return round((1 - prob) / prob * 100)

    @staticmethod
    def kalshi_price_to_probability(price_cents):
        """Convert Kalshi contract price (cents) to probability."""
        return price_cents / 100

    def compare_markets(self, kalshi_price_cents, sportsbook_odds,
                        odds_format="american", ticker="", event=""):
        """
        Compare a Kalshi contract price against sportsbook odds.

        kalshi_price_cents: Kalshi YES contract price (1-99 cents)
        sportsbook_odds: Odds from sportsbook
        odds_format: 'american' or 'decimal'

        Returns: arbitrage opportunity details
        """
        kalshi_prob = self.kalshi_price_to_probability(kalshi_price_cents)

        if odds_format == "american":
            book_prob = self.american_to_probability(sportsbook_odds)
        else:
            book_prob = self.decimal_to_probability(sportsbook_odds)

        # Edge: difference between the two prices
        # Positive edge = Kalshi is cheaper (buy on Kalshi)
        # Negative edge = sportsbook is cheaper (bet on sportsbook)
        edge_kalshi_cheap = book_prob - kalshi_prob  # Buy YES on Kalshi
        edge_book_cheap = kalshi_prob - book_prob     # Bet on sportsbook

        # Pure arbitrage: lock in profit regardless of outcome by taking both sides.
        #
        # Leg A: Buy YES on Kalshi at kalshi_prob cost per contract.
        # Leg B: Bet the NO side on the sportsbook at (1 - book_no_prob) implied cost.
        #
        # If Leg A cost + Leg B cost < 1.0, you collect $1 on whichever outcome
        # wins while paying less than $1 total — guaranteed profit.
        #
        # We check both orientations:
        #   - Buy YES Kalshi + Bet NO on book: kalshi_prob + (1 - book_prob) < 1
        #     Simplifies to: kalshi_prob < book_prob (Kalshi cheaper on YES side)
        #     -> This alone is just a directional edge, NOT a pure arb.
        #
        # Real pure arb requires the combined implied probability of BOTH legs < 1.
        # Because sportsbooks don't offer binary NO contracts directly, we compute
        # the true no-vig implied probability of the NO side from the sportsbook.
        # If the book has vig, the NO side implied prob = 1 - book_prob_with_vig.
        # We conservatively use the raw book_prob here (assumes no vig removal).
        #
        # Arb 1: Buy YES Kalshi + Bet NO on book (profit if YES wins AND NO pays >remainder)
        arb_yes_kalshi = kalshi_prob + (1 - book_prob)   # cost to lock in $1 on YES
        # Arb 2: Buy NO Kalshi + Bet YES on book
        kalshi_no_prob = (100 - kalshi_price_cents) / 100
        arb_no_kalshi = kalshi_no_prob + book_prob        # cost to lock in $1 on NO

        pure_arb = (arb_yes_kalshi < 1.0) or (arb_no_kalshi < 1.0)
        arb_profit = round(max(1 - arb_yes_kalshi, 1 - arb_no_kalshi), 4) if pure_arb else 0

        opportunity = {
            "ticker": ticker,
            "event": event,
            "kalshi_yes": kalshi_price_cents,
            "kalshi_prob": round(kalshi_prob, 4),
            "sportsbook_odds": sportsbook_odds,
            "sportsbook_prob": round(book_prob, 4),
            "edge_buy_kalshi": round(edge_kalshi_cheap, 4),
            "edge_bet_book": round(edge_book_cheap, 4),
            "pure_arbitrage": pure_arb,
            "arb_profit_per_dollar": arb_profit,
            "best_action": self._recommend_action(edge_kalshi_cheap, edge_book_cheap, pure_arb),
            "timestamp": datetime.now(),
        }

        if abs(edge_kalshi_cheap) >= self.min_edge or pure_arb:
            self.opportunities.append(opportunity)

        return opportunity

    def _recommend_action(self, edge_kalshi, edge_book, pure_arb=False):
        """Determine best action based on edges."""
        if pure_arb:
            return "PURE ARB: take both sides"
        elif edge_kalshi > self.min_edge:
            return "BUY YES on Kalshi"
        elif edge_book > self.min_edge:
            return "BET on Sportsbook"
        else:
            return "NO TRADE"

    def scan_batch(self, comparisons):
        """
        Scan multiple market comparisons at once.

        comparisons: List of dicts with keys:
            kalshi_price, sportsbook_odds, odds_format, ticker, event
        """
        results = []
        for comp in comparisons:
            result = self.compare_markets(
                kalshi_price_cents=comp["kalshi_price"],
                sportsbook_odds=comp["sportsbook_odds"],
                odds_format=comp.get("odds_format", "american"),
                ticker=comp.get("ticker", ""),
                event=comp.get("event", ""),
            )
            results.append(result)

        df = pd.DataFrame(results)
        if not df.empty:
            df = df.sort_values("edge_buy_kalshi", key=abs, ascending=False)
        return df

    def get_opportunities(self, min_edge=None):
        """Get all detected opportunities."""
        edge = min_edge or self.min_edge
        df = pd.DataFrame(self.opportunities)
        if not df.empty:
            df = df[df["edge_buy_kalshi"].abs() >= edge]
            df = df.sort_values("edge_buy_kalshi", key=abs, ascending=False)
        return df


class OddsConverter:
    """Utility class for converting between odds formats."""

    @staticmethod
    def american_to_decimal(american):
        if american > 0:
            return (american / 100) + 1
        else:
            return (100 / abs(american)) + 1

    @staticmethod
    def decimal_to_american(decimal):
        if decimal >= 2.0:
            return round((decimal - 1) * 100)
        else:
            return round(-100 / (decimal - 1))

    @staticmethod
    def implied_probability_with_vig(odds_a, odds_b, format="american"):
        """
        Calculate implied probabilities including vig/overround.

        Returns true probabilities after removing vig.
        """
        if format == "american":
            prob_a = ArbitrageScanner.american_to_probability(odds_a)
            prob_b = ArbitrageScanner.american_to_probability(odds_b)
        else:
            prob_a = 1 / odds_a
            prob_b = 1 / odds_b

        total = prob_a + prob_b  # Will be > 1 due to vig
        vig = total - 1

        # Remove vig proportionally
        true_prob_a = prob_a / total
        true_prob_b = prob_b / total

        return {
            "prob_a": round(true_prob_a, 4),
            "prob_b": round(true_prob_b, 4),
            "vig": round(vig, 4),
            "vig_pct": f"{vig:.1%}",
        }


def demo_arbitrage():
    """Demonstrate arbitrage detection."""
    print("=" * 60)
    print("ARBITRAGE SCANNER DEMO")
    print("=" * 60)

    scanner = ArbitrageScanner(min_edge=0.02)

    # Example comparisons
    comparisons = [
        {
            "ticker": "NBA-CEL-ML",
            "event": "Celtics vs Knicks - Celtics Win",
            "kalshi_price": 62,  # Kalshi YES at 62 cents
            "sportsbook_odds": -180,  # Sportsbook at -180
        },
        {
            "ticker": "NBA-TOTAL-OVER",
            "event": "Celtics vs Knicks Over 224.5",
            "kalshi_price": 48,
            "sportsbook_odds": -105,
        },
        {
            "ticker": "NFL-KC-ML",
            "event": "Chiefs vs Bills - Chiefs Win",
            "kalshi_price": 55,
            "sportsbook_odds": +110,
        },
        {
            "ticker": "NHL-BOS-ML",
            "event": "Bruins vs Leafs - Bruins Win",
            "kalshi_price": 58,
            "sportsbook_odds": -150,
        },
    ]

    results = scanner.scan_batch(comparisons)

    print(f"\nScanned {len(comparisons)} markets:\n")
    for _, row in results.iterrows():
        edge = row["edge_buy_kalshi"]
        print(f"  {row['event']}")
        print(f"    Kalshi: {row['kalshi_yes']}c ({row['kalshi_prob']:.1%}) | "
              f"Book: {row['sportsbook_odds']} ({row['sportsbook_prob']:.1%})")
        print(f"    Edge: {edge:+.1%} | Action: {row['best_action']}")
        print()

    # Odds converter demo
    print("--- Odds Conversion ---")
    converter = OddsConverter()
    vig_result = converter.implied_probability_with_vig(-110, -110)
    print(f"  Standard -110/-110 line:")
    print(f"    True probabilities: {vig_result['prob_a']:.1%} / {vig_result['prob_b']:.1%}")
    print(f"    Vig: {vig_result['vig_pct']}")


if __name__ == "__main__":
    demo_arbitrage()
