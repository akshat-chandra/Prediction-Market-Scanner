"""
Kalshi API Client
Handles authentication, market data retrieval, order book access, and order management.
"""
import time
import base64
import requests
from datetime import datetime
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from config import KALSHI_BASE_URL, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH


class RateLimiter:
    """
    Simple token-bucket rate limiter.
    Kalshi allows ~10 requests/sec. We stay at 8 to leave headroom.
    """
    def __init__(self, max_calls=8, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = []

    def wait(self):
        now = time.time()
        # Drop timestamps older than one period
        self.calls = [t for t in self.calls if now - t < self.period]
        if len(self.calls) >= self.max_calls:
            sleep_time = self.period - (now - self.calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        self.calls.append(time.time())


class KalshiClient:
    """Wrapper for Kalshi REST API with RSA-PSS authentication."""

    def __init__(self, api_key_id=None, private_key_path=None):
        self.base_url = KALSHI_BASE_URL
        self.api_key_id = api_key_id or KALSHI_API_KEY_ID
        self.private_key_path = private_key_path or KALSHI_PRIVATE_KEY_PATH
        self.private_key = self._load_private_key() if self.private_key_path else None
        self.session = requests.Session()
        self.rate_limiter = RateLimiter()

    def _load_private_key(self):
        """Load RSA private key from PEM file. Handles both PKCS#8 and PKCS#1 formats."""
        try:
            with open(self.private_key_path, "rb") as f:
                data = f.read()
            # Try PKCS#8 first ("BEGIN PRIVATE KEY"), then PKCS#1 ("BEGIN RSA PRIVATE KEY")
            try:
                return serialization.load_pem_private_key(data, password=None)
            except Exception:
                from cryptography.hazmat.primitives.serialization import load_pem_private_key
                from cryptography.hazmat.backends import default_backend
                return load_pem_private_key(data, password=None, backend=default_backend())
        except FileNotFoundError:
            print(f"Warning: Private key not found at {self.private_key_path}")
            print("Unauthenticated mode: market data only, no trading.")
            return None

    def _sign_request(self, method, path):
        """
        Generate RSA-PSS signature for authenticated REST requests.

        Kalshi requires: timestamp (ms) + HTTP method + full path, signed with your private key.
        The signature is base64-encoded and sent in the KALSHI-ACCESS-SIGNATURE header.
        This prevents replay attacks — a captured request is only valid for its exact timestamp.
        """
        timestamp = str(int(time.time() * 1000))
        # Kalshi expects the full path including the /trade-api/v2 prefix
        full_path = f"/trade-api/v2{path}"
        message = f"{timestamp}{method}{full_path}"
        signature = self.private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    def ws_auth_headers(self, debug: bool = False) -> dict:
        """
        Generate RSA-PSS auth headers for the Kalshi WebSocket handshake.

        Identical signing logic to _sign_request — same PSS params, same base64
        encoding, same header names. The only structural difference is full_path:
          REST: /trade-api/v2/{endpoint}   (prefix added by _sign_request)
          WS:   /trade-api/ws/v2           (full path, no extra prefix)

        Signed message: timestamp_ms + "GET" + "/trade-api/ws/v2"

        debug=True prints the exact signed string and header values so you can
        verify what's going over the wire. Pass debug=True from the WS client.

        Returns empty dict if no private key is loaded.
        """
        if not self.private_key:
            return {}

        timestamp = str(int(time.time() * 1000))   # milliseconds, same as REST
        full_path = "/trade-api/ws/v2"
        message   = f"{timestamp}GET{full_path}"

        signature = self.private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode()

        if debug:
            print(f"  [Kalshi WS auth debug]")
            print(f"    Signed message : {message!r}")
            print(f"    Timestamp (ms) : {timestamp}  (digits={len(timestamp)}, expect 13)")
            print(f"    Key ID         : {self.api_key_id!r}")
            print(f"    Signature      : {sig_b64[:20]}...  (total len={len(sig_b64)})")
            print(f"    Header names   : KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP / KALSHI-ACCESS-SIGNATURE")

        return {
            "KALSHI-ACCESS-KEY":       self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }

    def _get(self, path, params=None, auth_required=False):
        """Make GET request, signing if a private key is available."""
        self.rate_limiter.wait()
        url = f"{self.base_url}{path}"
        # Always sign when we have a key — Kalshi now requires auth on all endpoints
        headers = self._sign_request("GET", path) if self.private_key else {}
        response = self.session.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def _post(self, path, data=None):
        """Make authenticated POST request."""
        if not self.private_key:
            raise ValueError("Authentication required. Set API key and private key.")
        self.rate_limiter.wait()
        url = f"{self.base_url}{path}"
        headers = self._sign_request("POST", path)
        response = self.session.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json()

    def _delete(self, path):
        """Make authenticated DELETE request."""
        if not self.private_key:
            raise ValueError("Authentication required.")
        self.rate_limiter.wait()
        url = f"{self.base_url}{path}"
        headers = self._sign_request("DELETE", path)
        response = self.session.delete(url, headers=headers)
        response.raise_for_status()
        return response.json()

    # ==================== MARKET DATA (Public) ====================

    def get_markets(self, status="open", limit=100, cursor=None, series_ticker=None):
        """Get all available markets with optional filters."""
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        return self._get("/markets", params=params)

    def get_market(self, ticker):
        """Get detailed data for a specific market by ticker."""
        return self._get(f"/markets/{ticker}")

    def get_sports_markets(self, sport=None, limit=200):
        """
        Get sports-related markets, optionally filtered by sport.
        Searches market titles and categories for sport keywords.
        """
        all_markets = []
        cursor = None

        while True:
            response = self.get_markets(status="open", limit=100, cursor=cursor)
            markets = response.get("markets", [])
            all_markets.extend(markets)

            cursor = response.get("cursor")
            if not cursor or len(all_markets) >= limit:
                break

        sports_keywords = ["nba", "nfl", "nhl", "mlb", "ncaa", "soccer", "tennis",
                          "football", "basketball", "baseball", "hockey",
                          "touchdown", "points", "goals", "runs", "winner"]

        if sport:
            from config import SPORTS_CATEGORIES
            sports_keywords = SPORTS_CATEGORIES.get(sport.lower(), [sport.lower()])

        filtered = []
        for m in all_markets:
            title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
            if any(kw in title for kw in sports_keywords):
                filtered.append(m)

        return filtered

    def get_orderbook(self, ticker):
        """Get current order book for a market."""
        return self._get(f"/markets/{ticker}/orderbook")

    def get_market_price(self, ticker):
        """
        Extract best bid, ask, and mid from the live orderbook.

        The new Kalshi API returns orderbook_fp with yes_dollars / no_dollars
        as [[price, dollar_size], ...] lists. yes_bid = max(yes_dollars prices),
        yes_ask = 1 - max(no_dollars prices).
        Returns prices as cents (0-100).
        """
        try:
            ob = self.get_orderbook(ticker)
            fp = ob.get("orderbook_fp", {})

            yes_bids = fp.get("yes_dollars", [])
            no_bids  = fp.get("no_dollars", [])

            best_yes_bid = max((float(p[0]) for p in yes_bids), default=None)
            best_no_bid  = max((float(p[0]) for p in no_bids),  default=None)

            # YES ask = 1 - best NO bid (in dollar/cent terms)
            best_yes_ask = round(1.0 - best_no_bid, 4) if best_no_bid else None

            if best_yes_bid is None and best_yes_ask is None:
                return None

            mid = None
            if best_yes_bid and best_yes_ask:
                mid = round((best_yes_bid + best_yes_ask) / 2, 4)
            else:
                mid = best_yes_bid or best_yes_ask

            return {
                "ticker":   ticker,
                "yes_bid":  round(best_yes_bid * 100) if best_yes_bid else None,
                "yes_ask":  round(best_yes_ask * 100) if best_yes_ask else None,
                "mid_cents": round(mid * 100) if mid else None,
                "mid_prob":  mid,
            }
        except Exception:
            return None

    def get_sports_game_markets(self):
        """
        Fetch individual game moneyline markets from Kalshi's structured
        sports series: KXNBAGAME, KXNHLGAME, KXMLBGAME.

        Returns only markets where status == 'active' and expected_expiration_time
        is not more than 24 hours in the past — filters out closed/resolved games
        even when the API's status=open query param misses them due to lag.
        """
        from datetime import datetime, timezone, timedelta
        import re

        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)

        results = []
        series_map = {
            "KXNBAGAME": "nba",
            "KXNHLGAME": "nhl",
            "KXMLBGAME": "mlb",
        }

        for series, sport in series_map.items():
            try:
                event_resp = self._get("/events", params={
                    "status": "open", "limit": 50, "series_ticker": series
                })
                events = event_resp.get("events", [])
            except Exception:
                continue

            for event in events:
                event_ticker = event.get("event_ticker", "")
                event_title  = event.get("title", "")

                try:
                    mkt_resp = self._get("/markets", params={
                        "status": "open", "limit": 10,
                        "event_ticker": event_ticker
                    })
                    markets = mkt_resp.get("markets", [])
                except Exception:
                    continue

                for m in markets:
                    # Filter 1: market status must be exactly "active"
                    if m.get("status") != "active":
                        continue

                    # Filter 2: expected_expiration_time must not be >24h in the past.
                    # Catches resolved markets that are still tagged open due to API lag.
                    exp = m.get("expected_expiration_time") or m.get("expiration_time")
                    if exp:
                        try:
                            exp_dt = datetime.fromisoformat(
                                exp.replace("Z", "+00:00")
                            )
                            if exp_dt < cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass

                    ticker = m.get("ticker", "")
                    title  = m.get("title", "")

                    price = self.get_market_price(ticker)
                    if not price or price["mid_prob"] is None:
                        continue

                    results.append({
                        "ticker":            ticker,
                        "event_ticker":      event_ticker,
                        "title":             title,
                        "event_title":       event_title,
                        "sport":             sport,
                        "yes_bid":           price["yes_bid"],
                        "yes_ask":           price["yes_ask"],
                        "mid_cents":         price["mid_cents"],
                        "mid_prob":          price["mid_prob"],
                        "expiration_time":   exp,
                    })

        return results

    def get_market_candlesticks(self, ticker, period_interval=60):
        """
        Get candlestick data for a market.
        period_interval: 1 (1min), 60 (1hr), or 1440 (1day)
        """
        params = {"period_interval": period_interval}
        return self._get(f"/markets/{ticker}/candlesticks", params=params)

    def get_event(self, event_ticker):
        """Get event data including all associated markets."""
        return self._get(f"/events/{event_ticker}")

    def get_events(self, status="open", limit=100):
        """Get all events."""
        params = {"status": status, "limit": limit}
        return self._get("/events", params=params)

    def get_trades(self, ticker=None, limit=100):
        """Get recent trades, optionally for a specific market."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("/markets/trades", params=params)

    # ==================== TRADING (Authenticated) ====================

    def place_order(self, ticker, side, quantity, price=None, order_type="limit"):
        """
        DISABLED — paper trading mode. No real orders are submitted to Kalshi.

        Returns a mock response identical in shape to the live API so callers
        that log or inspect the response still work correctly.

        To re-enable live trading: set PAPER_TRADING_ONLY = False in config.py
        and remove this guard. Only do this with real credentials and intent.
        """
        from config import PAPER_TRADING_ONLY
        if PAPER_TRADING_ONLY:
            import time as _time
            mock = {
                "paper_trade": True,
                "ticker": ticker,
                "side": side,
                "count": quantity,
                "price": price,
                "type": order_type,
                "logged_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                "note": "PAPER TRADE — no real order submitted",
            }
            print(f"  [PAPER ORDER] {side.upper()} {quantity} {ticker} @ {price}¢  (not executed)")
            return mock

        # ── Live order path (only reached if PAPER_TRADING_ONLY = False) ──
        order = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": quantity,
            "type": order_type,
        }
        if price is not None and order_type == "limit":
            if side == "yes":
                order["yes_price"] = price
            else:
                order["no_price"] = price
        return self._post("/portfolio/orders", data=order)

    def cancel_order(self, order_id):
        """Cancel an existing open order."""
        return self._delete(f"/portfolio/orders/{order_id}")

    def get_positions(self):
        """Get current portfolio positions."""
        return self._get("/portfolio/positions", auth_required=True)

    def get_balance(self):
        """Get account balance."""
        return self._get("/portfolio/balance", auth_required=True)

    def get_orders(self, status=None):
        """Get orders, optionally filtered by status."""
        params = {}
        if status:
            params["status"] = status
        return self._get("/portfolio/orders", params=params, auth_required=True)

    # ==================== HISTORICAL DATA ====================

    def get_series(self, series_ticker):
        """Get a series and all its events."""
        return self._get(f"/series/{series_ticker}")

    def get_market_history(self, ticker, limit=100):
        """Get settlement history for a market."""
        params = {"limit": limit}
        return self._get(f"/markets/{ticker}/history", params=params)


# ==================== Quick Test ====================
if __name__ == "__main__":
    client = KalshiClient()

    print("=" * 60)
    print("KALSHI SPORTS MARKET SCANNER")
    print("=" * 60)

    try:
        sports = client.get_sports_markets()
        print(f"\nFound {len(sports)} sports markets:\n")
        for m in sports[:20]:
            yes_bid = m.get("yes_bid", "N/A")
            yes_ask = m.get("yes_ask", "N/A")
            volume = m.get("volume", 0)
            print(f"  {m['ticker']}: {m.get('title', 'N/A')}")
            print(f"    Yes Bid: {yes_bid}c | Yes Ask: {yes_ask}c | Volume: {volume}")
            print()
    except Exception as e:
        print(f"Error: {e}")
