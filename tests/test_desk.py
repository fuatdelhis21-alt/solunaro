"""Offline tests: synthetic candles + a scripted fake LLM. No network, no API key."""
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from desk import risk  # noqa: E402
from desk.data import MarketSnapshot  # noqa: E402
from desk.engine import Engine  # noqa: E402
from desk.indicators import Candle, rsi, summarize  # noqa: E402
from desk.llm import extract_json  # noqa: E402
from desk.notify import Notifier  # noqa: E402
from desk.paper import PaperBroker  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_yaml():
    with open(os.path.join(ROOT, "config.yaml")) as f:
        return yaml.safe_load(f)


def candles(n=200, start=100.0, step=3600, end_ts=None):
    end_ts = end_ts or (int(time.time()) // step * step)
    first = end_ts - n * step
    out = []
    p = start
    for i in range(n):
        o = p
        p = start + i * 0.05 + 2 * math.sin(i / 6)
        out.append(Candle(first + i * step, o, max(o, p) + 0.4, min(o, p) - 0.4, p, 1000 + i,
                          taker_buy_volume=550 + i / 2))
    return out


def invented_level(summary):
    """Midpoint of the widest gap between supplied levels below price."""
    last = summary["last_close"]
    lv = sorted(x for x in risk.allowed_levels(summary) if last * 0.93 < x < last)
    gaps = max(zip(lv, lv[1:]), key=lambda p: p[1] - p[0])
    v = sum(gaps) / 2
    assert not risk.grounded(v, risk.allowed_levels(summary))
    return v


def snap(mid="crypto:SOLUSDT", **kw):
    c = kw.pop("c", None) or candles()
    return MarketSnapshot(market_id=mid, kind="crypto", label="SOLUSDT", candles=c,
                          interval_sec=3600, fetched_at=int(time.time()), fee_bps=10,
                          keywords=["solana"], **kw)


class FakeLLM:
    """Routes by agent name found in the system prompt. Override answers per test."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.usage = {"calls": 0}

    def ask_json(self, system, user, agent=None):
        for name in ("ATLAS", "VEGA", "ORION", "LUNA", "TITAN", "NOVA"):
            if f"You are {name}" in system:
                self.calls.append(name)
                a = self.answers[name]
                return a(user) if callable(a) else a
        raise AssertionError("unknown agent")


class TestIndicators(unittest.TestCase):
    def test_rsi_bounds_and_summary(self):
        c = candles()
        r = rsi([x.close for x in c])
        self.assertTrue(0 <= r <= 100)
        s = summarize(c)
        self.assertEqual(s["last_close"], c[-1].close)
        self.assertIn("support_below", s["levels"])
        self.assertIsNotNone(s["taker_buy_ratio_24"])

    def test_extract_json(self):
        self.assertEqual(extract_json('x ```json\n{"a": 1}\n``` y'), {"a": 1})
        self.assertEqual(extract_json('pre {"b": [1,2]} post'), {"b": [1, 2]})


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.cfg = load_yaml()["paper"]
        self.c = candles()
        self.s = summarize(self.c)
        self.acct = {"balance": 10000, "open_positions": 0, "trades_today": 0, "pnl_today": 0,
                     "in_cooldown": False, "staleness_min": 5, "max_staleness_min": 90}

    def good_setup(self):
        last, a = self.s["last_close"], self.s["atr14"]
        return {"stop": last - 1 * a, "target": last + 3 * a}

    def test_grounded_setup_passes_and_sizes_to_max_loss(self):
        r = risk.evaluate(self.good_setup(), self.s["last_close"], self.s, self.cfg, self.acct, 10)
        self.assertTrue(r["all_pass"], r["checks"])
        self.assertLessEqual(r["max_loss_usd"], 10000 * 0.0025 + 0.01)
        self.assertLessEqual(r["notional"], 10000 * 0.20 + 0.01)

    def test_invented_stop_rejected(self):
        st = self.good_setup()
        st["stop"] = invented_level(self.s)             # not any supplied level
        r = risk.evaluate(st, self.s["last_close"], self.s, self.cfg, self.acct, 10)
        fails = {c["name"] for c in r["checks"] if c["result"] == "FAIL"}
        self.assertIn("stop_grounded_in_data", fails)
        self.assertFalse(r["all_pass"])

    def test_bad_rr_and_limits(self):
        last, a = self.s["last_close"], self.s["atr14"]
        r = risk.evaluate({"stop": last - 2 * a, "target": last + 1 * a}, last, self.s, self.cfg,
                          dict(self.acct, open_positions=1, pnl_today=-150), 10)
        fails = {c["name"] for c in r["checks"] if c["result"] == "FAIL"}
        self.assertTrue({"reward_risk", "max_open_positions", "daily_kill"} <= fails)

    def test_stale_data_fails(self):
        r = risk.evaluate(self.good_setup(), self.s["last_close"], self.s, self.cfg,
                          dict(self.acct, staleness_min=500), 10)
        self.assertFalse(r["all_pass"])


class TestPaper(unittest.TestCase):
    def test_stop_first_and_pnl(self):
        with tempfile.TemporaryDirectory() as d:
            b = PaperBroker(os.path.join(d, "x.sqlite"), 10000)
            pid = b.open("m", "M", 100.0, 10, 95.0, 110.0, 10, 0, 1)
            pos = b.open_positions()[0]
            later = pos["opened_ts"] + 10
            both = [Candle(later, 100, 111, 94, 105, 1)]           # touches both
            self.assertEqual(b.check_exits(pos, both)[0], "STOP")
            gap = [Candle(later, 90, 92, 89, 91, 1)]                 # gap below stop
            self.assertEqual(b.check_exits(pos, gap)[1], 90)
            pnl = b.close(pos, 110.0, "TARGET")
            self.assertAlmostEqual(pnl, 100 - (100 + 110) * 10 * 0.001, places=6)
            self.assertAlmostEqual(b.balance(), 10000 + pnl)
            self.assertEqual(pid, 1)


class TestEngine(unittest.TestCase):
    def make(self, answers, news=None, snapf=None):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_yaml()
        cfg["markets"]["dex"]["enabled"] = False
        cfg["markets"]["crypto"]["symbols"] = [{"symbol": "SOLUSDT", "keywords": ["solana"]}]
        cfg["prefilter"] = {"enabled": False}
        cfg["storage"] = {"db_path": f"{self.tmp.name}/d.sqlite",
                          "baseline_db_path": f"{self.tmp.name}/b.sqlite",
                          "reports_dir": f"{self.tmp.name}/r",
                          "kill_switch_file": f"{self.tmp.name}/STOP"}
        self.cfg = cfg
        self.llm = FakeLLM(answers)
        news_items = news if news is not None else [
            {"title": "Solana upgrade ships", "link": "https://ex.com/a",
             "published_ts": int(time.time()) - 3600, "source": "ex.com", "summary": "solana"}]
        eng = Engine(cfg, self.llm, fetch=snapf or (lambda k, i: snap()),
                     news_fetch=lambda: (news_items, []),
                     fng_fetch=lambda: {"latest": {"value": 55, "label": "Greed"}},
                     positioning_fetch=lambda sym: {"funding_rate_last_pct": 0.01},
                     notifier=Notifier("", ""))
        return eng

    def answers(self, **over):
        s = summarize(candles())
        last, a = s["last_close"], s["atr14"]
        base = {
            "ATLAS": {"ranking": [], "top_pick": "crypto:SOLUSDT"},
            "VEGA": {"verdict": "SETUP", "setup": {"direction": "LONG", "stop": last - a,
                                                   "stop_source": "atr", "target": last + 3 * a,
                                                   "target_source": "atr"}},
            "ORION": {"verdict": "SUPPORTIVE"},
            "LUNA": {"verdict": "NEUTRAL", "mood_score": 55},
            "TITAN": {"final_verdict": "APPROVE", "checks": []},
            "NOVA": {"decision": "TRADE", "reason": "all aligned"},
        }
        base.update(over)
        return base

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_trade_path_opens_paper_position(self):
        eng = self.make(self.answers())
        rep = eng.decide()
        self.assertEqual(rep["result"], "TRADE", rep.get("CODE_RISK"))
        self.assertEqual(self.llm.calls[0], "ATLAS")
        self.assertEqual(self.llm.calls[-1], "NOVA")
        self.assertEqual(len(eng.broker.open_positions()), 1)
        # second cycle: position limit -> no LLM spend
        n = len(self.llm.calls)
        self.assertIn("SKIPPED", eng.decide()["result"])
        self.assertEqual(len(self.llm.calls), n)

    def test_titan_veto_overrides_nova_trade(self):
        eng = self.make(self.answers(TITAN={"final_verdict": "REJECT"}))
        rep = eng.decide()
        self.assertEqual(rep["result"], "REJECT")
        self.assertEqual(len(eng.broker.open_positions()), 0)

    def test_fabricated_level_blocks_trade_even_if_everyone_approves(self):
        a = self.answers()
        a["VEGA"]["setup"]["stop"] = invented_level(summarize(candles()))
        eng = self.make(a)
        self.assertEqual(eng.decide()["result"], "REJECT")
        self.assertEqual(len(eng.broker.open_positions()), 0)

    def test_no_news_blocks_trade(self):
        eng = self.make(self.answers(), news=[])
        rep = eng.decide()
        self.assertEqual(rep["result"], "REJECT")
        self.assertIn("news_available", [c["name"] for c in rep["CODE_RISK"]["checks"]
                                         if c["result"] == "FAIL"])

    def test_atlas_picking_unknown_market_stops(self):
        eng = self.make(self.answers(ATLAS={"top_pick": "crypto:DOGEUSDT"}))
        self.assertIn("NO DECISION", eng.decide()["result"])
        self.assertEqual(self.llm.calls, ["ATLAS"])

    def test_stale_data_means_no_llm_calls(self):
        old = candles(end_ts=int(time.time()) // 3600 * 3600 - 86400)
        eng = self.make(self.answers(), snapf=lambda k, i: snap(c=old))
        self.assertIn("no eligible market", eng.decide()["result"])
        self.assertEqual(self.llm.calls, [])

    def test_kill_switch_closes_position_and_blocks(self):
        eng = self.make(self.answers())
        eng.decide()
        open(self.cfg["storage"]["kill_switch_file"], "w").close()
        self.assertIn("kill switch", eng.decide()["result"])
        ev = eng.monitor()
        self.assertEqual(ev[0]["reason"], "KILL_SWITCH")
        self.assertEqual(len(eng.broker.open_positions()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
