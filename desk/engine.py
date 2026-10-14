"""Orchestrates monitoring, the LLM-free baseline book and the six-agent decision cycle."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import agents as A
from . import baseline as B
from . import data as D
from . import risk
from .agents import Desk
from .indicators import summarize
from .notify import Notifier
from .paper import PaperBroker, save_report

log = logging.getLogger("desk")

DEFAULT_BASELINE = {"enabled": True, "trend_rsi_min": 45, "trend_rsi_max": 70,
                    "oversold_enabled": True, "oversold_rsi": 32,
                    "stop_atr": 1, "target_atr": 3}


def experiment_id(cfg: dict) -> str:
    """Fingerprint of everything that changes decisions. Editing prompts, risk rules, models,
    the baseline or prefilter starts a NEW experiment, so eval never mixes results across them."""
    blob = json.dumps({
        "prompts": [A.ATLAS, A.VEGA, A.ORION, A.LUNA, A.TITAN, A.NOVA],
        "paper": cfg.get("paper"), "baseline": {**DEFAULT_BASELINE, **(cfg.get("baseline") or {})},
        "prefilter": cfg.get("prefilter"), "model": (cfg.get("llm") or {}).get("model"),
        "models": (cfg.get("llm") or {}).get("models"), "effort": (cfg.get("llm") or {}).get("effort"),
        "markets": cfg.get("markets"),
        "min_news": (cfg.get("data") or {}).get("min_news_items"),
    }, sort_keys=True, default=str)
    name = cfg.get("experiment_name") or "exp"
    return f"{name}-{hashlib.sha1(blob.encode()).hexdigest()[:8]}"


def min_news_for(dcfg: dict, kind: str) -> int:
    """`min_news_items` is either one number for all markets or a per-kind map,
    e.g. {crypto: 1, dex: 0}: Solana pool tokens rarely appear in mainstream RSS."""
    v = dcfg.get("min_news_items", 0)
    return int(v.get(kind, 0)) if isinstance(v, dict) else int(v)


class Engine:
    def __init__(self, cfg: dict, llm, fetch=None, news_fetch=None, fng_fetch=None,
                 positioning_fetch=None, cryptopanic_fetch=None, notifier=None):
        self.cfg = cfg
        self.desk = Desk(llm)
        self.llm = llm
        st = cfg["storage"]
        self.broker = PaperBroker(st["db_path"], cfg["paper"]["starting_balance_usd"])
        base_db = st.get("baseline_db_path") or st["db_path"].replace(".sqlite", "_baseline.sqlite")
        self.base = PaperBroker(base_db, cfg["paper"]["starting_balance_usd"])
        self.bcfg = {**DEFAULT_BASELINE, **(cfg.get("baseline") or {})}
        self.experiment = experiment_id(cfg)
        self.broker.experiment = self.base.experiment = self.experiment
        self.fetch = fetch or self._fetch_market          # injectable for tests
        self.news_fetch = news_fetch or (lambda: D.fetch_news(
            cfg["news_feeds"], cfg["data"]["news_max_age_hours"]))
        self.fng_fetch = fng_fetch or D.fear_greed
        self.positioning_fetch = positioning_fetch or D.binance_futures_positioning
        cp = cfg.get("cryptopanic") or {}
        token = os.environ.get("CRYPTOPANIC_TOKEN")
        self.cryptopanic_fetch = cryptopanic_fetch or (
            (lambda cur: D.cryptopanic_news(cp["url"], token, cur, cfg["data"]["news_max_age_hours"]))
            if cp.get("enabled") and token else (lambda cur: []))
        self.notify = notifier or Notifier()
        self._cache: dict[str, D.MarketSnapshot] = {}

    # ------------------------------------------------------------ markets
    def market_configs(self) -> dict[str, tuple[str, dict]]:
        m, out = self.cfg["markets"], {}
        if m.get("crypto", {}).get("enabled"):
            for s in m["crypto"]["symbols"]:
                out[f"crypto:{s['symbol']}"] = ("crypto", s)
        if m.get("dex", {}).get("enabled"):
            for p in m["dex"]["pools"]:
                out[f"dex:{m['dex']['chain']}:{p['address']}"] = ("dex", p)
        return out

    def _fetch_market(self, kind: str, item: dict) -> D.MarketSnapshot:
        mcfg = self.cfg["markets"][kind]
        return D.crypto_snapshot(item, mcfg) if kind == "crypto" else D.dex_snapshot(item, mcfg)

    def snapshot(self, market_id: str) -> D.MarketSnapshot | None:
        """Fetch once per pass (cache cleared at the start of monitor/decide)."""
        if market_id in self._cache:
            return self._cache[market_id]
        kind, item = self.market_configs().get(market_id, (None, None))
        if not kind:
            return None
        s = self.fetch(kind, item)
        self._cache[market_id] = s
        return s

    def kill_switch(self) -> bool:
        return os.path.exists(self.cfg["storage"]["kill_switch_file"])

    # ------------------------------------------------------------ monitor
    def monitor(self) -> list[dict]:
        self._cache = {}
        events = []
        for book_name, book in (("DESK", self.broker), ("BASELINE", self.base)):
            for pos in book.open_positions():
                try:
                    snap = self.snapshot(pos["market_id"])
                except Exception as e:  # noqa: BLE001
                    log.error("monitor fetch failed for %s: %s", pos["market_id"], e)
                    continue
                if snap is None:
                    log.warning("open position %s has no market config", pos["market_id"])
                    continue
                hit = book.check_exits(pos, snap.candles)
                if hit:
                    reason, px, ts = hit
                elif self.kill_switch():
                    reason, px, ts = "KILL_SWITCH", snap.last_price, None
                else:
                    continue
                pnl = book.close(pos, px, reason, ts)
                ev = {"book": book_name, "position": pos["id"], "label": pos["label"],
                      "reason": reason, "exit": px, "pnl": round(pnl, 2)}
                events.append(ev)
                log.info("CLOSED %s #%s %s %s @ %s pnl=%.2f", book_name, pos["id"], pos["label"],
                         reason, px, pnl)
                if book_name == "DESK":
                    self.notify.send(f"📄 PAPER kapandı #{pos['id']} {pos['label']} {reason}\n"
                                     f"çıkış {px:.6g} | PnL {pnl:+.2f} USD | "
                                     f"bakiye {self.broker.balance():.2f}")
        self._resolve_shadows()
        return events

    def _resolve_shadows(self):
        max_h = self.cfg["data"].get("shadow_max_hours", 72)
        for sh in self.broker.open_shadows():
            try:
                snap = self.snapshot(sh["market_id"])
            except Exception as e:  # noqa: BLE001
                log.error("shadow fetch failed %s: %s", sh["market_id"], e)
                continue
            if snap:
                self.broker.resolve_shadow(sh, snap.candles, max_h)

    # ------------------------------------------------------------ collect
    def collect(self) -> tuple[dict, list[dict]]:
        dcfg = self.cfg["data"]
        snaps, overview = {}, []
        for mid in self.market_configs():
            try:
                s = self.snapshot(mid)
                if len(s.candles) < 30:
                    raise D.DataError(f"only {len(s.candles)} closed candles")
            except Exception as e:  # noqa: BLE001
                overview.append({"market_id": mid, "missing": [f"fetch failed: {e}"],
                                 "eligible": False, "ineligible_reasons": ["fetch failed"]})
                continue
            snaps[mid] = s
            summ = summarize(s.candles)
            why = []
            stale = s.staleness_minutes()
            if stale > dcfg["max_staleness_minutes"]:
                why.append(f"stale data ({stale:.0f} min)")
            if s.kind == "dex":
                dx, pool = self.cfg["markets"]["dex"], s.extra.get("pool")
                if not pool:
                    why.append("pool stats missing")
                else:
                    if (pool.get("liquidity_usd") or 0) < dx["min_liquidity_usd"]:
                        why.append("liquidity below minimum")
                    if ((pool.get("volume") or {}).get("h24") or 0) < dx["min_volume_24h_usd"]:
                        why.append("24h volume below minimum")
            sig = B.signal(summ, self.bcfg)
            tradable = not why                      # data/liquidity OK (baseline uses this)
            if self.cfg.get("prefilter", {}).get("enabled", True) and not sig:
                why.append("prefilter: no mechanical long candidate (no LLM call)")
            compact = {k: v for k, v in summ.items() if k != "last_12_candles"}
            overview.append({"market_id": mid, "kind": s.kind, "label": s.label,
                             "indicators": compact, "extra": s.extra, "missing": s.missing,
                             "staleness_min": round(stale, 1), "eligible": not why,
                             "tradable": tradable, "baseline_signal": sig,
                             "ineligible_reasons": why})
        return snaps, overview

    # ------------------------------------------------------------ baseline book
    def baseline_step(self, snaps: dict, overview: list[dict]) -> list[dict]:
        if not self.bcfg.get("enabled", True):
            return []
        p, dcfg, opened = self.cfg["paper"], self.cfg["data"], []
        for o in overview:
            sig = o.get("baseline_signal")
            if not sig or not o.get("tradable"):
                continue
            s = snaps[o["market_id"]]
            summ = summarize(s.candles)
            account = self._account(self.base, o["market_id"], s)
            rk = risk.evaluate(sig, s.last_price, summ, p, account, s.fee_bps, s.slippage_bps)
            ok = rk.get("all_pass")
            did = self.base.log_decision(o["market_id"], "TRADE" if ok else "SKIP", sig["rule"],
                                         ok, "; ".join(c["issue"] for c in rk["checks"]
                                                       if c["result"] == "FAIL"), "")
            if ok:
                pid = self.base.open(o["market_id"], s.label, rk["entry"], rk["qty"], rk["stop"],
                                     rk["target"], s.fee_bps, s.slippage_bps, did)
                opened.append({"position_id": pid, "market_id": o["market_id"], "rule": sig["rule"]})
                log.info("BASELINE OPEN #%s %s %s", pid, s.label, sig["rule"])
        return opened

    def _account(self, book, market_id, snap) -> dict:
        p, dcfg = self.cfg["paper"], self.cfg["data"]
        return {"balance": book.balance(), "open_positions": len(book.open_positions()),
                "trades_today": book.trades_today(), "pnl_today": book.pnl_today(),
                "in_cooldown": book.in_cooldown(market_id, p["cooldown_minutes_per_market"]),
                "staleness_min": snap.staleness_minutes(),
                "max_staleness_min": dcfg["max_staleness_minutes"]}

    # ------------------------------------------------------------ decide
    def decide(self) -> dict:
        self._cache = {}
        p, dcfg = self.cfg["paper"], self.cfg["data"]
        report: dict = {"ts": int(time.time()), "mode": "PAPER"}
        if self.kill_switch():
            report["result"] = "SKIPPED: kill switch file present"
            return report

        snaps, overview = self.collect()
        report["overview"] = overview
        report["experiment"] = self.experiment
        self._health_fetch(bool(snaps), overview)
        report["baseline_opened"] = self.baseline_step(snaps, overview)

        if len(self.broker.open_positions()) >= p["max_open_positions"]:
            report["result"] = "SKIPPED: max open positions reached (monitoring only)"
            return report
        eligible = {o["market_id"] for o in overview if o.get("eligible")}
        if not eligible:
            report["result"] = "NO DECISION: no eligible market (data/liquidity/prefilter)"
            return self._finish(report, None, "NO_MARKET", "-", False, report["result"])

        try:
            return self._llm_cycle(report, snaps, overview, eligible)
        except Exception as e:  # noqa: BLE001 — LLM/network failure: journal it, never trade
            log.exception("LLM cycle failed")
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            report["result"] = "ERROR: " + msg
            n = int(self.broker.get_meta("llm_error_streak") or 0) + 1
            self.broker.put_meta("llm_error_streak", n)
            if n in (1, 3) or n % 12 == 0:
                self.notify.send(f"⚠️ LLM döngüsü başarısız ({n}. kez üst üste): {msg}")
            return self._finish(report, None, "ERROR", "-", False, msg)

    def _llm_cycle(self, report, snaps, overview, eligible) -> dict:
        p, dcfg = self.cfg["paper"], self.cfg["data"]
        # ATLAS only sees eligible markets — cheaper and it cannot pick a filtered one
        atlas = self.desk.atlas([o for o in overview if o.get("eligible")])
        report["ATLAS"] = atlas
        pick = atlas.get("top_pick")
        if pick not in eligible:
            msg = f"ATLAS top_pick={pick!r} not an eligible WATCH market"
            report["result"] = "NO DECISION: " + msg
            return self._finish(report, pick, "NO_PICK", "-", False, msg)

        s = snaps[pick]
        summ = summarize(s.candles)
        price = s.last_price

        # Source packs for VEGA / ORION / LUNA
        all_news, news_err = self._safe(self.news_fetch, ([], ["news fetch failed"]))
        rel_news = D.news_for(s.keywords, all_news)
        cur = self._currency(pick, s)
        cp_items, cp_err = self._safe_single(lambda: self.cryptopanic_fetch([cur] if cur else []))
        seen = {n["title"].lower() for n in rel_news}
        for n in cp_items or []:
            if n["title"].lower() not in seen:
                rel_news.append(n)
                seen.add(n["title"].lower())
        rel_news.sort(key=lambda n: -n["published_ts"])
        rel_news = rel_news[:12]
        fng = fng_err = pos = pos_err = None
        if s.kind == "crypto":
            fng, fng_err = self._safe_single(self.fng_fetch)
            sym = pick.split(":", 1)[1]
            pos, pos_err = self._safe_single(lambda: self.positioning_fetch(sym))
        chart = {"market_id": pick, "label": s.label, "interval_sec": s.interval_sec,
                 "indicators": summ, "missing": s.missing}
        news = {"market_id": pick, "label": s.label, "keywords": s.keywords,
                "items": rel_news, "feed_errors": news_err, "cryptopanic_error": cp_err,
                "note": f"RSS + optional CryptoPanic items from the last "
                        f"{dcfg['news_max_age_hours']}h."}
        senti = {"market_id": pick, "label": s.label, "fear_greed": fng,
                 "fear_greed_error": fng_err,
                 "futures_positioning": pos, "futures_positioning_error": pos_err,
                 "taker_buy_ratio_24": summ.get("taker_buy_ratio_24"),
                 "volume_last_vs_avg20": summ.get("volume_last_vs_avg20"),
                 "change_24_pct": summ.get("change_24_pct"),
                 "ticker_24h": s.extra.get("ticker_24h"),
                 "pool_txns": (s.extra.get("pool") or {}).get("txns"),
                 "pool_volume": (s.extra.get("pool") or {}).get("volume"),
                 "pool_price_change_pct": (s.extra.get("pool") or {}).get("price_change_pct"),
                 "note": "No social-media data is collected by this bot."}
        with ThreadPoolExecutor(3) as ex:
            fv, fo, fl = (ex.submit(self.desk.vega, chart), ex.submit(self.desk.orion, news),
                          ex.submit(self.desk.luna, senti))
            vega, orion, luna = fv.result(), fo.result(), fl.result()
        report.update(VEGA=vega, ORION=orion, LUNA=luna, source_pack={
            "chart": chart, "news": news, "sentiment": senti})

        # Code risk checks, then TITAN
        setup = vega.get("setup") if vega.get("verdict") == "SETUP" else None
        rk = risk.evaluate(setup, price, summ, p, self._account(self.broker, pick, s),
                           s.fee_bps, s.slippage_bps)
        need = min_news_for(dcfg, s.kind)
        if len(rel_news) < need:
            rk["checks"].append({"name": "news_available", "result": "FAIL",
                                 "issue": f"{len(rel_news)} recent news items < {need}"})
            rk["all_pass"] = False
        report["CODE_RISK"] = rk
        reports = {"ATLAS": atlas, "VEGA": vega, "ORION": orion, "LUNA": luna}
        titan = self.desk.titan(p, rk, reports)
        report["TITAN"] = titan

        nova = self.desk.nova({"current_price": price, "market_id": pick, "label": s.label,
                               **reports, "TITAN": titan, "CODE_RISK": rk, "RULES": p})
        report["NOVA"] = nova

        # Final gate — every condition must hold
        t_ver = titan.get("final_verdict")
        n_dec = nova.get("decision")
        go = n_dec == "TRADE" and t_ver == "APPROVE" and rk.get("all_pass")
        reason = nova.get("reason", "")
        if n_dec == "TRADE" and not go:
            reason = f"TRADE overridden by code gate (TITAN={t_ver}, code_pass={rk.get('all_pass')})"
            n_dec = "REJECT"
        did = self.broker.log_decision(pick, n_dec, t_ver, rk.get("all_pass", False), reason, "")

        # Counterfactual: record every geometrically valid setup, taken or not
        if rk.get("entry") and rk.get("stop") is not None and rk["stop"] < rk["entry"] < rk["target"]:
            atlas_v = next((r.get("verdict") for r in atlas.get("ranking", [])
                            if r.get("market_id") == pick), None)
            self.broker.add_shadow(did, pick, rk["entry"], rk["stop"], rk["target"], go, n_dec, {
                "ATLAS": atlas_v, "VEGA": vega.get("verdict"), "ORION": orion.get("verdict"),
                "LUNA": luna.get("verdict"), "TITAN": t_ver, "NOVA": n_dec,
                "CODE": "PASS" if rk.get("all_pass") else "FAIL"})

        if go:
            pid = self.broker.open(pick, s.label, rk["entry"], rk["qty"], rk["stop"], rk["target"],
                                   s.fee_bps, s.slippage_bps, did)
            report["paper_order"] = {"position_id": pid, "entry": rk["entry"], "qty": rk["qty"],
                                     "stop": rk["stop"], "target": rk["target"],
                                     "max_loss_usd": rk["max_loss_usd"]}
            log.info("PAPER OPEN #%s %s entry=%s stop=%s target=%s", pid, s.label, rk["entry"],
                     rk["stop"], rk["target"])
            self.notify.send(
                f"🟢 PAPER TRADE #{pid} {s.label}\n"
                f"giriş {rk['entry']:.6g} | stop {rk['stop']:.6g} | hedef {rk['target']:.6g}\n"
                f"R:R {rk['reward_risk']} | maks zarar {rk['max_loss_usd']} USD\n"
                f"NOVA: {str(nova.get('reason', ''))[:300]}")
        report["result"] = n_dec
        self.broker.put_meta("llm_error_streak", 0)
        report["invalid_outputs"] = {k: v["_invalid"] for k, v in
                                     (("ATLAS", atlas), ("VEGA", vega), ("ORION", orion),
                                      ("LUNA", luna), ("TITAN", titan), ("NOVA", nova))
                                     if isinstance(v, dict) and v.get("_invalid")}
        calls = self._drain()
        report["llm_calls"] = calls
        self.broker.log_usage(did, calls)
        path = save_report(self.cfg["storage"]["reports_dir"], report)
        self.broker.set_report_path(did, path)
        return report

    # ------------------------------------------------------------ daily summary
    def daily_due(self, hour_utc: int, now: float | None = None) -> bool:
        now_dt = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
        today = now_dt.strftime("%Y-%m-%d")
        if now_dt.hour < hour_utc or self.broker.get_meta("last_daily") == today:
            return False
        self.broker.put_meta("last_daily", today)
        return True

    # ------------------------------------------------------------ health
    def _health_fetch(self, any_ok: bool, overview: list[dict]):
        n = 0 if any_ok else int(self.broker.get_meta("fetch_fail_streak") or 0) + 1
        self.broker.put_meta("fetch_fail_streak", n)
        if n == 3 or (n and n % 12 == 0):
            errs = "; ".join(str(o.get("missing"))[:120] for o in overview)[:600]
            self.notify.send(f"⚠️ {n} döngüdür hiçbir piyasadan veri alınamadı. Bot karar vermiyor.\n{errs}")

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _currency(market_id: str, s) -> str | None:
        if market_id.startswith("crypto:"):
            sym = market_id.split(":", 1)[1]
            for q in ("USDT", "USDC", "FDUSD", "BUSD"):
                if sym.endswith(q):
                    return sym[: -len(q)]
            return sym
        base = (s.extra.get("pool") or {}).get("base")
        return base.upper().lstrip("W") if base and base.upper() in ("WSOL", "WETH", "WBTC") else (
            base.upper() if base else None)

    def _drain(self) -> list[dict]:
        fn = getattr(self.llm, "drain_calls", None)
        return fn() if fn else []

    def _finish(self, report, market_id, nova, titan, code_pass, reason):
        calls = self._drain()
        report["llm_calls"] = calls
        path = save_report(self.cfg["storage"]["reports_dir"], report)
        did = self.broker.log_decision(market_id, nova, titan, code_pass, reason, path)
        self.broker.log_usage(did, calls)
        return report

    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.error("fetch failed: %s", e)
            return default

    @staticmethod
    def _safe_single(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001
            return None, str(e)
