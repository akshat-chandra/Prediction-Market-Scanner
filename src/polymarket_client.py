"""
Polymarket API Client
Read-only access to Polymarket game markets, prices, and order books.
No account or authentication required.

The correct API for game-level markets is the EVENTS endpoint, not markets.
Each event ("76ers vs. Knicks") contains embedded markets for moneyline,
totals (O/U), spreads, and player props.

Key fields available inline on every market (no separate CLOB call needed):
  bestBid, bestAsk, lastTradePrice, spread, outcomePrices, acceptingOrders

Supported sports and their tag slugs:
  nba      → NBA game events (playoffs, regular season)
  nhl      → NHL game events
  baseball → MLB game events
  soccer   → Soccer game events (EPL, Champions League, etc.)
"""
import json
import re
import time
import requests


# Tag slugs for each sport on Polymarket's events endpoint
SPORT_SLUGS = {
    "nba":      "nba",
    "nhl":      "nhl",
    "mlb":      "baseball",
    "baseball": "baseball",
    "soccer":   "soccer",
    "nfl":      "football",
}


def _parse_prices(market):
    """Extract YES price, bid, ask from a raw market dict."""
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        yes_price = float(prices[0]) if prices else None
    except (json.JSONDecodeError, ValueError, IndexError):
        yes_price = None

    best_bid = market.get("bestBid")
    best_ask = market.get("bestAsk")
    if best_bid is not None:
        best_bid = float(best_bid)
    if best_ask is not None:
        best_ask = float(best_ask)

    mid = None
    if best_bid is not None and best_ask is not None:
        mid = round((best_bid + best_ask) / 2, 4)
    elif yes_price is not None:
        mid = yes_price  # fall back to gamma price

    return {
        "yes_price": yes_price,
        "best_bid":  best_bid,
        "best_ask":  best_ask,
        "mid":       mid,
        "spread":    round(best_ask - best_bid, 4) if (best_bid and best_ask) else None,
        "last_trade": market.get("lastTradePrice"),
    }


def _market_type(question, event_title):
    """
    Classify a market by its question text.

    moneyline: question is (approximately) just the team matchup
    total:     question contains "O/U"
    spread:    question starts with "Spread:"
    prop:      everything else (player stats, first-inning runs, etc.)
    """
    q = question.strip()
    if q.startswith("Spread:"):
        return "spread"
    if "O/U" in q or "o/u" in q:
        return "total"
    # Moneyline: question closely matches the event title (team vs team)
    # Allow for slight differences like "Knicks vs. 76ers" vs "76ers vs. Knicks"
    if re.sub(r"[^a-z0-9]", "", q.lower()) == re.sub(r"[^a-z0-9]", "", event_title.lower()):
        return "moneyline"
    # Also catch moneylines where question IS just "Team A vs. Team B"
    if re.search(r"\bvs\.?\b", q, re.IGNORECASE) and "O/U" not in q and "Spread" not in q:
        return "moneyline"
    return "prop"


def _parse_tokens(market):
    """Extract YES and NO token IDs for CLOB order book lookup."""
    try:
        tokens = json.loads(market.get("clobTokenIds", "[]"))
        return tokens[0] if tokens else None, tokens[1] if len(tokens) > 1 else None
    except (json.JSONDecodeError, IndexError):
        return None, None


class PolymarketClient:
    """Read-only client for Polymarket game and futures markets."""

    GAMMA_URL = "https://gamma-api.polymarket.com"
    CLOB_URL  = "https://clob.polymarket.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-sports-trader/1.0"})
        self._last_request = 0.0
        self._min_interval = 0.15  # ~6 req/sec

    def _get(self, base_url, path, params=None):
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()
        resp = self.session.get(f"{base_url}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ==================== EVENTS (game-level) ====================

    def get_events(self, sport, limit=100):
        """
        Fetch all active events for a sport from the events endpoint.
        Returns raw event dicts including embedded markets.

        sport: 'nba', 'nhl', 'mlb', 'baseball', 'soccer', 'nfl'
        """
        slug = SPORT_SLUGS.get(sport.lower(), sport.lower())
        params = {"tag_slug": slug, "active": "true", "closed": "false", "limit": limit}
        data = self._get(self.GAMMA_URL, "/events", params=params)
        return data if isinstance(data, list) else []

    def get_game_events(self, sport, min_markets=1, status=None):
        """
        Return only game-level events (e.g. "76ers vs. Knicks"), not season futures.

        status filter:
          None        → all games (upcoming, live, settled)
          "upcoming"  → game hasn't started yet (moneyline not near 0 or 1)
          "live"      → moneyline still two-sided and start_date is past
          "open"      → upcoming + live combined (anything tradeable)

        Game events are identified by "vs" in the title and having at least
        min_markets embedded markets (filters out thin single-contract events).

        Hard exclusions applied before any status logic:
          - event.closed == True  (Polymarket has resolved it)
          - event.active == False (Polymarket has deactivated it)
        """
        from datetime import datetime, timezone
        raw_events = self.get_events(sport)
        now = datetime.now(timezone.utc)

        games = []
        for event in raw_events:
            title   = event.get("title", "")
            markets = event.get("markets", [])

            if " vs" not in title.lower():
                continue
            if len(markets) < min_markets:
                continue

            # Hard exclusion: event.closed is the only reliable field.
            # event.active is always True even for resolved events — confirmed
            # by inspection of closed Polymarket events (active=True, closed=True
            # coexist). The query param closed=false filters most cases; this
            # catches any that slip through due to API resolution lag.
            if event.get("closed"):
                continue

            game = self._structure_game(event)

            # Determine game status from moneyline price
            ml = game["moneyline"]
            if ml and ml["best_bid"] is not None and ml["best_ask"] is not None:
                bid = ml["best_bid"]
                if bid > 0.95 or bid < 0.05:
                    game["status"] = "settled"
                else:
                    # Use gameStartTime (actual tip-off) not startDate (market creation)
                    gst = game.get("game_start_time")
                    if gst:
                        try:
                            from datetime import timezone as _tz
                            # Normalize: "2026-05-06 00:30:00+00" → "2026-05-06T00:30:00+00:00"
                            gst_str = (str(gst).strip()
                                       .replace(" ", "T")
                                       .replace("+00:00:00", "+00:00")
                                       .replace("+00", "+00:00"))
                            if not any(c in gst_str for c in ("+", "-", "Z")):
                                gst_str += "+00:00"
                            tip_off = datetime.fromisoformat(gst_str)
                            game["status"] = "upcoming" if tip_off > now else "live"
                        except (ValueError, TypeError):
                            game["status"] = "live"
                    else:
                        game["status"] = "live"
            else:
                game["status"] = "settled"

            if status:
                if status == "open" and game["status"] == "settled":
                    continue
                elif status not in ("open",) and game["status"] != status:
                    continue

            games.append(game)

        games.sort(key=lambda g: g["volume"], reverse=True)

        # Deduplicate: keep only the highest-volume event per unique matchup.
        # Polymarket often creates multiple events for the same game (duplicates
        # with thin liquidity). Normalise the title to a frozenset of team names
        # so "Hurricanes vs. Flyers" and "Flyers vs. Hurricanes" collapse to one.
        seen = {}
        deduped = []
        for g in games:
            import re as _re
            teams = frozenset(
                t.strip().lower()
                for t in _re.split(r"\s+vs\.?\s+", g["title"], flags=_re.IGNORECASE)
            )
            if teams not in seen:
                seen[teams] = True
                deduped.append(g)

        return deduped

    def _structure_game(self, event):
        """
        Turn a raw event dict into a clean game object with categorized markets.

        Returns:
          event_id, title, home_team, away_team, volume, start_date,
          moneyline: single market dict or None,
          totals:    list of total (O/U) market dicts,
          spreads:   list of spread market dicts,
          props:     list of player prop market dicts (usually illiquid),
        """
        title = event.get("title", "")

        # Extract team names from "Away vs. Home" or "Away vs Home" pattern
        parts = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.IGNORECASE)
        away_team = parts[0].strip() if len(parts) > 0 else ""
        home_team = parts[1].strip() if len(parts) > 1 else ""

        moneyline = None
        totals    = []
        spreads   = []
        props     = []

        # gameStartTime is on the individual market objects, not the event.
        # Grab it from the first market that has it — same for all markets in a game.
        game_start_time = None
        for m in event.get("markets", []):
            raw_gst = m.get("gameStartTime")
            if raw_gst:
                game_start_time = raw_gst
                break

        for m in event.get("markets", []):
            # Skip closed or non-accepting markets.
            # acceptingOrders=False is set reliably when a market resolves.
            # market.closed=True is an additional check for markets in resolution
            # lag where acceptingOrders may still be True but trading has stopped.
            if not m.get("acceptingOrders", True) or m.get("closed", False):
                continue

            q    = m.get("question", "")
            mtype = _market_type(q, title)
            prices = _parse_prices(m)
            yes_token, no_token = _parse_tokens(m)

            entry = {
                "market_id":   m.get("id"),
                "question":    q,
                "market_type": mtype,
                "condition_id": m.get("conditionId"),
                "yes_token":   yes_token,
                "no_token":    no_token,
                "volume":      float(m.get("volume", 0) or 0),
                **prices,
            }

            if mtype == "moneyline":
                # Keep the highest-volume moneyline if there are duplicates
                if moneyline is None or entry["volume"] > moneyline["volume"]:
                    moneyline = entry
            elif mtype == "total":
                # Only include totals with active two-sided markets (not settled at ~100%)
                if prices["yes_price"] is not None and 0.02 < prices["yes_price"] < 0.98:
                    totals.append(entry)
            elif mtype == "spread":
                spreads.append(entry)
            else:
                props.append(entry)

        # Sort totals by line value extracted from question, e.g. O/U 213.5
        def _ou_line(q):
            m = re.search(r"O/U\s+([\d.]+)", q)
            return float(m.group(1)) if m else 0

        totals.sort(key=lambda t: _ou_line(t["question"]))

        return {
            "event_id":        event.get("id"),
            "title":           title,
            "away_team":       away_team,
            "home_team":       home_team,
            "volume":          float(event.get("volume", 0) or 0),
            "liquidity":       float(event.get("liquidity", 0) or 0),
            "start_date":      event.get("startDate"),
            "end_date":        event.get("endDate"),        # used for 24h staleness filter
            "game_start_time": game_start_time,             # actual tip-off / first pitch
            "moneyline":       moneyline,
            "totals":          totals,
            "spreads":         spreads,
            "props":           props,
        }

    # ==================== SEASON FUTURES ====================

    def get_futures_markets(self, sport):
        """
        Return season-long futures (champion, conference winner, MVP, etc.)
        These are the non-game events — no "vs" in the title.
        """
        raw_events = self.get_events(sport, limit=100)
        futures = []
        for event in raw_events:
            title = event.get("title", "")
            if " vs" in title.lower():
                continue
            for m in event.get("markets", []):
                if not m.get("acceptingOrders", True):
                    continue
                try:
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    yes_price = float(prices[0]) if prices else None
                except Exception:
                    yes_price = None
                futures.append({
                    "event_title": title,
                    "question":    m.get("question", ""),
                    "yes_price":   yes_price,
                    "best_bid":    m.get("bestBid"),
                    "best_ask":    m.get("bestAsk"),
                    "volume":      float(m.get("volume", 0) or 0),
                    "condition_id": m.get("conditionId"),
                })
        futures.sort(key=lambda m: m["volume"], reverse=True)
        return futures

    # ==================== ORDER BOOKS (CLOB) ====================

    def get_orderbook(self, token_id):
        """Fetch live CLOB order book for a token. Prices are 0.0–1.0."""
        try:
            data = self._get(self.CLOB_URL, "/book", params={"token_id": token_id})
        except requests.HTTPError as e:
            return {"bids": [], "asks": [], "error": str(e)}
        if "error" in data:
            return {"bids": [], "asks": [], "error": data["error"]}
        return {
            "token_id": token_id,
            "bids": [{"price": float(b["price"]), "size": float(b["size"])}
                     for b in data.get("bids", [])],
            "asks": [{"price": float(a["price"]), "size": float(a["size"])}
                     for a in data.get("asks", [])],
        }

    def get_market_mid(self, token_id):
        """Get best bid, best ask, and midpoint for a token from the live CLOB."""
        ob = self.get_orderbook(token_id)
        if ob.get("error"):
            return None
        bids = ob["bids"]
        asks = ob["asks"]
        best_bid = max((b["price"] for b in bids), default=None)
        best_ask = min((a["price"] for a in asks), default=None)
        if best_bid is None and best_ask is None:
            return None
        mid = round((best_bid + best_ask) / 2, 4) if (best_bid and best_ask) else (best_bid or best_ask)
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid":      mid,
            "spread":   round(best_ask - best_bid, 4) if (best_bid and best_ask) else None,
        }

    # ==================== LEGACY (kept for cross_platform.py compatibility) ====================

    def get_sports_markets(self, sport=None, limit=500):
        """
        Flat list of active markets across game events and futures.
        Used by cross_platform.py for event matching.

        Returns dicts with: question, yes_price, best_bid, best_ask, volume, yes_token.
        """
        sports = [sport] if sport else ["nba", "nhl", "mlb", "soccer"]
        out = []
        for s in sports:
            for game in self.get_game_events(s):
                if game["moneyline"]:
                    game["moneyline"]["event_title"] = game["title"]
                    out.append(game["moneyline"])
                for t in game["totals"]:
                    t["event_title"] = game["title"]
                    out.append(t)
        return out


# ==================== Quick Test ====================
if __name__ == "__main__":
    client = PolymarketClient()

    print("=" * 65)
    print("POLYMARKET LIVE GAME MARKETS")
    print("=" * 65)

    for sport in ["nba", "nhl", "baseball"]:
        games = client.get_game_events(sport)
        if not games:
            print(f"\n{sport.upper()}: no active game markets right now")
            continue

        print(f"\n{'─'*65}")
        print(f"  {sport.upper()} — {len(games)} active game(s)")
        print(f"{'─'*65}")

        for game in games[:4]:
            ml  = game["moneyline"]
            vol = f"${game['volume']:,.0f}"
            print(f"\n  {game['title']}  (vol {vol})")

            if ml:
                bid = f"{ml['best_bid']:.3f}" if ml['best_bid'] else "  ?"
                ask = f"{ml['best_ask']:.3f}" if ml['best_ask'] else "  ?"
                print(f"    Moneyline:  {game['away_team']} YES  bid={bid}  ask={ask}")

            for t in game["totals"]:
                bid = f"{t['best_bid']:.3f}" if t['best_bid'] else " ?"
                ask = f"{t['best_ask']:.3f}" if t['best_ask'] else " ?"
                print(f"    Total:      {t['question'][:45]}  bid={bid} ask={ask}")

            for s in game["spreads"]:
                bid = f"{s['best_bid']:.3f}" if s['best_bid'] else " ?"
                ask = f"{s['best_ask']:.3f}" if s['best_ask'] else " ?"
                print(f"    Spread:     {s['question'][:45]}  bid={bid} ask={ask}")

            print(f"    Props:      {len(game['props'])} player prop markets available")
