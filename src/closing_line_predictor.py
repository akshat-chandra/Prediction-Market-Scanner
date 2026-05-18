"""
Closing Line Value (CLV) predictor for Polymarket sports markets.

Fetches resolved game moneylines, records price snapshots at T-24h/T-6h/T-1h/close,
appends to data/polymarket_historical.csv, and trains a linear regression to predict
remaining price drift toward the closing line.

PAPER TRADING ONLY — no orders are placed on any platform.
"""

import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LinearRegression
from sklearn.metrics import r2_score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CSV_PATH = DATA_DIR / "polymarket_historical.csv"

CSV_COLUMNS = [
    "market_id", "title", "category",
    "snapshot_time", "hours_to_close",
    "price", "volume", "imbalance",
    "closing_price", "resolution",
    "drift_remaining", "cross_platform_gap",
]

# ── Sport → Polymarket tag slug ───────────────────────────────────────────────

SPORT_SLUGS = {
    "nba": "nba",
    "nhl": "nhl",
    "mlb": "baseball",
    "nfl": "football",
    "ncaab": "ncaa-basketball",
    "soccer": "soccer",
}

SNAPSHOT_HOURS = [24, 6, 1, 0]   # hours before close


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json_field(market, key):
    raw = market.get(key, "[]")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []


def _is_main_moneyline(question: str, event_title: str) -> bool:
    """
    True if this market is the full-game moneyline.
    Identified by question == event title, OR question == "X vs. Y" / "X vs Y"
    without any spread/total/prop prefix and no half/quarter qualifier.
    """
    q = question.strip()
    if q == event_title.strip():
        return True
    q_lower = q.lower()
    return (
        " vs" in q_lower
        and "o/u" not in q_lower
        and "spread" not in q_lower
        and ": " not in q           # filters "1H Moneyline:" etc.
        and "moneyline" not in q_lower
        and "1h" not in q_lower
        and "2h" not in q_lower
        and "quarter" not in q_lower
        and "half" not in q_lower
    )


def _parse_close_time(raw: str) -> Optional[datetime]:
    """Parse ISO / Postgres-style timestamps to UTC datetime."""
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "T")
    # Normalise "+00" → "+00:00"
    if s.endswith("+00"):
        s += ":00"
    elif s.endswith("+00:00:00"):
        s = s[:-6]
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ClosingLinePredictor
# ─────────────────────────────────────────────────────────────────────────────

class ClosingLinePredictor:
    GAMMA_URL = "https://gamma-api.polymarket.com"
    CLOB_URL  = "https://clob.polymarket.com"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-sports-trader/1.0"})
        self._last_req = 0.0
        self._rate_limit = 0.20   # 5 req/sec max
        DATA_DIR.mkdir(exist_ok=True)
        self._ensure_csv()

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _get(self, base: str, path: str, params: Optional[dict] = None):
        wait = self._rate_limit - (time.time() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.time()
        try:
            r = self.session.get(f"{base}{path}", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning("Request failed %s%s: %s", base, path, e)
            return None

    # ── CSV management ────────────────────────────────────────────────────────

    def _ensure_csv(self):
        if not CSV_PATH.exists():
            with open(CSV_PATH, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_COLUMNS).writeheader()

    def _load_existing_keys(self) -> set:
        """Return (market_id, hours_to_close) pairs already in CSV."""
        if not CSV_PATH.exists():
            return set()
        try:
            df = pd.read_csv(CSV_PATH, usecols=["market_id", "hours_to_close"])
            return set(zip(df["market_id"].astype(str), df["hours_to_close"].astype(float)))
        except Exception:
            return set()

    def _append_rows(self, rows: list[dict]):
        if not rows:
            return
        with open(CSV_PATH, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_COLUMNS).writerows(rows)

    # ── Gamma: resolved game markets ─────────────────────────────────────────

    def fetch_resolved_markets(self, sport: str = "nba", limit: int = 100) -> list[dict]:
        """
        Return list of resolved moneyline markets for the given sport.
        Each dict has: market_id, title, category, end_time, token_id,
                       closing_price, resolution, volume.
        """
        slug = SPORT_SLUGS.get(sport.lower(), sport.lower())
        params = {
            "tag_slug": slug,
            "closed": "true",
            "limit": limit,
            "order": "startDate",
            "ascending": "false",
        }
        data = self._get(self.GAMMA_URL, "/events", params=params)
        if not data or not isinstance(data, list):
            log.warning("No event data for sport=%s", sport)
            return []

        markets = []
        for event in data:
            ev_title = event.get("title", "")
            for m in event.get("markets", []):
                if not m.get("closed", False):
                    continue

                question = m.get("question", "")
                if not _is_main_moneyline(question, ev_title):
                    continue

                tokens  = _parse_json_field(m, "clobTokenIds")
                prices  = _parse_json_field(m, "outcomePrices")
                outcomes = _parse_json_field(m, "outcomes")

                if len(tokens) < 2 or len(prices) < 2:
                    continue

                # Token 0 = first team; track it consistently for drift analysis
                token_0 = tokens[0]
                closing_price_0 = self._safe_float(prices[0])
                if closing_price_0 is None:
                    continue

                # Resolution: 1 if first team (token 0) won, 0 if second team won
                resolution = 1 if closing_price_0 >= 0.95 else (0 if closing_price_0 <= 0.05 else None)

                close_str = (
                    m.get("closedTime")
                    or m.get("endDate")
                    or event.get("endDate")
                )
                end_time = _parse_close_time(close_str)
                if end_time is None:
                    continue

                market_entry = {
                    "market_id":     m.get("id") or m.get("conditionId"),
                    "title":         question[:120],
                    "category":      sport,
                    "end_time":      end_time,
                    "token_id":      token_0,
                    "closing_price": round(closing_price_0, 4),
                    "resolution":    resolution,
                    "volume":        self._safe_float(m.get("volume", 0)) or 0.0,
                }
                markets.append(market_entry)

        log.info("  Resolved %s %s game moneylines", len(markets), sport.upper())
        return markets

    @staticmethod
    def _safe_float(v) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # ── CLOB: price history ───────────────────────────────────────────────────

    def fetch_price_at(self, token_id: str, target_ts: float) -> Optional[float]:
        """
        Return the price of token_id closest to target_ts (±90 min window).
        Uses CLOB /prices-history at hourly fidelity.
        """
        window = 5400   # ±90 min
        data = self._get(self.CLOB_URL, "/prices-history", params={
            "market":   token_id,
            "fidelity": 60,
            "startTs":  int(target_ts - window),
            "endTs":    int(target_ts + window),
        })
        if not data:
            return None
        history = data.get("history", [])
        if not history:
            return None
        best = min(history, key=lambda p: abs(p["t"] - target_ts))
        return float(best["p"])

    # ── Snapshot builder ─────────────────────────────────────────────────────

    def build_snapshots(self, market: dict, existing_keys: set) -> list:
        """
        For each snapshot hour (24, 6, 1, 0), fetch the price and assemble a CSV row.
        Skips snapshots already present in existing_keys.
        """
        market_id     = str(market["market_id"])
        close_dt      = market["end_time"]
        close_ts      = close_dt.timestamp()
        closing_price = market["closing_price"]
        rows = []

        for hours in SNAPSHOT_HOURS:
            key = (market_id, float(hours))
            if key in existing_keys:
                continue

            if hours == 0:
                price = closing_price
            else:
                target_ts = close_ts - hours * 3600
                price = self.fetch_price_at(market["token_id"], target_ts)

            if price is None:
                log.debug("  No price for %s at T-%dh", market_id, hours)
                continue

            snapshot_dt = close_dt - timedelta(hours=hours)
            rows.append({
                "market_id":        market_id,
                "title":            market["title"],
                "category":         market["category"],
                "snapshot_time":    snapshot_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "hours_to_close":   float(hours),
                "price":            round(price, 4),
                "volume":           round(market["volume"], 2),
                "imbalance":        float("nan"),     # not available for historical
                "closing_price":    closing_price,
                "resolution":       market["resolution"],
                "drift_remaining":  round(closing_price - price, 4),
                "cross_platform_gap": float("nan"),  # populated via cross_platform.py integration
            })

        return rows

    # ── Main run ──────────────────────────────────────────────────────────────

    def run_once(self, sports: Optional[list] = None, limit: int = 100) -> int:
        """
        Fetch resolved markets for all sports and append new snapshots to CSV.
        Returns number of new rows written.
        """
        if sports is None:
            sports = ["nba"]   # NBA-first per spec; extend as season/playoffs progress

        existing_keys = self._load_existing_keys()
        total = 0

        for sport in sports:
            log.info("Fetching resolved %s markets (limit=%d)...", sport.upper(), limit)
            markets = self.fetch_resolved_markets(sport, limit=limit)

            for mkt in markets:
                rows = self.build_snapshots(mkt, existing_keys)
                self._append_rows(rows)
                for r in rows:
                    existing_keys.add((r["market_id"], float(r["hours_to_close"])))
                total += len(rows)
                if rows:
                    log.debug("  %s: +%d rows", mkt["title"][:50], len(rows))

        log.info("Run complete — appended %d new rows to %s", total, CSV_PATH)
        return total

    def run_scheduled(self, interval_hours: int = 6, sports: Optional[list] = None):
        """Block-loop: run_once() every interval_hours. Ctrl+C to stop."""
        log.info("Scheduled fetcher starting — interval=%dh  Ctrl+C to stop.", interval_hours)
        while True:
            t0 = time.time()
            try:
                self.run_once(sports=sports)
            except Exception as e:
                log.error("Run failed: %s", e, exc_info=True)
            sleep_sec = max(0, interval_hours * 3600 - (time.time() - t0))
            log.info("Sleeping %.0f min until next run.", sleep_sec / 60)
            time.sleep(sleep_sec)


# ─────────────────────────────────────────────────────────────────────────────
# Linear regression model
# ─────────────────────────────────────────────────────────────────────────────

def train_model(csv_path: Path = CSV_PATH) -> Optional[LinearRegression]:
    """
    Train a linear regression to predict drift_remaining.

    Features (where non-NaN):
        price, volume, hours_to_close, cross_platform_gap

    Target:
        drift_remaining  (= closing_price − price at snapshot)

    Only rows with hours_to_close > 0 are used (T=0 has drift=0 by definition).
    Prints coefficients and R² to stdout.
    """
    if not csv_path.exists():
        print(f"[model] No CSV found at {csv_path}")
        return None

    df = pd.read_csv(csv_path)
    df = df[df["hours_to_close"] > 0].copy()

    if len(df) < 10:
        print(f"[model] Too few rows ({len(df)}) — need ≥10 to train. Collect more data first.")
        return None

    feature_cols = ["price", "volume", "hours_to_close", "cross_platform_gap"]
    available = [c for c in feature_cols if c in df.columns]

    # Drop rows where any non-NaN-always feature is missing
    core = ["price", "volume", "hours_to_close"]
    df = df.dropna(subset=core + ["drift_remaining"])

    # cross_platform_gap: use only if we have at least some non-NaN values
    use_gap = "cross_platform_gap" in available and df["cross_platform_gap"].notna().sum() > 5
    features = core + (["cross_platform_gap"] if use_gap else [])
    if use_gap:
        df = df.dropna(subset=["cross_platform_gap"])

    X = df[features].values.astype(float)
    y = df["drift_remaining"].values.astype(float)

    model = LinearRegression()
    model.fit(X, y)
    y_pred = model.predict(X)
    r2 = r2_score(y, y_pred)

    print("\n" + "=" * 58)
    print("CLV LINEAR REGRESSION  —  drift_remaining prediction")
    print("=" * 58)
    print(f"  Rows trained on  : {len(df)}")
    print(f"  Features         : {features}")
    print(f"  Intercept        : {model.intercept_:.4f}")
    for feat, coef in zip(features, model.coef_):
        print(f"  {feat:<22}: {coef:+.4f}")
    print(f"  R²               : {r2:.4f}")
    print("=" * 58)

    _print_model_interpretation(features, model.coef_)
    return model


def _print_model_interpretation(features: list[str], coefs: np.ndarray):
    print("\nInterpretation:")
    for feat, coef in zip(features, coefs):
        if feat == "price":
            direction = "higher prices drift DOWN" if coef < 0 else "higher prices drift UP"
            print(f"  price coef {coef:+.4f}: {direction} toward closing — "
                  f"{'mean-reversion' if coef < 0 else 'momentum'} signal")
        elif feat == "hours_to_close":
            direction = "more time → more drift remaining" if coef > 0 else "less drift as market matures"
            print(f"  hours_to_close coef {coef:+.4f}: {direction}")
        elif feat == "volume":
            print(f"  volume coef {coef:+.4f}: high-volume markets drift "
                  f"{'less' if coef < 0 else 'more'} (liquidity effect)")
        elif feat == "cross_platform_gap":
            print(f"  cross_platform_gap coef {coef:+.4f}: gap predicts "
                  f"{'convergence' if coef > 0 else 'divergence'}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket CLV data fetcher & predictor")
    parser.add_argument("--schedule", action="store_true",
                        help="Run on a 6-hour schedule (block forever)")
    parser.add_argument("--interval", type=int, default=6,
                        help="Schedule interval in hours (default 6)")
    parser.add_argument("--sports", nargs="+", default=["nba"],
                        help="Sports to fetch: nba nhl mlb nfl soccer (default: nba)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max resolved events to pull per sport (default 100)")
    parser.add_argument("--model-only", action="store_true",
                        help="Skip fetching; just train the model on existing CSV")
    args = parser.parse_args()

    predictor = ClosingLinePredictor()

    if args.model_only:
        train_model()
    elif args.schedule:
        predictor.run_scheduled(interval_hours=args.interval, sports=args.sports)
    else:
        n = predictor.run_once(sports=args.sports, limit=args.limit)
        print(f"\nFetcher done — {n} new rows written to {CSV_PATH}")
        train_model()
