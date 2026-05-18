"""
Configuration for Kalshi Sports Trader
Set your API credentials here or use environment variables.
"""
import os

# Kalshi API Configuration
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
# Production WebSocket host is external-api-ws.kalshi.com (separate from the REST host).
# trading-api.kalshi.com resolves but is the REST host — using it for WS gives 401
# regardless of auth correctness because it's a different backend.
# Source: https://docs.kalshi.com/websockets/websocket-connection
KALSHI_WS_URL      = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_WS_URL_DEMO = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# Polymarket WebSocket
# /ws/ returns HTTP 404; /ws/market is the correct path (verified via WS handshake test)
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# PAPER TRADING ONLY — prevents any real order submission to Kalshi or Polymarket.
# Set to False only if you have live credentials and explicitly intend to trade real money.
PAPER_TRADING_ONLY = True

# Trading Parameters
DEFAULT_SPREAD_WIDTH = 0.05      # 5 cents on each side of fair value
MIN_EDGE_THRESHOLD = 0.03        # Minimum 3% edge to trade
MAX_POSITION_PER_CONTRACT = 100  # Max contracts per market
MAX_PORTFOLIO_EXPOSURE = 1000    # Max total exposure in dollars
DRAWDOWN_LIMIT = 0.10            # 10% drawdown triggers position reduction

# Injury Adjustment
# Players listed here are treated as season-long absences.
# Code looks up their BPM and MP automatically from data/BR_Data.csv.
# Update when a player goes down long-term or returns.
INJURED_OUT = {
    # Example:
    # "Milwaukee Bucks": ["Damian Lillard"],
}

# Playoff GP scale factor for blended ratings.
# Playoff games count as this fraction of a regular season game in the GP-weighted blend.
# 1.0 = full weight, 0.5 = half weight. Lower when bracket path was soft (weak opponents
# inflate raw net rating; perf_adj already handles SOS — this prevents double-counting).
PLAYOFF_GP_SCALE = 0.5

# Manual team power rating adjustments (added on top of the computed rating).
# Use for mid-season roster changes or health improvements not captured by full-season stats.
# Cavs: post-Harden (Feb 7+) net rating is +4.8 vs full-season +4.1 — bump reflects
# the real roster that will play in the playoffs.
MANUAL_TEAM_ADJUSTMENTS = {
    "Cleveland Cavaliers": +0.7,
}

# Path to Basketball Reference advanced stats CSV (downloaded manually from BR)
BR_DATA_PATH = "data/BR_Data.csv"

# Sport-specific settings
SPORTS_CATEGORIES = {
    "nba": ["NBA", "basketball", "nba"],
    "nhl": ["NHL", "hockey", "nhl"],
    "ncaab": ["NCAAB", "college basketball", "ncaab", "march madness"],
    "nfl": ["NFL", "football", "nfl"],
    "mlb": ["MLB", "baseball", "mlb"],
}
