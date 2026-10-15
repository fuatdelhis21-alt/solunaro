"""Calibration: run the whole bot offline with RANDOM agents on a synthetic market.

Random agents have no skill by construction, so every "edge" the evaluation reports here is a
false positive. Run many seeds to check the statistics stay honest:

    for i in $(seq 1 40); do SEED=$i python3 tools/calibrate.py; done | grep CAL

Results say NOTHING about real performance. No network, no API key, no cost.
"""
import math, os, random, sys, tempfile, time as _time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
# Fixed synthetic epoch (2026-01-01 UTC) so a seed always reproduces the same run; a wall-clock
# start shifted the UTC-day clusters between runs and changed the statistics.
clock = {"t": 1767225600 + 30}
_time.time = lambda: clock["t"]
import yaml
from desk.engine import Engine
from desk.indicators import Candle, summarize
from desk.data import MarketSnapshot
from desk.notify import Notifier
from desk import evaluate as E
from desk.dashboard import build_html

import random as _random
SEED = int(os.environ.get("SEED", 7))
random.seed(SEED)
H = 3600
start = clock["t"] - 30 - 200 * H
path, p = [], 150.0
for i in range(800):
    o = p
    p = max(1, p * (1 + random.gauss(0.0004, 0.009)))
    hi, lo = max(o, p) * (1 + abs(random.gauss(0, 0.003))), min(o, p) * (1 - abs(random.gauss(0, 0.003)))
    path.append(Candle(start + i * H, o, hi, lo, p, 1000 + random.random() * 500, 500 + random.random() * 300))

def fetch(kind, item):
    now = clock["t"]
    cs = [c for c in path if c.ts + H <= now][-200:]
    return MarketSnapshot("crypto:SOLUSDT", "crypto", "SOLUSDT", cs, H, now, 10, keywords=["solana"])

class RandLLM:
    def __init__(self): self.q = []
    def ask_json(self, system, user, agent=None):
        # VEGA/ORION/LUNA run in parallel threads: one RNG per (seed, agent, time) keeps a seed
        # reproducible regardless of thread scheduling.
        rng = _random.Random(f"{SEED}-{agent}-{clock['t']}")
        self.q.append({"agent": agent, "model": "sim", "input_tokens": 1500, "output_tokens": 300})
        if agent == "ATLAS": return {"ranking": [{"market_id": "crypto:SOLUSDT", "verdict": "WATCH"}], "top_pick": "crypto:SOLUSDT"}
        if agent == "VEGA":
            s = summarize(fetch(None, None).candles); a = s["atr14"]; l = s["last_close"]
            if rng.random() < 0.3: return {"verdict": "NO SETUP", "setup": None}
            return {"verdict": "SETUP", "setup": {"direction": "LONG", "stop": l - a, "target": l + 3 * a}}
        if agent == "ORION": return {"verdict": rng.choice(["SUPPORTIVE", "NEUTRAL", "AGAINST"])}
        if agent == "LUNA": return {"verdict": rng.choice(["SUPPORTIVE", "NEUTRAL", "AGAINST"])}
        if agent == "TITAN": return {"final_verdict": rng.choice(["APPROVE", "APPROVE", "REJECT", "REVISE"])}
        if agent == "NOVA": return {"decision": rng.choice(["TRADE", "TRADE", "WAIT", "REJECT"]), "reason": "sim"}
    def drain_calls(self): q, self.q = self.q, []; return q

d = tempfile.mkdtemp()
with open(os.path.join(ROOT, "config.yaml")) as f:
    cfg = yaml.safe_load(f)
cfg["markets"]["dex"]["enabled"] = False
cfg["markets"]["crypto"]["symbols"] = [{"symbol": "SOLUSDT", "keywords": ["solana"]}]
cfg["storage"] = {"db_path": f"{d}/d.sqlite", "baseline_db_path": f"{d}/b.sqlite",
                  "reports_dir": f"{d}/r", "kill_switch_file": f"{d}/STOP"}
cfg["llm"]["prices_per_mtok"] = {"sim": {"input": 3, "output": 15}}
news = lambda: ([{"title": "Solana x", "link": "https://e/x", "published_ts": clock["t"] - 600, "source": "e", "summary": ""}], [])
eng = Engine(cfg, RandLLM(), fetch=fetch, news_fetch=news, fng_fetch=lambda: {"latest": {"value": 50}},
             positioning_fetch=lambda s: {"funding_rate_last_pct": 0.01}, notifier=Notifier("", ""))
results = {}
for h in range(590):
    for m in range(0, 60, 5):
        clock["t"] = start + 200 * H + h * H + m * 60 + 30
        eng.monitor()
        if m == 0:
            r = eng.decide()["result"]
            results[r.split(":")[0]] = results.get(r.split(":")[0], 0) + 1
rep = E.full_report(eng.broker, eng.base, cfg["llm"]["prices_per_mtok"])
print("cycle results:", results)
for k in ("desk", "baseline"):
    print(k, {x: y for x, y in rep[k].items() if x != "equity_curve"})
print("verdict:", rep["verdict"])
print("shadows:", rep["shadows_by_decision"])
print("TITAN acc:", rep["agent_accuracy"].get("TITAN"))
print("spend:", rep["llm_spend"]["usd_total"], rep["llm_spend"]["input_tokens"])
print("notifications:", len(eng.notify.sent))
# consistency: desk TRADE shadows vs positions
taken = [s for s in eng.broker.shadows() if s["taken"]]
print("taken shadows", len(taken), "positions", len(eng.broker.closed_positions()) + len(eng.broker.open_positions()))
bad = [(s["status"], p["exit_reason"]) for s, p in zip(taken, eng.broker.closed_positions()) if s["status"] != p["exit_reason"]]
print("shadow/position exit mismatches:", bad[:5], len(bad))
import json as _j
_sel = rep["selection_test"] or {}
print("CAL", _j.dumps({"seed": SEED, "desk": rep["verdict"].split(":")[0],
                       "sel": _sel.get("ci95"), "sel_verdict": _sel.get("verdict"),
                       "sel_n": [_sel.get("n_taken"), _sel.get("n_skipped")],
                       "agents": {k: v["verdict"] for k, v in rep["agent_value"].items()}}))
