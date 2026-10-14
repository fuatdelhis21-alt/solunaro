"""Historical backtest of the LLM-free BASELINE rule (not the LLM desk).

Why only the baseline: replaying LLM agents on past candles is not a fair test — the model may
already know what happened next (lookahead through training data), and past news/sentiment
snapshots are not available. The LLM desk can only be measured forward, in paper.

What this answers: does the mechanical rule itself have positive expectancy after fees? If not,
the desk's prefilter is feeding the LLMs bad candidates and the rule should be fixed first.

No lookahead: the signal uses candles[..i] (all closed), entry is at candle i's close (+slippage),
exits are checked from candle i+1 with the same stop-first / gap-at-open logic as the paper broker.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import baseline as B
from . import risk
from .evaluate import bootstrap_diff
from .indicators import Candle, summarize

WINDOW = 200


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def run(candles: list[Candle], interval_sec: int, bcfg: dict, pcfg: dict, fee_bps: float,
        slippage_bps: float = 0, max_hold_hours: float = 72) -> dict:
    bal0 = pcfg["starting_balance_usd"]
    bal, trades, pos = bal0, [], None
    day_trades: dict[str, int] = {}
    day_pnl: dict[str, float] = {}
    last_trade_ts = None
    skipped = {}
    for i in range(WINDOW, len(candles)):
        c = candles[i]
        if pos:                                         # exits use candles strictly after entry
            hit = None
            if c.low <= pos["stop"]:
                hit = ("STOP", min(pos["stop"], c.open))
            elif c.high >= pos["target"]:
                hit = ("TARGET", max(pos["target"], c.open))
            elif c.ts + interval_sec - pos["ts"] >= max_hold_hours * 3600:
                hit = ("EXPIRED", c.close)
            if hit:
                raw = hit[1]
                exit_px = raw * (1 - slippage_bps / 10000)
                fees = (pos["entry"] + exit_px) * pos["qty"] * fee_bps / 10000
                pnl = (exit_px - pos["entry"]) * pos["qty"] - fees
                bal += pnl
                d = _day(c.ts)
                day_pnl[d] = day_pnl.get(d, 0) + pnl
                trades.append({**pos, "exit": exit_px, "reason": hit[0], "pnl": pnl,
                               "exit_ts": c.ts + interval_sec,
                               "r": (exit_px - pos["entry"]) / (pos["entry"] - pos["stop"])})
                pos = None
            continue                                    # never re-enter on the exit candle
        window = candles[i - WINDOW + 1: i + 1]
        summ = summarize(window)
        sig = B.signal(summ, bcfg)
        if not sig:
            continue
        now = c.ts + interval_sec                       # decision time = candle close
        d = _day(now)
        acct = {"balance": bal, "open_positions": 0, "trades_today": day_trades.get(d, 0),
                "pnl_today": day_pnl.get(d, 0.0),
                "in_cooldown": bool(last_trade_ts and now - last_trade_ts
                                    < pcfg["cooldown_minutes_per_market"] * 60),
                "staleness_min": 0, "max_staleness_min": 1}
        rk = risk.evaluate(sig, c.close, summ, pcfg, acct, fee_bps, slippage_bps)
        if not rk["all_pass"]:
            for chk in rk["checks"]:
                if chk["result"] == "FAIL":
                    skipped[chk["name"]] = skipped.get(chk["name"], 0) + 1
            continue
        pos = {"ts": now, "entry": rk["entry"], "stop": rk["stop"], "target": rk["target"],
               "qty": rk["qty"], "rule": sig["rule"]}
        day_trades[d] = day_trades.get(d, 0) + 1
        last_trade_ts = now
    return _summary(trades, bal0, bal, candles, skipped, open_position=pos is not None)


def _summary(trades, bal0, bal, candles, skipped, open_position) -> dict:
    rs = [t["r"] for t in trades]
    pnls = [t["pnl"] for t in trades]
    eq, peak, mdd = bal0, bal0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_rule = {}
    for t in trades:
        g = by_rule.setdefault(t["rule"], [])
        g.append(t["r"])
    ci = bootstrap_diff(rs, [0.0, 0.0]) if len(rs) >= 2 else None    # mean R vs zero
    if len(rs) < 20 or not ci:
        verdict = f"INSUFFICIENT SAMPLE ({len(rs)} trades)"
    elif ci["ci95"][0] > 0:
        verdict = "POSITIVE expectancy (95% CI of mean R above zero)"
    elif ci["ci95"][1] < 0:
        verdict = "NEGATIVE expectancy (95% CI of mean R below zero) — fix the rule first"
    else:
        verdict = "NO SIGNIFICANT EDGE (95% CI of mean R includes zero)"
    first, last = candles[0].ts, candles[-1].ts
    return {
        "period": [_iso(first), _iso(last)], "candles": len(candles), "trades": len(trades),
        "open_at_end": open_position,
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "net_pnl_usd": round(bal - bal0, 2), "return_pct": round((bal / bal0 - 1) * 100, 3),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "mean_r_ci95": ci["ci95"] if ci else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_drawdown_pct": round(mdd, 3),
        "exits": {k: sum(1 for t in trades if t["reason"] == k) for k in ("TARGET", "STOP", "EXPIRED")},
        "by_rule": {k: {"n": len(v), "avg_r": round(sum(v) / len(v), 3)} for k, v in by_rule.items()},
        "skipped_by_check": skipped,
        "verdict": verdict,
        "trade_list": [{"entry_ts": t["ts"], "exit_ts": t["exit_ts"], "rule": t["rule"],
                        "reason": t["reason"], "r": round(t["r"], 4)} for t in trades],
    }


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
