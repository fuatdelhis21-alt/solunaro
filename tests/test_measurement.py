"""Baseline book, counterfactual shadows, agent accuracy, eval, cost & notifications."""
import json
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_desk import FakeLLM, load_yaml, snap  # noqa: E402

from desk import evaluate as E  # noqa: E402
from desk.dashboard import build_html  # noqa: E402
from desk.engine import Engine  # noqa: E402
from desk.indicators import Candle, summarize  # noqa: E402
from desk.notify import Notifier  # noqa: E402
from desk.paper import PaperBroker  # noqa: E402


def trend_candles(n=200, end_ts=None, extra=()):
    end = end_ts or int(time.time()) // 3600 * 3600
    first = end - n * 3600
    out, p = [], 100.0
    for i in range(n):
        o = p
        p = 100 + i * 0.05 + 0.6 * math.sin(i * 1.7)
        out.append(Candle(first + i * 3600, o, max(o, p) + 0.3, min(o, p) - 0.3, p, 1000,
                          taker_buy_volume=520))
    return out + list(extra)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = load_yaml()
        cfg["markets"]["dex"]["enabled"] = False
        cfg["markets"]["crypto"]["symbols"] = [{"symbol": "SOLUSDT", "keywords": ["solana"]}]
        cfg["storage"] = {"db_path": f"{self.tmp.name}/d.sqlite",
                          "baseline_db_path": f"{self.tmp.name}/b.sqlite",
                          "reports_dir": f"{self.tmp.name}/r",
                          "kill_switch_file": f"{self.tmp.name}/STOP"}
        self.cfg = cfg
        self.candles = trend_candles()
        self.s = summarize(self.candles)

    def tearDown(self):
        self.tmp.cleanup()

    def answers(self, **over):
        last, a = self.s["last_close"], self.s["atr14"]
        base = {
            "ATLAS": {"ranking": [{"market_id": "crypto:SOLUSDT", "verdict": "WATCH"}],
                      "top_pick": "crypto:SOLUSDT"},
            "VEGA": {"verdict": "SETUP", "setup": {"direction": "LONG", "stop": last - a,
                                                   "target": last + 3 * a}},
            "ORION": {"verdict": "SUPPORTIVE"},
            "LUNA": {"verdict": "NEUTRAL", "mood_score": 55},
            "TITAN": {"final_verdict": "APPROVE"},
            "NOVA": {"decision": "TRADE", "reason": "aligned"},
        }
        base.update(over)
        return base

    def engine(self, answers, candles_fn=None, cp=None):
        self.llm = FakeLLM(answers)
        self.notifier = Notifier("", "")
        self.cur = {"c": self.candles}
        news = [{"title": "Solana news", "link": "https://ex/a",
                 "published_ts": int(time.time()) - 600, "source": "ex", "summary": "solana"}]
        return Engine(self.cfg, self.llm,
                      fetch=candles_fn or (lambda k, i: snap(c=self.cur["c"])),
                      news_fetch=lambda: (news, []),
                      fng_fetch=lambda: {"latest": {"value": 50}},
                      positioning_fetch=lambda sym: {"funding_rate_last_pct": 0.01,
                                                     "open_interest_change_24h_pct": 3.2},
                      cryptopanic_fetch=cp or (lambda cur: []),
                      notifier=self.notifier)


class TestBaselineAndPrefilter(Base):
    def test_baseline_trades_independently_of_llm(self):
        eng = self.engine(self.answers(NOVA={"decision": "REJECT", "reason": "no"}))
        rep = eng.decide()
        self.assertEqual(len(rep["baseline_opened"]), 1)
        self.assertEqual(len(eng.base.open_positions()), 1)
        self.assertEqual(len(eng.broker.open_positions()), 0)     # desk said REJECT

    def test_prefilter_blocks_llm_when_no_candidate(self):
        from test_desk import candles as flat_candles          # RSI ~87 -> no signal
        eng = self.engine(self.answers(), candles_fn=lambda k, i: snap(c=flat_candles()))
        rep = eng.decide()
        self.assertIn("NO DECISION", rep["result"])
        self.assertEqual(self.llm.calls, [])
        self.assertIn("prefilter", json.dumps(rep["overview"]))

    def test_baseline_runs_even_when_desk_has_open_position(self):
        eng = self.engine(self.answers())
        eng.decide()                                   # desk + baseline both open
        self.assertEqual(len(eng.broker.open_positions()), 1)
        # close baseline position by hitting its stop, keep desk open
        pos = eng.base.open_positions()[0]
        eng.base.close(pos, pos["stop"], "STOP")
        eng.base.db.execute("UPDATE positions SET opened_ts=opened_ts-86400, closed_ts=closed_ts")
        eng.base.db.execute("UPDATE decisions SET ts=ts-86400")
        eng.base.db.commit()
        n = len(self.llm.calls)
        rep = eng.decide()
        self.assertIn("SKIPPED", rep["result"])
        self.assertEqual(len(self.llm.calls), n)       # no LLM spend
        self.assertEqual(len(rep["baseline_opened"]), 1)


class TestShadowsAndAccuracy(Base):
    def test_rejected_setup_is_tracked_and_resolved(self):
        eng = self.engine(self.answers(TITAN={"final_verdict": "REJECT"}))
        eng.decide()
        sh = eng.broker.open_shadows()
        self.assertEqual(len(sh), 1)
        self.assertEqual(sh[0]["taken"], 0)
        self.assertEqual(sh[0]["decision"], "REJECT")
        # price later rallies through the target
        t = sh[0]["target"]
        self.cur["c"] = self.candles + [Candle(sh[0]["ts"] + 60, t - 1, t + 1, t - 1.2, t + 0.5, 1)]
        eng.monitor()
        done = eng.broker.shadows()[0]
        self.assertEqual(done["status"], "TARGET")
        self.assertGreater(done["r_multiple"], 2.5)
        st = E.shadow_stats(eng.broker)
        self.assertEqual(st["REJECT"]["hit_target"], 1)
        acc = E.agent_accuracy(eng.broker)
        self.assertEqual(acc["TITAN"]["REJECT"]["target_rate_pct"], 100.0)
        self.assertEqual(acc["ORION"]["SUPPORTIVE"]["resolved"], 1)

    def test_taken_trade_shadow_matches_position(self):
        eng = self.engine(self.answers())
        eng.decide()
        sh = eng.broker.open_shadows()[0]
        self.assertEqual(sh["taken"], 1)
        st = sh["stop"]
        self.cur["c"] = self.candles + [Candle(sh["ts"] + 60, st + 0.1, st + 0.2, st - 1, st - 0.5, 1)]
        ev = eng.monitor()
        self.assertIn("STOP", [e["reason"] for e in ev if e["book"] == "DESK"])
        self.assertEqual(eng.broker.shadows()[0]["status"], "STOP")
        self.assertTrue(any("PAPER kapandı" in m for m in self.notifier.sent))

    def test_shadow_expires_mark_to_market(self):
        with tempfile.TemporaryDirectory() as d:
            b = PaperBroker(f"{d}/x.sqlite", 10000)
            b.add_shadow(1, "m", 100, 95, 110, 0, "WAIT", {"NOVA": "WAIT"})
            sh = b.open_shadows()[0]
            c = [Candle(sh["ts"] + 10, 100, 101, 99, 102, 1)]
            self.assertIsNone(b.resolve_shadow(sh, c, 72, now=sh["ts"] + 3600))
            st, r = b.resolve_shadow(sh, c, 72, now=sh["ts"] + 73 * 3600)
            self.assertEqual(st, "EXPIRED")
            self.assertAlmostEqual(r, 0.4)


class TestEvalCostNotify(Base):
    def test_eval_insufficient_sample_and_comparison(self):
        eng = self.engine(self.answers())
        rep = E.full_report(eng.broker, eng.base)
        self.assertIn("INSUFFICIENT SAMPLE", rep["verdict"])
        d = {"closed_trades": 25, "max_drawdown_pct": 2}
        good = [3.0] * 12 + [-1.0] * 13          # mean +0.92 R
        bad = [3.0] * 5 + [-1.0] * 20            # mean -0.2 R
        noisy = [3.0] * 7 + [-1.0] * 18          # mean +0.12 R
        self.assertIn("Desk ahead", E.verdict(d, d, E.bootstrap_diff(good, bad)))
        self.assertIn("WORSE", E.verdict(d, d, E.bootstrap_diff(bad, good)))
        self.assertIn("NO SIGNIFICANT", E.verdict(d, d, E.bootstrap_diff(noisy, bad)))

    def test_book_metrics_drawdown(self):
        with tempfile.TemporaryDirectory() as d:
            b = PaperBroker(f"{d}/x.sqlite", 1000)
            for exit_px in (110, 90, 90):
                b.open("m", "M", 100, 1, 95, 110, 0, 0, 1)
                b.close(b.open_positions()[0], exit_px, "X")
            m = E.book_metrics(b)
            self.assertEqual(m["closed_trades"], 3)
            self.assertAlmostEqual(m["net_pnl_usd"], -10)
            self.assertAlmostEqual(m["max_drawdown_pct"], 20 / 1010 * 100, places=2)
            self.assertAlmostEqual(m["profit_factor"], 0.5)

    def test_usage_logged_and_priced_only_when_configured(self):
        class PricedLLM(FakeLLM):
            def ask_json(self, system, user, agent=None):
                out = super().ask_json(system, user, agent)
                self._q = getattr(self, "_q", []) + [{"agent": self.calls[-1], "model": "m1",
                                                      "input_tokens": 1000, "output_tokens": 200}]
                return out

            def drain_calls(self):
                q, self._q = getattr(self, "_q", []), []
                return q
        eng = self.engine(self.answers())
        eng.llm = eng.desk.llm = PricedLLM(self.answers())
        eng.decide()
        sp = E.llm_spend(eng.broker, None)
        self.assertEqual(sp["input_tokens"], 6000)
        self.assertIsNone(sp["usd_total"])
        sp = E.llm_spend(eng.broker, {"m1": {"input": 3, "output": 15}})
        self.assertAlmostEqual(sp["usd_total"], 6 * (0.003 + 0.003))

    def test_cryptopanic_merged_and_trade_notified(self):
        cp = lambda cur: [{"title": f"{cur[0]} ETF inflows", "link": "https://cp/1",  # noqa: E731
                           "published_ts": int(time.time()) - 60, "source": "cryptopanic/x",
                           "summary": "", "votes": {"positive": 5}}]
        eng = self.engine(self.answers(), cp=cp)
        rep = eng.decide()
        titles = [n["title"] for n in rep["source_pack"]["news"]["items"]]
        self.assertIn("SOL ETF inflows", titles)
        self.assertEqual(rep["source_pack"]["sentiment"]["futures_positioning"]
                         ["open_interest_change_24h_pct"], 3.2)
        self.assertTrue(any("PAPER TRADE" in m for m in self.notifier.sent))

    def test_daily_due_once_per_day(self):
        eng = self.engine(self.answers())
        t = time.mktime(time.strptime("2026-10-13 19:00", "%Y-%m-%d %H:%M")) - time.timezone
        self.assertFalse(eng.daily_due(18, now=t - 2 * 3600))
        self.assertTrue(eng.daily_due(18, now=t))
        self.assertFalse(eng.daily_due(18, now=t + 60))

    def test_dashboard_renders(self):
        eng = self.engine(self.answers(TITAN={"final_verdict": "REJECT"}))
        eng.decide()
        html = build_html(E.full_report(eng.broker, eng.base))
        self.assertIn("<svg", html)
        self.assertIn("INSUFFICIENT SAMPLE", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))


if __name__ == "__main__":
    unittest.main()
