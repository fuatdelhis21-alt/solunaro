"""SQLite paper broker + decision journal. No exchange connection exists in this project."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, market_id TEXT, label TEXT, opened_ts INTEGER, entry REAL, qty REAL,
  stop REAL, target REAL, fee_bps REAL, slippage_bps REAL, status TEXT,
  closed_ts INTEGER, exit REAL, exit_reason TEXT, pnl_usd REAL, decision_id INTEGER);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, ts INTEGER, market_id TEXT, nova TEXT, titan TEXT,
  code_checks_pass INTEGER, reason TEXT, report_path TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS shadows(
  id INTEGER PRIMARY KEY, decision_id INTEGER, ts INTEGER, market_id TEXT, entry REAL,
  stop REAL, target REAL, taken INTEGER, decision TEXT, verdicts TEXT, status TEXT,
  resolved_ts INTEGER, exit REAL, r_multiple REAL);
CREATE TABLE IF NOT EXISTS llm_usage(
  id INTEGER PRIMARY KEY, ts INTEGER, decision_id INTEGER, agent TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER);
"""


def day_start(ts: int | None = None) -> int:
    d = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class PaperBroker:
    def __init__(self, db_path: str, starting_balance: float):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # migrate older databases (additive only, existing rows are kept)
        for table, col, typ in (("decisions", "experiment", "TEXT"),
                                ("positions", "entry_ts", "INTEGER"),
                                ("shadows", "entry_ts", "INTEGER"),
                                ("llm_usage", "latency_ms", "INTEGER")):
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()
        self.experiment: str | None = None     # written on new decisions
        self.scope: str | None = None          # if set, eval queries only this experiment
        if self._meta("starting_balance") is None:
            self._set_meta("starting_balance", str(starting_balance))

    def _meta(self, k):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def _set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
        self.db.commit()

    # ---- account
    def balance(self) -> float:
        start = float(self._meta("starting_balance"))
        realized = self.db.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) s FROM positions WHERE status='CLOSED'").fetchone()["s"]
        return start + realized

    def open_positions(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()

    def trades_today(self) -> int:
        return self.db.execute("SELECT COUNT(*) c FROM positions WHERE opened_ts>=?",
                               (day_start(),)).fetchone()["c"]

    def pnl_today(self) -> float:
        return self.db.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) s FROM positions WHERE status='CLOSED' AND closed_ts>=?",
            (day_start(),)).fetchone()["s"]

    def in_cooldown(self, market_id: str, minutes: float) -> bool:
        r = self.db.execute("SELECT MAX(ts) t FROM decisions WHERE market_id=? AND nova='TRADE'",
                            (market_id,)).fetchone()
        return bool(r["t"]) and time.time() - r["t"] < minutes * 60

    # ---- journal
    def log_decision(self, market_id, nova, titan, code_pass, reason, report_path) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions(ts,market_id,nova,titan,code_checks_pass,reason,report_path,"
            "experiment) VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), market_id, nova, titan, int(code_pass), reason, report_path,
             self.experiment))
        self.db.commit()
        return cur.lastrowid

    # ---- shadows: counterfactual outcome of EVERY grounded setup, taken or not
    def add_shadow(self, decision_id, market_id, entry, stop, target, taken, decision, verdicts,
                   entry_ts: int | None = None):
        self.db.execute(
            "INSERT INTO shadows(decision_id,ts,market_id,entry,stop,target,taken,decision,"
            "verdicts,status,entry_ts) VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?)",
            (decision_id, int(time.time()), market_id, entry, stop, target, int(taken), decision,
             json.dumps(verdicts), entry_ts))
        self.db.commit()

    def open_shadows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM shadows WHERE status='OPEN'").fetchall()

    def resolve_shadow(self, sh: sqlite3.Row, candles, max_hours: float, now: int | None = None):
        """Same exit logic as real paper positions; EXPIRED is marked to market."""
        now = now or int(time.time())
        hit = self.check_exits(sh, candles)
        if hit:
            status, px, ts = hit
        elif now - sh["ts"] >= max_hours * 3600 and candles:
            status, px, ts = "EXPIRED", candles[-1].close, now
        else:
            return None
        r = (px - sh["entry"]) / (sh["entry"] - sh["stop"]) if sh["entry"] > sh["stop"] else 0
        self.db.execute("UPDATE shadows SET status=?, resolved_ts=?, exit=?, r_multiple=? WHERE id=?",
                        (status, ts, px, r, sh["id"]))
        self.db.commit()
        return status, r

    def _scoped(self, table: str, extra: str = "", params: tuple = ()) -> list[sqlite3.Row]:
        """Rows of `table` joined to their decision, filtered by self.scope when set."""
        q = f"SELECT t.* FROM {table} t LEFT JOIN decisions d ON d.id = t.decision_id WHERE 1=1"
        if self.scope:
            q += " AND d.experiment = ?"
            params = (self.scope,) + params
        return self.db.execute(q + extra, params).fetchall()

    def shadows(self) -> list[sqlite3.Row]:
        return self._scoped("shadows")

    def experiments(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT experiment, MIN(ts) first_ts, MAX(ts) last_ts, COUNT(*) n "
                               "FROM decisions GROUP BY experiment ORDER BY first_ts").fetchall()

    # ---- LLM usage
    def log_usage(self, decision_id, calls: list[dict]):
        for c in calls:
            self.db.execute("INSERT INTO llm_usage(ts,decision_id,agent,model,input_tokens,"
                            "output_tokens) VALUES(?,?,?,?,?,?)",
                            (c.get("ts", int(time.time())), decision_id, c.get("agent"),
                             c.get("model"), c.get("input_tokens", 0), c.get("output_tokens", 0)))
            if c.get("latency_ms") is not None:
                self.db.execute("UPDATE llm_usage SET latency_ms=? WHERE id=last_insert_rowid()",
                                (int(c["latency_ms"]),))
        self.db.commit()

    def usage_rows(self) -> list[sqlite3.Row]:
        q = ("SELECT u.agent, u.model, COUNT(*) calls, SUM(u.input_tokens) inp, "
             "SUM(u.output_tokens) outp FROM llm_usage u LEFT JOIN decisions d ON d.id=u.decision_id")
        args = ()
        if self.scope:
            q += " WHERE d.experiment = ?"
            args = (self.scope,)
        return self.db.execute(q + " GROUP BY u.agent, u.model", args).fetchall()

    def closed_positions(self) -> list[sqlite3.Row]:
        return self._scoped("positions", " AND t.status='CLOSED' ORDER BY t.closed_ts")

    def decisions(self, since: int = 0) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM decisions WHERE ts>=?", (since,)
        if self.scope:
            q += " AND experiment=?"
            args += (self.scope,)
        return self.db.execute(q + " ORDER BY ts", args).fetchall()

    def get_meta(self, k):
        return self._meta(k)

    def put_meta(self, k, v):
        self._set_meta(k, str(v))

    def set_report_path(self, decision_id: int, path: str):
        self.db.execute("UPDATE decisions SET report_path=? WHERE id=?", (path, decision_id))
        self.db.commit()

    # ---- orders
    def open(self, market_id, label, entry, qty, stop, target, fee_bps, slippage_bps,
             decision_id, entry_ts: int | None = None) -> int:
        """`entry_ts` = close time of the candle whose close is the entry price. Exits are
        checked from the candle that opens at that time, exactly like the backtest."""
        cur = self.db.execute(
            "INSERT INTO positions(market_id,label,opened_ts,entry,qty,stop,target,fee_bps,"
            "slippage_bps,status,decision_id,entry_ts) VALUES(?,?,?,?,?,?,?,?,?, 'OPEN', ?, ?)",
            (market_id, label, int(time.time()), entry, qty, stop, target, fee_bps,
             slippage_bps, decision_id, entry_ts))
        self.db.commit()
        return cur.lastrowid

    def close(self, pos: sqlite3.Row, raw_exit: float, reason: str, ts: int | None = None) -> float:
        exit_px = raw_exit * (1 - pos["slippage_bps"] / 10000)
        fees = (pos["entry"] + exit_px) * pos["qty"] * pos["fee_bps"] / 10000
        pnl = (exit_px - pos["entry"]) * pos["qty"] - fees
        # status guard: a stale row (e.g. a second process) can never close or book PnL twice
        cur = self.db.execute("UPDATE positions SET status='CLOSED', closed_ts=?, exit=?, "
                              "exit_reason=?, pnl_usd=? WHERE id=? AND status='OPEN'",
                              (ts or int(time.time()), exit_px, reason, pnl, pos["id"]))
        self.db.commit()
        return pnl if cur.rowcount else 0.0

    def check_exits(self, pos: sqlite3.Row, candles) -> tuple[str, float, int] | None:
        """Walk closed candles that opened at/after the entry time. If one candle touches both
        stop and target, assume the stop hit first (conservative). Gaps fill at the open."""
        keys = pos.keys()
        start = (pos["entry_ts"] if "entry_ts" in keys and pos["entry_ts"] is not None
                 else pos["opened_ts"] if "opened_ts" in keys else pos["ts"])
        for c in candles:
            if c.ts < start:
                continue
            if c.low <= pos["stop"]:
                return "STOP", min(pos["stop"], c.open), c.ts
            if c.high >= pos["target"]:
                return "TARGET", max(pos["target"], c.open), c.ts
        return None

    def summary(self) -> dict:
        rows = self.db.execute("SELECT * FROM positions WHERE status='CLOSED'").fetchall()
        wins = [r for r in rows if r["pnl_usd"] > 0]
        dec = self.db.execute("SELECT nova, COUNT(*) c FROM decisions GROUP BY nova").fetchall()
        return {"balance": round(self.balance(), 2), "closed_trades": len(rows),
                "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else None,
                "realized_pnl": round(sum(r["pnl_usd"] for r in rows), 2),
                "open": [dict(r) for r in self.open_positions()],
                "decisions": {r["nova"]: r["c"] for r in dec}}


def save_report(reports_dir: str, payload: dict) -> str:
    os.makedirs(reports_dir, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(reports_dir, f"{name}.json")
    with open(path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    return path
