"""
Fair Value Estimation Engine
Generates independent probability estimates for sports prediction contracts
using regression-based models and Bayesian updates.
"""
import numpy as np
import pandas as pd
from scipy import stats


class FairValueEngine:
    """
    Estimates fair value for sports prediction market contracts.
    Compares model-generated probabilities against market prices to find edge.
    """

    def __init__(self):
        # Default model weights (would be calibrated on historical data)
        self.nba_weights = {
            "pace": 0.35,
            "off_efficiency": 0.25,
            "def_efficiency": 0.25,
            "efg": 0.05,
            "tov_rate": 0.05,
            "orb_rate": 0.03,
            "ft_rate": 0.02,
        }
        self.confidence_default = 0.6  # Default confidence in model estimate

    def estimate_total_probability(self, model_total, market_line, model_std=8.0):
        """
        Given a model's projected total and the market line,
        estimate the probability of the over hitting.

        model_total: Your model's projected total points
        market_line: The prediction market's implied total (or contract threshold)
        model_std: Standard deviation of your model's error (calibrated historically)

        Returns: probability that the actual total exceeds market_line

        Example: model projects 228, line is 224.5, std=8
          -> P(actual > 224.5) given N(228, 8) ~ 67% (over is likely)
        """
        # sf (survival function) = P(X > market_line) where X ~ N(model_total, model_std)
        prob_over = stats.norm.sf(market_line, loc=model_total, scale=model_std)
        return round(prob_over, 4)

    def estimate_spread_probability(self, model_spread, market_spread, model_std=6.0):
        """
        Estimate probability that the favorite covers the spread.

        model_spread: Your model's projected margin from the favorite's perspective
                      (positive = favorite wins by that many points, e.g. +7)
        market_spread: The spread the favorite must cover (e.g. +5.5 means win by 5.5)
        model_std: Standard deviation of spread prediction error

        Use positive margins for both inputs — "favorite wins by X."

        Example: model says favorite wins by 7 (model_spread=7),
                 market requires winning by 5.5 (market_spread=5.5)
                 -> P(actual > 5.5 | N(7, 6)) ~ 60% -> lean cover
        """
        # P(actual margin > market_spread) = sf(market_spread, loc=model_spread, scale=std)
        prob_cover = stats.norm.sf(market_spread, loc=model_spread, scale=model_std)
        return round(prob_cover, 4)

    def estimate_moneyline_probability(self, power_rating_a, power_rating_b,
                                        home_advantage=3.0, is_home_a=True):
        """
        Estimate win probability from power ratings.

        power_rating_a/b: Team power ratings (points above average)
        home_advantage: Home court advantage in points
        is_home_a: Whether team A is home
        """
        diff = power_rating_a - power_rating_b
        if is_home_a:
            diff += home_advantage
        else:
            diff -= home_advantage

        # Convert point differential to win probability
        # Empirical relationship: each point of expected margin ~ 3% win probability
        # Logistic function provides smooth mapping
        prob_a = 1 / (1 + 10 ** (-diff / 6))

        return round(prob_a, 4)

    def calculate_edge(self, model_prob, market_prob):
        """
        Calculate the edge between model probability and market price.

        model_prob: Your estimated probability (0-1)
        market_prob: Market implied probability (contract price / 100)

        Returns: dict with edge metrics
        """
        edge = model_prob - market_prob
        edge_pct = edge / market_prob if market_prob > 0 else 0

        # Kelly fraction for optimal sizing
        if edge > 0:
            # Simplified Kelly: edge / odds
            odds = (1 / market_prob) - 1 if market_prob < 1 else 0
            kelly = edge / (1 - market_prob) if market_prob < 1 else 0
        else:
            kelly = 0

        return {
            "model_prob": model_prob,
            "market_prob": market_prob,
            "edge": round(edge, 4),
            "edge_pct": round(edge_pct, 4),
            "kelly_fraction": round(max(kelly, 0), 4),
            "signal": "BUY YES" if edge > 0.03 else "BUY NO" if edge < -0.03 else "NO TRADE",
            "confidence": self._confidence_level(abs(edge)),
        }

    def _confidence_level(self, abs_edge):
        """Categorize confidence based on edge magnitude."""
        if abs_edge >= 0.10:
            return "HIGH"
        elif abs_edge >= 0.05:
            return "MEDIUM"
        elif abs_edge >= 0.03:
            return "LOW"
        else:
            return "NO EDGE"

    def bayesian_update(self, prior_prob, evidence_likelihood_true,
                         evidence_likelihood_false):
        """
        Update probability estimate using Bayes' theorem.

        prior_prob: Prior probability of the event
        evidence_likelihood_true: P(evidence | event is true)
        evidence_likelihood_false: P(evidence | event is false)

        Example: Prior P(team wins) = 0.6
                 Star player confirmed playing (evidence)
                 P(star plays | team wins) = 0.9
                 P(star plays | team loses) = 0.7
                 Updated P(team wins | star plays) = ?
        """
        numerator = evidence_likelihood_true * prior_prob
        denominator = (evidence_likelihood_true * prior_prob +
                      evidence_likelihood_false * (1 - prior_prob))

        posterior = numerator / denominator if denominator > 0 else prior_prob
        return round(posterior, 4)

    def regression_adjustment(self, observed_value, population_mean,
                               sample_size, regression_weight=50):
        """
        Regress an observed stat toward the population mean.
        Used for noisy stats like 3PT%, save%, etc.

        observed_value: Recent observed stat
        population_mean: Long-term average
        sample_size: Number of observations in recent sample
        regression_weight: How strongly to regress (higher = more regression)
        """
        adjusted = ((sample_size * observed_value +
                     regression_weight * population_mean) /
                    (sample_size + regression_weight))
        return round(adjusted, 4)

    def project_nba_total(self, team_a_pace, team_b_pace,
                          team_a_off_eff, team_a_def_eff,
                          team_b_off_eff, team_b_def_eff,
                          league_avg_eff=115.0,
                          playoff=True):
        """
        Project NBA game total using the multiplicative pace-efficiency model.

        Pace = possessions per 48 minutes (per team)
        Off/Def efficiency = points per 100 possessions

        The correct formula is multiplicative, not additive:
          score_A = (off_A / league_avg) * (def_B / league_avg) * league_avg * pace/100

        This captures the interaction between offenses and defenses properly.
        Additive averaging overstates totals by ~10-15 pts.

        playoff: apply a ~6% downward adjustment. Playoff games are slower
        and more defensive than regular season — teams average ~13 pts fewer
        combined than regular season stats would project.
        """
        projected_pace = (team_a_pace + team_b_pace) / 2

        # Use four-factor adjusted ratings when available (adj_ortg/adj_drtg),
        # otherwise fall back to raw off_rating/def_rating.
        # The adjusted ratings remove shooting luck and incorporate home/away splits.
        eff_a_off = team_a_off_eff
        eff_a_def = team_a_def_eff
        eff_b_off = team_b_off_eff
        eff_b_def = team_b_def_eff

        # Multiplicative adjustment: how much better/worse than league average
        team_a_scoring = (eff_a_off / league_avg_eff) * \
                         (eff_b_def / league_avg_eff) * \
                         league_avg_eff * (projected_pace / 100)

        team_b_scoring = (eff_b_off / league_avg_eff) * \
                         (eff_a_def / league_avg_eff) * \
                         league_avg_eff * (projected_pace / 100)

        projected_total = team_a_scoring + team_b_scoring

        # Playoff regression: ~6% reduction in scoring vs regular season
        # Empirically, playoff totals run 12-15 pts below reg-season projection
        if playoff:
            projected_total *= 0.94
            team_a_scoring  *= 0.94
            team_b_scoring  *= 0.94

        return {
            "projected_total":  round(projected_total, 1),
            "team_a_projected": round(team_a_scoring, 1),
            "team_b_projected": round(team_b_scoring, 1),
            "projected_pace":   round(projected_pace, 1),
            "playoff_adjusted": playoff,
        }

    def project_nba_win_prob(self, away_net_rating, home_net_rating,
                              home_court_advantage=2.5):
        """
        Estimate win probability from season net ratings.

        Calibrated so that a 10-point net rating gap ≈ 85% win probability,
        matching historical NBA data and typical playoff spreads.

        home_court_advantage: playoff home court is worth ~2.5 pts
        (regular season is ~3.0, playoffs slightly less due to road warriors)
        """
        # Adjust for home court — home team gets the advantage
        adjusted_diff = away_net_rating - home_net_rating - home_court_advantage

        # Constant of 12 calibrated for NBA net ratings:
        # diff=10 → ~85%, diff=5 → ~70%, diff=0 → ~40% (road team slight dog)
        away_win_prob = 1 / (1 + 10 ** (-adjusted_diff / 12))
        return round(away_win_prob, 4)


class EdgeScanner:
    """Scan multiple markets for trading opportunities."""

    def __init__(self, fair_value_engine, min_edge=0.03):
        self.fv = fair_value_engine
        self.min_edge = min_edge

    def scan_markets(self, markets, model_probs):
        """
        Compare model probabilities against market prices for a list of markets.

        markets: List of Kalshi market objects
        model_probs: Dict of {ticker: model_probability}

        Returns: DataFrame of opportunities sorted by edge
        """
        opportunities = []

        for market in markets:
            ticker = market["ticker"]
            if ticker not in model_probs:
                continue

            market_prob = market.get("yes_bid", 50) / 100  # Use bid as conservative
            model_prob = model_probs[ticker]

            edge_info = self.fv.calculate_edge(model_prob, market_prob)

            if abs(edge_info["edge"]) >= self.min_edge:
                opportunities.append({
                    "ticker": ticker,
                    "title": market.get("title", ""),
                    "market_price": market_prob,
                    "model_price": model_prob,
                    "edge": edge_info["edge"],
                    "signal": edge_info["signal"],
                    "confidence": edge_info["confidence"],
                    "kelly": edge_info["kelly_fraction"],
                    "volume": market.get("volume", 0),
                })

        df = pd.DataFrame(opportunities)
        if not df.empty:
            df = df.sort_values("edge", key=abs, ascending=False)
        return df


def demo_fair_value():
    """Demonstrate fair value estimation."""
    fv = FairValueEngine()

    print("=" * 60)
    print("FAIR VALUE ENGINE DEMO")
    print("=" * 60)

    # NBA Total projection
    print("\n--- NBA Total Projection ---")
    result = fv.project_nba_total(
        team_a_pace=102, team_b_pace=98,
        team_a_off_eff=115, team_a_def_eff=108,
        team_b_off_eff=110, team_b_def_eff=112,
    )
    print(f"  Projected Total: {result['projected_total']}")
    print(f"  Team A: {result['team_a_projected']} | Team B: {result['team_b_projected']}")

    # Compare against market
    market_line = 220
    prob_over = fv.estimate_total_probability(result["projected_total"], market_line)
    print(f"\n  Market Line: {market_line}")
    print(f"  P(Over {market_line}): {prob_over:.1%}")

    edge = fv.calculate_edge(prob_over, 0.52)  # Market implies 52%
    print(f"  Edge vs Market: {edge['edge']:+.1%}")
    print(f"  Signal: {edge['signal']}")

    # Bayesian update example
    print("\n--- Bayesian Update ---")
    prior = 0.55  # P(team covers)
    updated = fv.bayesian_update(prior, 0.80, 0.50)
    print(f"  Prior P(cover): {prior:.1%}")
    print(f"  Sharp action on the cover side detected")
    print(f"  Updated P(cover): {updated:.1%}")

    # Regression example
    print("\n--- Regression Adjustment ---")
    raw_3pt = 0.42  # Team shooting 42% from 3 over last 10 games
    regressed = fv.regression_adjustment(0.42, 0.36, sample_size=10, regression_weight=50)
    print(f"  Raw 3PT% (10 games): {raw_3pt:.1%}")
    print(f"  League Average: 36.0%")
    print(f"  Regressed 3PT%: {regressed:.1%}")


if __name__ == "__main__":
    demo_fair_value()
