"""Deterministic, LLM-free strategy. Two jobs:

1. PREFILTER — markets that are not a mechanical long candidate never reach the LLM (saves cost).
2. BASELINE BOOK — every candidate is traded mechanically in a separate paper account with the
   same risk rules. If the six-agent desk cannot beat this book, the LLMs add no value.
"""
from __future__ import annotations


def signal(summary: dict, cfg: dict) -> dict | None:
    """Return {"rule", "stop", "target"} or None. Uses only computed indicators."""
    last, e20, e50 = summary.get("last_close"), summary.get("ema20"), summary.get("ema50")
    r, a = summary.get("rsi14"), summary.get("atr14")
    if None in (last, e20, e50, r, a) or a <= 0:
        return None
    stop = last - cfg["stop_atr"] * a
    target = last + cfg["target_atr"] * a
    if last > e20 > e50 and cfg["trend_rsi_min"] <= r <= cfg["trend_rsi_max"]:
        return {"rule": "TREND", "stop": stop, "target": target}
    if cfg.get("oversold_enabled") and r < cfg["oversold_rsi"]:
        return {"rule": "OVERSOLD", "stop": stop, "target": target}
    return None
