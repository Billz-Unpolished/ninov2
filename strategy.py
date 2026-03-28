"""
strategy.py — Composite weighted signal from 7 indicators for BTC 5-min binary markets.

Produces a single score: positive = Up, negative = Down.
Confidence = min(abs(score) / 7.0, 1.0)
"""

import time
import requests


def fetch_candles(symbol="BTCUSDT", interval="1m", limit=30):
    """Fetch recent 1-minute candles from Binance."""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        candles = []
        for c in raw:
            candles.append({
                "open_time": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "close_time": c[6],
            })
        return candles
    except Exception as e:
        print(f"[strategy] Binance candle fetch error: {e}")
        return []


def fetch_current_price(symbol="BTCUSDT"):
    """Fetch current BTC price from Binance."""
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": symbol}
    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        print(f"[strategy] Binance price fetch error: {e}")
        return None


def _ema(values, period):
    """Calculate EMA for a list of values."""
    if len(values) < period:
        return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes, period=14):
    """Calculate RSI from close prices."""
    if len(closes) < period + 1:
        return 50.0  # neutral
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze(candles, window_open_price, current_price, tick_prices=None):
    """
    Run all 7 indicators and return (score, confidence, details).

    Parameters:
        candles: list of 1-min candle dicts from Binance
        window_open_price: BTC price at the start of this 5-min window
        current_price: latest BTC price
        tick_prices: list of (timestamp, price) from the bot's 2s polling loop

    Returns:
        (score, confidence, details_dict)
        score > 0 = Up, score < 0 = Down
    """
    if not candles or current_price is None or window_open_price is None:
        return 0.0, 0.0, {"error": "insufficient data"}

    score = 0.0
    details = {}
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    # --- 1. Window Delta (weight 5-7) — THE dominant signal ---
    window_pct = (current_price - window_open_price) / window_open_price * 100
    abs_pct = abs(window_pct)

    if abs_pct > 0.10:
        w_delta_weight = 7
    elif abs_pct > 0.02:
        w_delta_weight = 5
    elif abs_pct > 0.005:
        w_delta_weight = 3
    elif abs_pct > 0.001:
        w_delta_weight = 1
    else:
        w_delta_weight = 0

    direction = 1 if window_pct > 0 else (-1 if window_pct < 0 else 0)
    delta_contribution = direction * w_delta_weight
    score += delta_contribution
    details["window_delta"] = {
        "pct": round(window_pct, 6),
        "weight": w_delta_weight,
        "contribution": delta_contribution,
    }

    # --- 2. Micro Momentum (weight 2) — Last 2 candles ---
    if len(closes) >= 3:
        micro = closes[-1] - closes[-3]
        micro_dir = 1 if micro > 0 else (-1 if micro < 0 else 0)
        micro_contribution = micro_dir * 2
        score += micro_contribution
        details["micro_momentum"] = {
            "delta": round(micro, 2),
            "contribution": micro_contribution,
        }
    else:
        details["micro_momentum"] = {"delta": 0, "contribution": 0}

    # --- 3. Acceleration (weight 1.5) ---
    if len(closes) >= 4:
        recent_move = closes[-1] - closes[-2]
        prior_move = closes[-3] - closes[-4]
        accel = recent_move - prior_move
        accel_dir = 1 if accel > 0 else (-1 if accel < 0 else 0)
        accel_contribution = accel_dir * 1.5
        score += accel_contribution
        details["acceleration"] = {
            "value": round(accel, 2),
            "contribution": accel_contribution,
        }
    else:
        details["acceleration"] = {"value": 0, "contribution": 0}

    # --- 4. EMA Crossover 9/21 (weight 1) ---
    if len(closes) >= 21:
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        ema_dir = 1 if ema9 > ema21 else -1
        ema_contribution = ema_dir * 1
        score += ema_contribution
        details["ema_crossover"] = {
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "contribution": ema_contribution,
        }
    else:
        details["ema_crossover"] = {"contribution": 0}

    # --- 5. RSI 14-period (weight 1-2) ---
    rsi_val = _rsi(closes, 14)
    if rsi_val > 75:
        rsi_contribution = -2  # overbought = bearish
    elif rsi_val < 25:
        rsi_contribution = 2   # oversold = bullish
    elif rsi_val > 60:
        rsi_contribution = -1
    elif rsi_val < 40:
        rsi_contribution = 1
    else:
        rsi_contribution = 0
    score += rsi_contribution
    details["rsi"] = {"value": round(rsi_val, 2), "contribution": rsi_contribution}

    # --- 6. Volume Surge (weight 1) ---
    if len(volumes) >= 6:
        recent_vol = sum(volumes[-3:]) / 3
        prior_vol = sum(volumes[-6:-3]) / 3
        if prior_vol > 0 and recent_vol > prior_vol * 1.5:
            # Volume surge confirms the current micro direction
            vol_dir = 1 if closes[-1] > closes[-3] else -1
            vol_contribution = vol_dir * 1
        else:
            vol_contribution = 0
    else:
        vol_contribution = 0
    score += vol_contribution
    details["volume_surge"] = {"contribution": vol_contribution}

    # --- 7. Real-Time Tick Trend (weight 2) ---
    tick_contribution = 0
    if tick_prices and len(tick_prices) >= 3:
        ups = 0
        downs = 0
        for i in range(1, len(tick_prices)):
            if tick_prices[i][1] > tick_prices[i - 1][1]:
                ups += 1
            elif tick_prices[i][1] < tick_prices[i - 1][1]:
                downs += 1
        total_ticks = ups + downs
        if total_ticks > 0:
            tick_move_pct = abs(tick_prices[-1][1] - tick_prices[0][1]) / tick_prices[0][1] * 100
            up_ratio = ups / total_ticks
            down_ratio = downs / total_ticks
            if up_ratio >= 0.60 and tick_move_pct > 0.005:
                tick_contribution = 2
            elif down_ratio >= 0.60 and tick_move_pct > 0.005:
                tick_contribution = -2
    score += tick_contribution
    details["tick_trend"] = {"contribution": tick_contribution}

    # --- Final confidence ---
    confidence = min(abs(score) / 7.0, 1.0)

    details["total_score"] = round(score, 2)
    details["confidence"] = round(confidence, 4)
    details["direction"] = "UP" if score > 0 else ("DOWN" if score < 0 else "NEUTRAL")

    return score, confidence, details
