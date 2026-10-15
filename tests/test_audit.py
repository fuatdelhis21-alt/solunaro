"""Audit hardening: numeric validation, data quality, entry timing, restart safety, ops metrics.
Offline only: no network, no API key, no paid LLM call."""
import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_desk  # noqa: E402
from test_desk import candles, invented_level, load_yaml  # noqa: E402

from desk import data as D  # noqa: E402
from desk import evaluate as E  # noqa: E402
from desk import risk  # noqa: E402
from desk.indicators import Candle, summarize  # noqa: E402
from desk.paper import PaperBroker  # noqa: E402

NAN, INF = float("nan"), float("inf")


class TestRiskNumeric(unittest.TestCase):
    def setUp(self):
        self.cfg = load_yaml()["paper"]
        self.s = summarize(candles())
        self.acct = {"balance": 10000, "open_positions": 0, "trades_today": 0, "pnl_today": 0,
                     "in_cooldown": False, "staleness_min": 5, "max_staleness_min": 90}
        last, a = self.s["last_close"], self.s["atr14"]
        self.good = {"stop": last - a, "target": last + 3 * a}

    def ev(self, setup=None, price=None, acct=None, fee=10, slip=0):
        return risk.evaluate(setup or self.good, self.s["last_close"] if price is None else price,
                             self.s, self.cfg, acct or self.acct, fee, slip)

    def test_good_setup_still_passes(self):
        self.assertTrue(self.ev()["all_pass"])

    def test_non_finite_zero_negative_inputs_rejected_without_crash(self):
        last = self.s["last_close"]
        for bad in (NAN, INF, -INF, 0, -5):
            for setup, price in (({**self.good, "stop": bad}, last),
                                 ({**self.good, "target": bad}, last),
                                 (self.good, bad)):
                r = self.ev(setup, price)
                self.assertFalse(r["all_pass"], (bad, setup, price))
                self.assertIn("inputs_valid", risk.failed(r))
        self.assertIn("inputs_valid", risk.failed(self.ev(fee=-1)))
        self.assertIn("inputs_valid", risk.failed(self.ev(slip=NAN)))

    def test_unknown_account_values_fail_closed(self):
        for k, v in (("balance", NAN), ("balance", 0), ("pnl_today", NAN), ("staleness_min", None)):
            r = self.ev(acct={**self.acct, k: v})
            self.assertFalse(r["all_pass"], k)
            self.assertIn("account_valid", risk.failed(r))

    def test_reward_risk_is_net_of_costs(self):
        last, a = self.s["last_close"], self.s["atr14"]
        setup = {"stop": last - a, "target": last + 2 * a}          # gross R:R exactly 2.0
        self.assertTrue(math.isclose((setup["target"] - last) / (last - setup["stop"]), 2.0))
        r = self.ev(setup)
        self.assertLess(r["reward_risk"], 2.0)
        self.assertIn("reward_risk", risk.failed(r))

    def test_max_loss_includes_costs_and_never_exceeds_limit(self):
        r = self.ev(slip=50)
        limit = self.acct["balance"] * self.cfg["max_loss_pct_per_trade"] / 100
        self.assertLessEqual(r["max_loss_usd"], limit + 0.01)
        self.assertLessEqual(r["notional"], self.acct["balance"] * self.cfg["max_position_pct"] / 100 + 0.01)

    def test_setup_valid_ignores_account_blocks_but_not_fabrication(self):
        blocked = self.ev(acct={**self.acct, "in_cooldown": True})
        self.assertFalse(blocked["all_pass"])
        self.assertTrue(risk.setup_valid(blocked))
        fake = self.ev({**self.good, "stop": invented_level(self.s)})
        self.assertFalse(risk.setup_valid(fake))


class TestCandleQuality(unittest.TestCase):
    def c(self, ts, o=10, h=11, l=9, c=10, v=1):
        return Candle(ts, o, h, l, c, v)

    def test_invalid_candles_fail_closed(self):
        for bad in (self.c(0, o=NAN), self.c(0, h=INF), self.c(0, l=0), self.c(0, c=-1),
                    self.c(0, v=-1), self.c(0, h=8, l=9), self.c(0, h=9.5), self.c(0, l=10.5)):
            with self.assertRaises(D.DataError):
                D.clean_candles([self.c(-3600), bad], "t")

    def test_duplicates_and_order_fixed_gaps_counted(self):
        out = D.clean_candles([self.c(7200), self.c(0), self.c(7200, c=10.5), self.c(3600)], "t")
        self.assertEqual([x.ts for x in out], [0, 3600, 7200])
        self.assertEqual(out[-1].close, 10.5)
        self.assertEqual(D.gap_count([self.c(0), self.c(3600 * 4)], 3600), 3)

    def test_binance_bad_row_rejected(self):
        now = int(time.time()) // 3600 * 3600
        rows = [[(now - 7200) * 1000, "1", "0.5", "0.9", "1.5", "10", (now - 3601) * 1000,
                 "0", 5, "6", "0", "0"]]                               # high < close
        with mock.patch.object(D, "_get", return_value=mock.Mock(json=lambda: rows)):
            with self.assertRaises(D.DataError):
                D.binance_klines("SOLUSDT", "1h", 1)

    def test_dexscreener_wrong_pool_or_chain_rejected(self):
        for pair in ({"chainId": "ethereum", "pairAddress": "P"},
                     {"chainId": "solana", "pairAddress": "OTHER"}):
            with mock.patch.object(D, "_get", return_value=mock.Mock(json=lambda p=pair: {"pairs": [p]})):
                with self.assertRaises(D.DataError):
                    D.dexscreener_pool("solana", "P")


class Resp:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}


class TestHttpRetry(unittest.TestCase):
    def test_429_then_ok(self):
        seq = [Resp(429, {"Retry-After": "1"}), Resp(200)]
        with mock.patch.object(D.requests, "get", side_effect=seq) as g, \
                mock.patch.object(D.time, "sleep") as sl:
            self.assertEqual(D._get("u").status_code, 200)
        self.assertEqual(g.call_count, 2)
        sl.assert_called_once()

    def test_bounded_retries_then_error(self):
        with mock.patch.object(D.requests, "get", return_value=Resp(503)) as g, \
                mock.patch.object(D.time, "sleep"):
            with self.assertRaises(D.DataError):
                D._get("u", retries=2)
        self.assertEqual(g.call_count, 3)

    def test_client_error_not_retried_and_timeout_bounded(self):
        with mock.patch.object(D.requests, "get", return_value=Resp(404)) as g:
            with self.assertRaises(D.DataError):
                D._get("u")
        self.assertEqual(g.call_count, 1)
        with mock.patch.object(D.requests, "get", side_effect=D.requests.Timeout()) as g, \
                mock.patch.object(D.time, "sleep"):
            with self.assertRaises(D.DataError):
                D._get("u", retries=1)
        self.assertEqual(g.call_count, 2)


class TestBrokerSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = f"{self.tmp.name}/d.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_candle_after_entry_is_checked(self):
        """Regression: entry at candle T's close used to skip candle T+1 when the process opened
        the position a few minutes after T+1 started (opened_ts > candle ts)."""
        b = PaperBroker(self.db, 10000)
        t = int(time.time()) // 3600 * 3600 - 3600                    # candle that started 1h ago
        pid = b.open("m", "M", 100.0, 1, 95.0, 110.0, 0, 0, 1, entry_ts=t)
        pos = b.open_positions()[0]
        self.assertGreater(pos["opened_ts"], t)
        self.assertEqual(b.check_exits(pos, [Candle(t, 100, 101, 94, 96, 1)])[0], "STOP")
        self.assertIsNone(b.check_exits(pos, [Candle(t - 3600, 100, 101, 90, 96, 1)]))
        self.assertEqual(pid, pos["id"])

    def test_close_is_idempotent(self):
        b = PaperBroker(self.db, 10000)
        b.open("m", "M", 100.0, 10, 95.0, 110.0, 0, 0, 1)
        pos = b.open_positions()[0]
        first = b.close(pos, 95.0, "STOP")
        self.assertEqual(b.close(pos, 50.0, "STOP"), 0.0)          # stale row, second process
        self.assertAlmostEqual(b.balance(), 10000 + first)

    def test_limits_survive_restart(self):
        b = PaperBroker(self.db, 10000)
        b.open("m", "M", 100.0, 10, 95.0, 110.0, 0, 0, 1)
        b.close(b.open_positions()[0], 95.0, "STOP")
        b.open("m2", "M2", 100.0, 1, 95.0, 110.0, 0, 0, 2)
        b.db.close()
        r = PaperBroker(self.db, 99999)                                # new process, other cfg
        self.assertEqual(len(r.open_positions()), 1)
        self.assertEqual(r.trades_today(), 2)
        self.assertAlmostEqual(r.pnl_today(), -50.0)
        self.assertAlmostEqual(r.balance(), 9950.0)                    # starting balance kept

    def test_old_schema_gets_new_columns(self):
        con = sqlite3.connect(self.db)
        con.executescript("CREATE TABLE positions(id INTEGER PRIMARY KEY, status TEXT);"
                          "INSERT INTO positions(status) VALUES('CLOSED');")
        con.commit()
        con.close()
        b = PaperBroker(self.db, 10000)
        cols = {r["name"] for r in b.db.execute("PRAGMA table_info(positions)")}
        self.assertIn("entry_ts", cols)
        self.assertEqual(b.db.execute("SELECT COUNT(*) c FROM positions").fetchone()["c"], 1)


class TestEngineAudit(unittest.TestCase):
    # reuse the engine fixtures without re-running test_desk's own tests
    make = test_desk.TestEngine.make
    answers = test_desk.TestEngine.answers
    tearDown = test_desk.TestEngine.tearDown

    def test_position_entry_time_is_signal_candle_close(self):
        eng = self.make(self.answers())
        eng.decide()
        pos = eng.broker.open_positions()[0]
        c = candles()
        self.assertEqual(pos["entry_ts"], c[-1].ts + 3600)
        self.assertEqual(eng.broker.open_shadows()[0]["entry_ts"], c[-1].ts + 3600)

    def test_fabricated_setup_not_recorded_as_shadow(self):
        a = self.answers()
        a["VEGA"]["setup"]["stop"] = invented_level(summarize(candles()))
        eng = self.make(a)
        rep = eng.decide()
        self.assertIn("stop_grounded_in_data", rep["failed_checks"])
        self.assertEqual(len(eng.broker.shadows()), 0)

    def test_account_blocked_valid_setup_is_shadowed_and_reason_is_machine_readable(self):
        eng = self.make(self.answers())
        eng.decide()                                                   # opens, starts cooldown
        eng.broker.close(eng.broker.open_positions()[0], 100.0, "TEST")
        rep = eng.decide()
        self.assertEqual(rep["result"], "REJECT")
        self.assertIn("cooldown", rep["failed_checks"])
        self.assertEqual(len(eng.broker.shadows()), 2)
        ops = E.operations(eng.broker)
        self.assertEqual(ops["overridden_trade_failed_checks"].get("cooldown"), 1)
        self.assertIsNotNone(ops["last_data_ok_ts"])

    def test_llm_latency_reported(self):
        eng = self.make(self.answers())
        eng.llm.drain_calls = lambda: [{"agent": "NOVA", "model": "m", "input_tokens": 1,
                                        "output_tokens": 1, "latency_ms": 1200}]
        eng.decide()
        lat = E.operations(eng.broker)["llm_latency_ms"]["NOVA"]
        self.assertEqual((lat["n"], lat["p50"]), (1, 1200))


class TestProcessLock(unittest.TestCase):
    def test_second_process_refused(self):
        import run
        with tempfile.TemporaryDirectory() as d:
            cfg = {"storage": {"db_path": f"{d}/data/d.sqlite"}}
            h = run.acquire_lock(cfg)
            # flock is per open file description: a second open() conflicts like another process
            with self.assertRaises(SystemExit):
                run.acquire_lock(cfg)
            h.close()
            run.acquire_lock(cfg).close()


if __name__ == "__main__":
    unittest.main()
