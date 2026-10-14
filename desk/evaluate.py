"""Measurement: desk vs baseline, counterfactual outcomes, per-agent accuracy, LLM spend.
Everything is computed from the sqlite journals — nothing is estimated or invented."""
from __future__ import annotations

import json
import random
from collections import defaultdict

MIN_SAMPLE = 20
AGENT_FIELDS = {"ATLAS": "atlas", "VEGA": "vega", "ORION": "orion", "LUNA": "luna",
                "TITAN": "titan", "NOVA": "nova"}


def book_metrics(broker) -> dict:
    rows = broker.closed_positions()
    start = float(broker.get_meta("starting_balance"))
    pnls = [r["pnl_usd"] for r in rows]
    eq, peak, mdd, curve = start, start, 0.0, [(None, start)]
    for r in rows:
        eq += r["pnl_usd"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100 if peak else 0)
        curve.append((r["closed_ts"], round(eq, 2)))
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = r_list(rows)
    return {
        "closed_trades": len(rows),
        "open_trades": len(broker.open_positions()),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 1) if rows else None,
        "net_pnl_usd": round(sum(pnls), 2),
        "return_pct": round(sum(pnls) / start * 100, 3) if start else None,
        "expectancy_usd": round(sum(pnls) / len(rows), 2) if rows else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_drawdown_pct": round(mdd, 3),
        "equity_curve": curve,
    }


def shadow_stats(broker) -> dict:
    """Outcome of every grounded setup, grouped by the desk's final decision."""
    by = defaultdict(lambda: {"n": 0, "target": 0, "stop": 0, "expired": 0, "open": 0, "r": []})
    for s in broker.shadows():
        g = by[s["decision"]]
        g["n"] += 1
        st = s["status"]
        g[{"TARGET": "target", "STOP": "stop", "EXPIRED": "expired"}.get(st, "open")] += 1
        if s["r_multiple"] is not None:
            g["r"].append(s["r_multiple"])
    out = {}
    for k, g in by.items():
        done = g["target"] + g["stop"]
        out[k] = {"setups": g["n"], "hit_target": g["target"], "hit_stop": g["stop"],
                  "expired": g["expired"], "still_open": g["open"],
                  "target_rate_pct": round(g["target"] / done * 100, 1) if done else None,
                  "avg_r": round(sum(g["r"]) / len(g["r"]), 3) if g["r"] else None}
    return out


def agent_accuracy(broker) -> dict:
    """For each agent verdict: how often did the setup reach target before stop?"""
    acc = defaultdict(lambda: defaultdict(lambda: {"n": 0, "target": 0, "stop": 0, "r": []}))
    for s in broker.shadows():
        if s["status"] == "OPEN":
            continue
        v = json.loads(s["verdicts"] or "{}")
        for agent, verdict in v.items():
            g = acc[agent][str(verdict)]
            g["n"] += 1
            if s["status"] == "TARGET":
                g["target"] += 1
            elif s["status"] == "STOP":
                g["stop"] += 1
            if s["r_multiple"] is not None:
                g["r"].append(s["r_multiple"])
    out = {}
    for agent, verdicts in acc.items():
        out[agent] = {}
        for verdict, g in verdicts.items():
            done = g["target"] + g["stop"]
            out[agent][verdict] = {
                "resolved": g["n"], "target_rate_pct": round(g["target"] / done * 100, 1) if done else None,
                "avg_r": round(sum(g["r"]) / len(g["r"]), 3) if g["r"] else None}
    return out


def llm_spend(broker, prices: dict | None) -> dict:
    """Token totals per agent/model. USD only if the user filled in prices (per 1M tokens)."""
    rows, total_in, total_out, usd, priced = broker.usage_rows(), 0, 0, 0.0, True
    per = []
    for r in rows:
        total_in += r["inp"] or 0
        total_out += r["outp"] or 0
        p = (prices or {}).get(r["model"])
        cost = None
        if p and p.get("input") is not None and p.get("output") is not None:
            cost = (r["inp"] or 0) / 1e6 * p["input"] + (r["outp"] or 0) / 1e6 * p["output"]
            usd += cost
        else:
            priced = False
        per.append({"agent": r["agent"], "model": r["model"], "calls": r["calls"],
                    "input_tokens": r["inp"], "output_tokens": r["outp"],
                    "usd": round(cost, 4) if cost is not None else None})
    return {"per_agent": per, "input_tokens": total_in, "output_tokens": total_out,
            "usd_total": round(usd, 4) if priced and rows else None,
            "usd_note": None if priced else "Fill llm.prices_per_mtok in config.yaml to see USD."}


def r_list(rows) -> list[float]:
    return [(r["exit"] - r["entry"]) / (r["entry"] - r["stop"]) for r in rows
            if r["exit"] is not None and r["entry"] > r["stop"]]


def bootstrap_diff(a: list[float], b: list[float], n: int = 4000, seed: int = 1) -> dict | None:
    """95% bootstrap CI for mean(a) - mean(b). Deterministic (fixed seed)."""
    if len(a) < 2 or len(b) < 2:
        return None
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        ma = sum(rng.choice(a) for _ in a) / len(a)
        mb = sum(rng.choice(b) for _ in b) / len(b)
        diffs.append(ma - mb)
    diffs.sort()
    return {"diff": round(sum(a) / len(a) - sum(b) / len(b), 3),
            "ci95": [round(diffs[int(0.025 * n)], 3), round(diffs[int(0.975 * n)], 3)]}


def cluster_bootstrap_diff(items: list[tuple], n: int = 3000, seed: int = 1,
                           level: float = 0.95) -> dict | None:
    """items = (cluster_key, in_group_a: bool, r). Mean(a) - mean(b) with a CI from resampling
    whole CLUSTERS (UTC days). Hourly setups on the same market overlap and resolve together, so
    treating them as independent makes intervals far too narrow (false discoveries)."""
    a = [r for _, g, r in items if g]
    b = [r for _, g, r in items if not g]
    if len(a) < 2 or len(b) < 2:
        return None
    clusters: dict = defaultdict(list)
    for k, g, r in items:
        clusters[k].append((g, r))
    keys = list(clusters)
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        sa = sb = na = nb = 0.0
        for _k in keys:
            for g, r in clusters[rng.choice(keys)]:
                if g:
                    sa, na = sa + r, na + 1
                else:
                    sb, nb = sb + r, nb + 1
        if na and nb:
            diffs.append(sa / na - sb / nb)
    if len(diffs) < n * 0.5:
        return None
    diffs.sort()
    lo_q = (1 - level) / 2
    return {"diff": round(sum(a) / len(a) - sum(b) / len(b), 3),
            "ci95": [round(diffs[int(lo_q * len(diffs))], 3),
                     round(diffs[min(int((1 - lo_q) * len(diffs)), len(diffs) - 1)], 3)],
            "clusters": len(keys), "level": level}


def _day_key(ts: int) -> int:
    return int(ts) // 86400


def verdict(desk: dict, base: dict, test: dict | None = None) -> str:
    """Only claims an edge when the 95% CI of the R difference excludes zero."""
    n1, n2 = desk["closed_trades"], base["closed_trades"]
    if n1 < MIN_SAMPLE or n2 < MIN_SAMPLE:
        return (f"INSUFFICIENT SAMPLE: desk {n1}, baseline {n2} closed trades "
                f"(need >= {MIN_SAMPLE} each). No conclusion yet.")
    if not test:
        return "INSUFFICIENT SAMPLE: R multiples unavailable."
    lo, hi = test["ci95"]
    if lo > 0:
        return (f"Desk ahead: +{test['diff']} R/trade vs baseline, 95% CI [{lo}, {hi}] "
                f"excludes zero (max DD {desk['max_drawdown_pct']}% vs {base['max_drawdown_pct']}%).")
    if hi < 0:
        return (f"Desk WORSE than the LLM-free baseline: {test['diff']} R/trade, "
                f"95% CI [{lo}, {hi}].")
    return (f"NO SIGNIFICANT DIFFERENCE: desk vs baseline {test['diff']:+} R/trade, 95% CI "
            f"[{lo}, {hi}] includes zero. The LLMs have not shown an edge yet.")


def selection_test(broker) -> dict | None:
    """Within the SAME setups: do the ones NOVA traded do better than the ones it skipped?
    This controls for market regime, unlike desk-vs-baseline."""
    done = [s for s in broker.shadows() if s["status"] != "OPEN" and s["r_multiple"] is not None]
    taken = [s["r_multiple"] for s in done if s["taken"]]
    skipped = [s["r_multiple"] for s in done if not s["taken"]]
    t = cluster_bootstrap_diff([(_day_key(s["ts"]), bool(s["taken"]), s["r_multiple"]) for s in done])
    if not t:
        return None
    t.update(n_taken=len(taken), n_skipped=len(skipped))
    lo, hi = t["ci95"]
    t["verdict"] = ("selection adds value" if lo > 0 else "selection hurts" if hi < 0
                    else "no significant selection skill")
    if len(taken) < MIN_SAMPLE or len(skipped) < MIN_SAMPLE:
        t["verdict"] = f"insufficient sample (need >= {MIN_SAMPLE} each)"
    return t


POSITIVE = {"ORION": {"SUPPORTIVE"}, "LUNA": {"SUPPORTIVE"}, "TITAN": {"APPROVE"},
            "NOVA": {"TRADE"}, "CODE": {"PASS"}}
NEGATIVE = {"ORION": {"AGAINST"}, "LUNA": {"AGAINST"}, "TITAN": {"REJECT", "REVISE"},
            "NOVA": {"REJECT", "WAIT"}, "CODE": {"FAIL"}}


def agent_value(broker) -> dict:
    """Does an agent's opinion carry information? Mean R when it was positive minus mean R when
    it was negative, over the same pool of setups, with a 99% day-clustered bootstrap CI. ATLAS and VEGA are
    excluded: every recorded setup already passed them, so there is no negative group."""
    done = [s for s in broker.shadows() if s["status"] != "OPEN" and s["r_multiple"] is not None]
    out = {}
    for agent in POSITIVE:
        pos, neg, items = [], [], []
        for s in done:
            v = json.loads(s["verdicts"] or "{}").get(agent)
            if v in POSITIVE[agent]:
                pos.append(s["r_multiple"])
                items.append((_day_key(s["ts"]), True, s["r_multiple"]))
            elif v in NEGATIVE[agent]:
                neg.append(s["r_multiple"])
                items.append((_day_key(s["ts"]), False, s["r_multiple"]))
        # 99% intervals: five agents are tested at once (multiple-comparison correction)
        t = cluster_bootstrap_diff(items, level=0.99)
        if not t:
            out[agent] = {"n_pos": len(pos), "n_neg": len(neg), "verdict": "no data"}
            continue
        lo, hi = t["ci95"]
        if min(len(pos), len(neg)) < 10:
            v = "insufficient sample (need >= 10 in each group)"
        elif lo > 0:
            v = "ADDS INFORMATION"
        elif hi < 0:
            v = "INVERSE SIGNAL (its positives do worse)"
        else:
            v = "no measurable information"
        out[agent] = {"n_pos": len(pos), "n_neg": len(neg), "diff_r": t["diff"],
                      "ci95": t["ci95"], "verdict": v}
    return out


def full_report(desk_broker, base_broker, prices=None) -> dict:
    d, b = book_metrics(desk_broker), book_metrics(base_broker)
    test = bootstrap_diff(r_list(desk_broker.closed_positions()), r_list(base_broker.closed_positions()))
    return {"desk": d, "baseline": b, "r_test": test, "verdict": verdict(d, b, test),
            "selection_test": selection_test(desk_broker),
            "agent_value": agent_value(desk_broker),
            "experiment": desk_broker.scope or "ALL",
            "experiments": [dict(r) for r in desk_broker.experiments()],
            "shadows_by_decision": shadow_stats(desk_broker),
            "agent_accuracy": agent_accuracy(desk_broker),
            "llm_spend": llm_spend(desk_broker, prices),
            "decision_counts": _counts(desk_broker)}


def _counts(broker) -> dict:
    c = defaultdict(int)
    for r in broker.decisions():
        c[r["nova"]] += 1
    return dict(c)
