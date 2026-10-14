"""Validation, LLM failure handling, experiments, agent value, backtest, history, health."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_desk import FakeLLM, load_yaml, snap  # noqa: E402
from test_measurement import Base, trend_candles  # noqa: E402

from desk import backtest as BT  # noqa: E402
from desk import data as D  # noqa: E402
from desk import evaluate as E  # noqa: E402
from desk.agents import normalize  # noqa: E402
from desk.engine import DEFAULT_BASELINE, experiment_id  # noqa: E402
from desk.indicators import Candle  # noqa: E402
from desk.paper import PaperBroker  # noqa: E402


class TestValidation(unittest.TestCase):
    def test_unknown_values_become_conservative(self):
        self.assertEqual(normalize("NOVA", {"decision": "BUY NOW"})["decision"], "REJECT")
        self.assertEqual(normalize("NOVA", {"decision": " trade "})["decision"], "TRADE")
        self.assertEqual(normalize("TITAN", {"final_verdict": "OK"})["final_verdict"], "REJECT")
        self.assertEqual(normalize("TITAN", "approve")["final_verdict"], "REJECT")
        v = normalize("VEGA", {"verdict": "SETUP", "setup": "stop 95"})
        self.assertEqual(v["verdict"], "NEED MORE DATA")
        self.assertIn("_invalid", v)

    def test_invalid_nova_cannot_open_trade(self):
        b = Base()
        b.setUp()
        try:
            eng = b.engine(b.answers(NOVA={"decision": "STRONG BUY"}))
            rep = eng.decide()
            self.assertEqual(rep["result"], "REJECT")
            self.assertIn("NOVA", rep["invalid_outputs"])
            self.assertEqual(len(eng.broker.open_positions()), 0)
        finally:
            b.tearDown()


class TestFailuresAndHealth(Base):
    def test_llm_exception_is_journaled_not_traded(self):
        def boom(user):
            raise RuntimeError("API down")
        eng = self.engine(self.answers(TITAN=boom))
        rep = eng.decide()
        self.assertTrue(rep["result"].startswith("ERROR"))
        self.assertEqual(len(eng.broker.open_positions()), 0)
        self.assertEqual(eng.broker.decisions()[-1]["nova"], "ERROR")
        self.assertTrue(any("LLM döngüsü başarısız" in m for m in self.notifier.sent))
        # baseline still worked in the same cycle
        self.assertEqual(len(eng.base.open_positions()), 1)

    def test_fetch_failure_streak_alert(self):
        def fail(k, i):
            raise D.DataError("down")
        eng = self.engine(self.answers(), candles_fn=fail)
        for _ in range(3):
            eng.decide()
        self.assertEqual(sum("hiçbir piyasadan veri" in m for m in self.notifier.sent), 1)
        self.assertEqual(self.llm.calls, [])


class TestExperiments(Base):
    def test_id_changes_with_rules_and_scope_filters(self):
        e1 = experiment_id(self.cfg)
        cfg2 = json.loads(json.dumps(self.cfg))
        cfg2["paper"]["min_reward_risk"] = 3
        self.assertNotEqual(e1, experiment_id(cfg2))
        self.assertEqual(e1, experiment_id(json.loads(json.dumps(self.cfg))))

        eng = self.engine(self.answers())
        eng.decide()
        pos = eng.broker.open_positions()[0]
        eng.broker.close(pos, pos["target"], "TARGET")
        eng.broker.scope = e1
        self.assertEqual(E.book_metrics(eng.broker)["closed_trades"], 1)
        eng.broker.scope = "other-exp"
        self.assertEqual(E.book_metrics(eng.broker)["closed_trades"], 0)
        self.assertEqual(E.shadow_stats(eng.broker), {})

    def test_old_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            p = f"{d}/old.sqlite"
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE decisions(id INTEGER PRIMARY KEY, ts INTEGER, market_id TEXT, "
                        "nova TEXT, titan TEXT, code_checks_pass INTEGER, reason TEXT, report_path TEXT)")
            con.commit()
            con.close()
            b = PaperBroker(p, 1000)
            b.experiment = "x"
            b.log_decision("m", "WAIT", "-", 0, "", "")
            self.assertEqual(b.decisions()[0]["experiment"], "x")


class TestAgentValue(unittest.TestCase):
    def test_detects_informative_and_useless_agents(self):
        with tempfile.TemporaryDirectory() as d:
            b = PaperBroker(f"{d}/x.sqlite", 10000)
            for i in range(40):
                good = i % 2 == 0                      # ORION SUPPORTIVE exactly on winners
                b.add_shadow(i, "m", 100, 95, 110, 0, "WAIT", {
                    "ORION": "SUPPORTIVE" if good else "AGAINST",
                    "LUNA": "SUPPORTIVE" if i % 4 < 2 else "AGAINST"})   # unrelated to outcome
            b.db.execute("UPDATE shadows SET ts = ts - (id % 20) * 86400")   # spread over 20 days
            b.db.commit()
            for s in b.open_shadows():
                win = json.loads(s["verdicts"])["ORION"] == "SUPPORTIVE"
                px = 111 if win else 94
                b.resolve_shadow(s, [Candle(s["ts"] + 1, 100, max(px, 100), min(px, 100), px, 1)], 72)
            v = E.agent_value(b)
            self.assertEqual(v["ORION"]["verdict"], "ADDS INFORMATION")
            self.assertEqual(v["LUNA"]["verdict"], "no measurable information")


class TestBacktest(unittest.TestCase):
    def setUp(self):
        self.p = load_yaml()["paper"]
        self.b = dict(DEFAULT_BASELINE)

    def test_no_lookahead_prefix_consistency(self):
        import math
        import random
        random.seed(3)
        cs, p = [], 100.0
        t0 = 1_700_000_000 // 3600 * 3600
        for i in range(1500):
            o = p
            p = p * (1 + random.gauss(0.0003, 0.01))
            cs.append(Candle(t0 + i * 3600, o, max(o, p) * 1.003, min(o, p) * 0.997, p, 1000))
        full = BT.run(cs, 3600, self.b, self.p, 10)
        part = BT.run(cs[:1000], 3600, self.b, self.p, 10)
        self.assertGreater(full["trades"], 5)
        # every trade that closed inside the first 1000 candles must be identical in both runs:
        # adding FUTURE candles can never change a past decision (no lookahead)
        cutoff = cs[999].ts + 3600
        a = [t for t in part["trade_list"] if t["exit_ts"] <= cutoff]
        b = [t for t in full["trade_list"] if t["exit_ts"] <= cutoff]
        self.assertGreater(len(a), 3)
        self.assertEqual(a, b)
        self.assertIn(full["verdict"].split(" ")[0], {"INSUFFICIENT", "POSITIVE", "NEGATIVE", "NO"})
        self.assertTrue(math.isfinite(full["max_drawdown_pct"]))

    def test_entry_at_signal_close_exit_next_candle(self):
        cs = trend_candles(n=260, end_ts=1_800_000_000 // 3600 * 3600)
        from desk import baseline as B
        from desk.indicators import summarize
        k = next(i for i in range(200, len(cs)) if B.signal(summarize(cs[i - 199:i + 1]), self.b))
        s = summarize(cs[k - 199:k + 1])
        target = s["last_close"] + 3 * s["atr14"]
        # candle right after the signal gaps above the target
        jump = Candle(cs[k].ts + 3600, target + 1, target + 2, target + 0.5, target + 1.5, 1)
        r = BT.run(cs[:k + 1] + [jump], 3600, self.b, self.p, 10)
        self.assertEqual(r["trades"], 1)
        self.assertEqual(r["exits"]["TARGET"], 1)
        self.assertGreater(r["avg_r"], 3)        # gap fill at open is better than target


class TestHistory(unittest.TestCase):
    def test_binance_pagination(self):
        now = int(time.time()) // 3600 * 3600
        calls = []

        def fake(url, params=None, timeout=15):
            calls.append(params.get("endTime"))
            end = (params.get("endTime") or (now - 1) * 1000) // 1000
            start = end - params["limit"] * 3600
            rows = [[t * 1000, "1", "2", "0.5", "1.5", "10", (t + 3599) * 1000, "0", 1, "5", "0", "0"]
                    for t in range((start // 3600 + 1) * 3600, end, 3600)][-params["limit"]:]

            class R:
                def json(self_inner):
                    return rows
            return R()
        with mock.patch.object(D, "_get", side_effect=fake), mock.patch.object(D.time, "sleep"):
            cs = D.binance_history("SOLUSDT", "1h", 2500)
        self.assertEqual(len(cs), 2500)
        self.assertEqual(len({c.ts for c in cs}), 2500)
        self.assertEqual([c.ts for c in cs], sorted(c.ts for c in cs))
        self.assertGreaterEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
