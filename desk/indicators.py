"""Deterministic indicators computed from candles. No LLM involved."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candle:
    ts: int          # open time, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float | None = None


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) <= period:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def swing_levels(candles: list[Candle], window: int = 3, keep: int = 4) -> dict:
    """Recent swing highs (resistance) and swing lows (support)."""
    highs, lows = [], []
    for i in range(window, len(candles) - window):
        seg = candles[i - window:i + window + 1]
        if candles[i].high == max(c.high for c in seg):
            highs.append(round(candles[i].high, 8))
        if candles[i].low == min(c.low for c in seg):
            lows.append(round(candles[i].low, 8))
    last = candles[-1].close
    res = sorted({h for h in highs if h > last})[:keep]
    sup = sorted({l for l in lows if l < last}, reverse=True)[:keep]
    return {"resistance_above": res, "support_below": sup}


def summarize(candles: list[Candle]) -> dict:
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    last = candles[-1]
    vol_avg20 = sum(vols[-21:-1]) / max(len(vols[-21:-1]), 1)
    out = {
        "last_close": last.close,
        "last_candle_open_ts": last.ts,
        "candles_used": len(candles),
        "ema20": round(e20[-1], 8),
        "ema50": round(e50[-1], 8),
        "ema200": round(e200[-1], 8) if len(closes) >= 200 else None,
        "rsi14": round(rsi(closes), 2) if rsi(closes) is not None else None,
        "atr14": round(atr(candles), 8) if atr(candles) is not None else None,
        "high_24": max(c.high for c in candles[-24:]),
        "low_24": min(c.low for c in candles[-24:]),
        "high_lookback": max(c.high for c in candles),
        "low_lookback": min(c.low for c in candles),
        "change_24_pct": round((last.close / candles[-25].close - 1) * 100, 3) if len(candles) > 25 else None,
        "volume_last_vs_avg20": round(last.volume / vol_avg20, 3) if vol_avg20 else None,
        "levels": swing_levels(candles),
        "last_12_candles": [
            [c.ts, c.open, c.high, c.low, c.close, round(c.volume, 4)] for c in candles[-12:]
        ],
    }
    if last.taker_buy_volume is not None:
        tb = sum(c.taker_buy_volume or 0 for c in candles[-24:])
        tv = sum(c.volume for c in candles[-24:])
        out["taker_buy_ratio_24"] = round(tb / tv, 4) if tv else None
    return out
