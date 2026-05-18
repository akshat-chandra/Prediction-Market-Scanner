"""
Cross-Platform Arb Scanner: Kalshi vs Polymarket
Finds matching sports events on both platforms and flags price discrepancies.

The core challenge is that there's no shared ticker between platforms.
A Kalshi market "NBA: Thunder to win Finals? YES" and a Polymarket market
"Will the OKC Thunder win the 2026 NBA Finals?" are the same contract
but named differently. We match them with a keyword overlap score.

Why this matters for DRW: detecting these gaps before they close is exactly
what a cross-platform arb desk would do in real-time. The framework here
is the research layer — execution speed (latency, capital, simultaneous fills)
is the operations layer you'd build on top.
"""
import re
from datetime import datetime


# ==================== EVENT MATCHING ====================

# Stop words that add noise to keyword matching
_STOP_WORDS = {
    "will", "the", "a", "an", "to", "win", "2026", "2025", "nba", "nhl", "mlb",
    "nfl", "in", "of", "for", "be", "is", "are", "who", "which", "finals",
    "championship", "cup", "series", "super", "bowl", "world", "stanley",
    "game", "winner", "at", "vs", "winner",
}

# Team name aliases: maps any variant to a canonical token set.
# Kalshi uses city names ("Oklahoma City", "Los Angeles L"), Polymarket
# uses nicknames ("Thunder", "Lakers"). We expand both sides before matching.
_TEAM_ALIASES = {
    # NBA
    "oklahoma city": {"thunder", "okc", "oklahoma"},
    "thunder":        {"oklahoma", "city", "okc"},
    "los angeles l":  {"lakers", "lal", "angeles"},
    "los angeles c":  {"clippers", "lac", "angeles"},
    "lakers":         {"los", "angeles", "lal"},
    "golden state":   {"warriors", "gsw"},
    "warriors":       {"golden", "state", "gsw"},
    "new york":       {"knicks", "nyk"},
    "knicks":         {"new", "york", "nyk"},
    "san antonio":    {"spurs", "sas"},
    "spurs":          {"san", "antonio", "sas"},
    "minnesota":      {"timberwolves", "wolves", "min"},
    "timberwolves":   {"minnesota", "wolves"},
    "philadelphia":   {"sixers", "76ers", "phi"},
    "76ers":          {"philadelphia", "phi"},
    "cleveland":      {"cavaliers", "cavs", "cle"},
    "cavaliers":      {"cleveland", "cavs"},
    "detroit":        {"pistons", "det"},
    "pistons":        {"detroit"},
    "boston":         {"celtics", "bos"},
    "celtics":        {"boston"},
    "miami":          {"heat", "mia"},
    "indiana":        {"pacers", "ind"},
    # NHL
    "carolina":       {"hurricanes", "canes", "car"},
    "hurricanes":     {"carolina", "canes"},
    "philadelphia f": {"flyers", "phi"},
    "flyers":         {"philadelphia"},
    "colorado":       {"avalanche", "avs", "col"},
    "avalanche":      {"colorado", "avs"},
    "minnesota w":    {"wild", "min"},
    "wild":           {"minnesota"},
    "vegas":          {"golden knights", "vgk"},
    "golden knights": {"vegas", "vgk"},
    "anaheim":        {"ducks", "ana"},
    "ducks":          {"anaheim"},
    "montreal":       {"canadiens", "habs", "mtl"},
    "canadiens":      {"montreal", "habs"},
    "buffalo":        {"sabres", "buf"},
    "sabres":         {"buffalo"},
    # MLB
    "tampa bay":      {"rays", "tb"},
    "rays":           {"tampa", "bay"},
    "kansas city":    {"royals", "kc"},
    "royals":         {"kansas", "city"},
    "chicago c":      {"cubs", "chc"},
    "cubs":           {"chicago"},
    "chicago w":      {"white sox", "cws"},
    "new york y":     {"yankees", "nyy"},
    "yankees":        {"new", "york"},
    "new york m":     {"mets", "nym"},
    "mets":           {"new", "york"},
    "los angeles d":  {"dodgers", "lad"},
    "dodgers":        {"los", "angeles"},
}


def _expand_with_aliases(tokens):
    """Add alias tokens so city names match nicknames and vice versa."""
    expanded = set(tokens)
    text = " ".join(tokens)
    for phrase, aliases in _TEAM_ALIASES.items():
        if phrase in text:
            expanded |= aliases
    return expanded


def _keywords(text):
    """
    Extract meaningful keywords from a market question for fuzzy matching.
    Lowercases, strips punctuation, removes stop words, expands team aliases.
    """
    tokens = re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
    base = {t for t in tokens if t not in _STOP_WORDS and len(t) > 2}
    return _expand_with_aliases(base)


def match_score(question_a, question_b):
    """
    Compute keyword overlap between two market questions (0.0–1.0).

    Uses Jaccard similarity on keyword sets: |intersection| / |union|.
    A score above ~0.25 is usually a genuine match for sports markets
    where team names are the dominant signal.

    Example:
      "Will the Oklahoma City Thunder win the 2026 NBA Finals?"
      "NBA Finals: Thunder win? YES"
      → shared: {oklahoma, city, thunder}  → score ≈ 0.38  → match
    """
    kw_a = _keywords(question_a)
    kw_b = _keywords(question_b)
    if not kw_a or not kw_b:
        return 0.0
    intersection = kw_a & kw_b
    union = kw_a | kw_b
    return len(intersection) / len(union)


def find_matches(kalshi_markets, poly_markets, min_score=0.25):
    """
    For each Kalshi market, find the best-matching Polymarket market.

    Returns a list of match dicts sorted by score descending.
    Filters out pairs below min_score to suppress false positives.

    kalshi_markets: list of dicts from KalshiClient (have 'title' and 'ticker')
    poly_markets:   list of dicts from PolymarketClient (have 'question' and 'id')
    """
    matches = []

    for km in kalshi_markets:
        kalshi_text = km.get("title", "") + " " + km.get("subtitle", "")
        best_score = 0.0
        best_poly = None

        for pm in poly_markets:
            score = match_score(kalshi_text, pm["question"])
            if score > best_score:
                best_score = score
                best_poly = pm

        if best_score >= min_score and best_poly is not None:
            matches.append({
                "kalshi_ticker":   km.get("ticker"),
                "kalshi_question": kalshi_text.strip(),
                "poly_id":         best_poly.get("market_id") or best_poly.get("id") or best_poly.get("condition_id"),
                "poly_question":   best_poly["question"],
                "match_score":     round(best_score, 3),
            })

    matches.sort(key=lambda m: m["match_score"], reverse=True)
    return matches


def deduplicate_matches(matches, kalshi_by_ticker, poly_by_id):
    """
    For each Polymarket market, keep only the single best Kalshi match.

    Kalshi has two YES markets per game (one per team). Both match the same
    Polymarket contract with the same keyword score, producing false gaps.
    e.g. Kalshi-OKC (86c) and Kalshi-LAL (14c) both match Polymarket
    "Lakers vs Thunder" — the 72c difference is meaningless.

    Fix: among all Kalshi candidates for a given Polymarket market, keep the
    one whose price is CLOSEST to the Polymarket price. That's always the
    same-team comparison.
    """
    from collections import defaultdict

    # Build a price lookup for Polymarket markets
    poly_price = {}
    for m in poly_by_id.values():
        pid = m.get("market_id") or m.get("id") or m.get("condition_id")
        if pid:
            p = m.get("yes_price") or m.get("mid")
            if p is not None:
                poly_price[pid] = float(p)

    # Group all Kalshi candidates per Polymarket market
    by_poly = defaultdict(list)
    for match in matches:
        by_poly[match["poly_id"]].append(match)

    deduped = []
    for poly_id, group in by_poly.items():
        if len(group) == 1:
            deduped.append(group[0])
            continue

        pp = poly_price.get(poly_id)
        if pp is None:
            deduped.append(group[0])  # no price to compare, keep best score
            continue

        # Keep the Kalshi market whose mid price is closest to Polymarket's
        def price_distance(match):
            km = kalshi_by_ticker.get(match["kalshi_ticker"], {})
            bid = km.get("yes_bid")
            ask = km.get("yes_ask")
            if bid is not None and ask is not None:
                kalshi_mid = (bid + ask) / 200   # avg, then /100 for prob
            elif bid is not None:
                kalshi_mid = bid / 100
            else:
                return 1.0
            return abs(kalshi_mid - pp)

        best = min(group, key=price_distance)
        deduped.append(best)

    deduped.sort(key=lambda m: m["match_score"], reverse=True)
    return deduped


# ==================== PRICE COMPARISON ====================

def compare_prices(kalshi_market, poly_market):
    """
    Compare YES prices between a matched Kalshi and Polymarket contract.

    Normalizes both to probability space (0.0–1.0) and computes:
      - Raw price gap
      - Which platform is cheaper for YES
      - Whether a pure arb exists (gap net of vig > 0)

    Kalshi prices: yes_bid / yes_ask in cents (1–99) → divide by 100
    Polymarket prices: already in 0.0–1.0 from outcomePrices
    """
    # Kalshi: use midpoint of yes_bid and yes_ask if both present; else whichever exists
    k_bid = kalshi_market.get("yes_bid")
    k_ask = kalshi_market.get("yes_ask")
    if k_bid is not None and k_ask is not None:
        kalshi_mid = (k_bid + k_ask) / 200   # average, then /100 for prob
        kalshi_spread = (k_ask - k_bid) / 100
    elif k_bid is not None:
        kalshi_mid = k_bid / 100
        kalshi_spread = None
    elif k_ask is not None:
        kalshi_mid = k_ask / 100
        kalshi_spread = None
    else:
        return None

    poly_mid = poly_market.get("yes_price")
    if poly_mid is None:
        return None

    # Raw price gap: positive = Kalshi is CHEAPER than Polymarket (buy on Kalshi)
    gap = poly_mid - kalshi_mid

    # Effective arb: to lock in risk-free profit you'd need to buy YES on the
    # cheap platform AND simultaneously buy NO on the expensive platform.
    # Cost of YES-Kalshi + Cost of NO-Poly = kalshi_mid + (1 - poly_mid)
    # If this < 1.0, you profit regardless of outcome.
    # (Note: ignores trading fees and execution risk — real arb requires both
    #  legs to fill before prices move.)
    cost_yes_kalshi_no_poly = kalshi_mid + (1 - poly_mid)
    cost_no_kalshi_yes_poly = (1 - kalshi_mid) + poly_mid

    pure_arb = cost_yes_kalshi_no_poly < 1.0 or cost_no_kalshi_yes_poly < 1.0

    if cost_yes_kalshi_no_poly < cost_no_kalshi_yes_poly:
        arb_profit = round(1 - cost_yes_kalshi_no_poly, 4)
        arb_action = "BUY YES Kalshi + BUY NO Polymarket"
    else:
        arb_profit = round(1 - cost_no_kalshi_yes_poly, 4)
        arb_action = "BUY NO Kalshi + BUY YES Polymarket"

    return {
        "kalshi_mid":     round(kalshi_mid, 4),
        "kalshi_spread":  round(kalshi_spread, 4) if kalshi_spread else None,
        "poly_mid":       round(poly_mid, 4),
        "gap":            round(gap, 4),        # + = Kalshi cheaper on YES
        "abs_gap":        round(abs(gap), 4),
        "cheaper_for_yes": "Kalshi" if gap > 0 else "Polymarket",
        "pure_arb":       pure_arb,
        "arb_profit":     arb_profit if pure_arb else 0,
        "arb_action":     arb_action if pure_arb else "no arb",
    }


# ==================== FULL SCANNER ====================

class CrossPlatformScanner:
    """
    Ties together event matching and price comparison into a single scan.

    Usage:
        scanner = CrossPlatformScanner(kalshi_client, poly_client)
        results = scanner.scan(min_edge=0.02)
    """

    def __init__(self, kalshi_client, poly_client, match_threshold=0.25):
        self.kalshi = kalshi_client
        self.poly   = poly_client
        self.match_threshold = match_threshold

    def scan(self, min_edge=0.02, sport=None, verbose=True):
        """
        Full pipeline: fetch both platforms, match events, compare prices.

        min_edge: Minimum price gap to include in results (filters noise)
        sport:    Optional keyword to limit Kalshi fetch (e.g. 'nba', 'nhl')
        verbose:  Print progress messages during long fetches

        Returns: list of opportunity dicts, sorted by abs_gap descending
        """
        if verbose:
            print("  Fetching Kalshi sports markets...")
        try:
            kalshi_markets = self.kalshi.get_sports_markets(sport=sport, limit=200)
        except Exception as e:
            print(f"  Kalshi fetch failed: {e}")
            kalshi_markets = []

        if verbose:
            print(f"  Found {len(kalshi_markets)} Kalshi markets.")
            print("  Fetching Polymarket sports markets...")

        try:
            poly_markets = self.poly.get_sports_markets(limit=500)
        except Exception as e:
            print(f"  Polymarket fetch failed: {e}")
            poly_markets = []

        if verbose:
            print(f"  Found {len(poly_markets)} Polymarket markets.")

        if not kalshi_markets or not poly_markets:
            print("  Cannot compare — one or both platforms returned no markets.")
            return []

        # Match events across platforms
        matches = find_matches(kalshi_markets, poly_markets,
                               min_score=self.match_threshold)

        if verbose:
            print(f"  Matched {len(matches)} event pairs (score ≥ {self.match_threshold}).")

        # Build lookup dicts for quick access
        kalshi_by_ticker = {m["ticker"]: m for m in kalshi_markets}
        poly_by_id = {
            m.get("market_id") or m.get("id") or m.get("condition_id"): m
            for m in poly_markets
        }

        # Compare prices for each matched pair
        opportunities = []
        for match in matches:
            km = kalshi_by_ticker.get(match["kalshi_ticker"])
            pm = poly_by_id.get(match["poly_id"])
            if not km or not pm:
                continue

            price_cmp = compare_prices(km, pm)
            if price_cmp is None:
                continue

            if price_cmp["abs_gap"] < min_edge and not price_cmp["pure_arb"]:
                continue

            opportunities.append({
                **match,
                **price_cmp,
                "kalshi_vol":  km.get("volume", 0),
                "poly_vol":    pm.get("volume", 0),
                "scanned_at":  datetime.now().isoformat(timespec="seconds"),
            })

        opportunities.sort(key=lambda o: o["abs_gap"], reverse=True)
        return opportunities

    @staticmethod
    def print_results(opportunities):
        """Pretty-print scan results."""
        if not opportunities:
            print("\n  No cross-platform opportunities found above threshold.")
            return

        print(f"\n  {'='*58}")
        print(f"  Cross-Platform Scan: {len(opportunities)} opportunities")
        print(f"  {'='*58}\n")

        for opp in opportunities:
            arb_tag = " *** PURE ARB ***" if opp["pure_arb"] else ""
            print(f"  [{opp['match_score']:.2f} match]{arb_tag}")
            print(f"  Kalshi:      {opp['kalshi_question'][:60]}")
            print(f"  Polymarket:  {opp['poly_question'][:60]}")
            print(f"  Kalshi mid:  {opp['kalshi_mid']:.1%}  |  "
                  f"Poly mid: {opp['poly_mid']:.1%}  |  "
                  f"Gap: {opp['gap']:+.1%}")
            print(f"  Cheaper YES: {opp['cheaper_for_yes']}")
            if opp["pure_arb"]:
                print(f"  ARB PROFIT:  {opp['arb_profit']:.2%} per $1  |  "
                      f"Action: {opp['arb_action']}")
            print(f"  Volumes:     Kalshi ${opp['kalshi_vol']:,.0f}  |  "
                  f"Poly ${opp['poly_vol']:,.0f}")
            print()


# ==================== Standalone demo (no live Kalshi auth needed) ====================
if __name__ == "__main__":
    """
    Demonstrate the matching and price comparison logic using mocked data.
    The full live scan requires Kalshi auth; this shows the mechanics work.
    """
    from polymarket_client import PolymarketClient

    print("=" * 60)
    print("CROSS-PLATFORM ARB DEMO (Polymarket live + mock Kalshi)")
    print("=" * 60)

    poly = PolymarketClient()

    print("\nFetching live Polymarket sports markets...")
    poly_markets = poly.get_sports_markets(limit=300)
    poly_markets.sort(key=lambda m: m["volume"], reverse=True)
    print(f"Found {len(poly_markets)} sports markets on Polymarket.\n")

    # Mock Kalshi markets with deliberately mismatched prices to demo arb detection
    # In production these would come from KalshiClient.get_sports_markets()
    mock_kalshi = []
    for pm in poly_markets[:10]:
        if pm["yes_price"] is None:
            continue
        # Simulate Kalshi trading at a slight discount to Polymarket
        import random
        random.seed(hash(pm["question"]))
        offset = random.uniform(-0.04, 0.04)
        yes_mid = max(0.02, min(0.98, pm["yes_price"] + offset))
        half_spread = 0.01
        mock_kalshi.append({
            "ticker":   f"MOCK-{pm['id']}",
            "title":    pm["question"],
            "subtitle": "",
            "yes_bid":  int((yes_mid - half_spread) * 100),
            "yes_ask":  int((yes_mid + half_spread) * 100),
            "volume":   pm["volume"] * 0.3,  # Kalshi typically smaller
        })

    # Run matching and price comparison
    matches = find_matches(mock_kalshi, poly_markets, min_score=0.30)
    print(f"Matched {len(matches)} event pairs.\n")

    kalshi_by_ticker = {m["ticker"]: m for m in mock_kalshi}
    poly_by_id       = {m["id"]: m for m in poly_markets}

    opportunities = []
    for match in matches:
        km = kalshi_by_ticker.get(match["kalshi_ticker"])
        pm = poly_by_id.get(match["poly_id"])
        if not km or not pm:
            continue
        cmp = compare_prices(km, pm)
        if cmp:
            opportunities.append({**match, **cmp,
                                   "kalshi_vol": km.get("volume", 0),
                                   "poly_vol":   pm.get("volume", 0)})

    opportunities.sort(key=lambda o: o["abs_gap"], reverse=True)
    CrossPlatformScanner.print_results(opportunities[:8])
