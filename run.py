#!/usr/bin/env python3
"""Solunaro — Six Agent AI Trading Desk — PAPER ONLY.

  python run.py once      # one monitor pass + one six-agent decision
  python run.py loop      # run forever (monitor every N min, decide every M min)
  python run.py status    # paper account summary
  python run.py data      # fetch & print source packs only (no LLM, no cost)
  python run.py eval      # desk vs baseline, counterfactuals, agent accuracy, LLM spend
  python run.py dashboard # write data/dashboard.html (and send to Telegram if configured)
  python run.py backtest  # historical test of the LLM-free baseline rule (no LLM, no cost)
  python run.py keycheck  # is ANTHROPIC_API_KEY accepted? (lists models, no tokens, no cost)

eval/dashboard only count the CURRENT experiment (prompts+rules+models fingerprint); add --all
to include every experiment.
"""
import argparse
import json
import logging
import os
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from desk import data as D  # noqa: E402
from desk.indicators import summarize  # noqa: E402


def acquire_lock(cfg):
    """Only one process may trade the paper books (`once` while the service runs would
    otherwise race for the same position slot). Held until the process exits."""
    import fcntl
    path = os.path.join(os.path.dirname(cfg["storage"]["db_path"]), "solunaro.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Başka bir Solunaro süreci çalışıyor (servis açık olabilir). "
                 "Önce: sudo systemctl stop solunaro")
    return fh


def keycheck() -> int:
    """GET /v1/models with the configured key. Prints only the HTTP status and the API's error
    message; the key itself is never printed."""
    import requests
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("ANTHROPIC_API_KEY boş. .env dosyasına ekle.")
        return 1
    try:
        r = requests.get("https://api.anthropic.com/v1/models", timeout=20,
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except requests.RequestException as e:
        print("Bağlantı hatası:", type(e).__name__)
        return 1
    if r.status_code == 200:
        print("HTTP 200: anahtar geçerli.")
        return 0
    try:
        msg = r.json().get("error", {}).get("message", "")
    except ValueError:
        msg = ""
    print(f"HTTP {r.status_code}: {msg[:200]}")
    if not key.startswith("sk-ant-"):
        print("Not: Anthropic API anahtarları 'sk-ant-' ile başlar. console.anthropic.com > "
              "API Keys > Create Key penceresindeki değeri kopyala (listedeki 'apikey_...' "
              "kimliği anahtar değildir).")
    return 1


def load_cfg(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    st = cfg["storage"]
    st.setdefault("baseline_db_path", "data/baseline.sqlite")
    st.setdefault("dashboard_path", "data/dashboard.html")
    for k in ("db_path", "baseline_db_path", "dashboard_path", "reports_dir", "kill_switch_file"):
        if not os.path.isabs(cfg["storage"][k]):
            cfg["storage"][k] = os.path.join(HERE, cfg["storage"][k])
    if str(cfg["paper"].get("leverage", "none")).lower() != "none":
        sys.exit("Leverage is not supported. This bot is paper/spot only.")
    return cfg


def make_engine(cfg):
    from desk.engine import Engine
    from desk.llm import AnthropicLLM
    c = cfg["llm"]
    llm = AnthropicLLM(c["model"], c["max_tokens"], c.get("effort"), c["timeout_seconds"],
                       models=c.get("models") or {}, fallbacks=c.get("fallbacks", True))
    return Engine(cfg, llm)


def books(cfg, all_experiments=False):
    from desk.engine import experiment_id
    from desk.paper import PaperBroker
    bal = cfg["paper"]["starting_balance_usd"]
    d = PaperBroker(cfg["storage"]["db_path"], bal)
    b = PaperBroker(cfg["storage"]["baseline_db_path"], bal)
    if not all_experiments:
        d.scope = b.scope = experiment_id(cfg)
    return d, b


def eval_report(cfg, all_experiments=False):
    from desk.evaluate import full_report
    d, b = books(cfg, all_experiments)
    return full_report(d, b, cfg["llm"].get("prices_per_mtok") or {})


def write_dashboard(cfg, rep=None, all_experiments=False):
    from desk.dashboard import build_html
    rep = rep or eval_report(cfg, all_experiments)
    path = cfg["storage"]["dashboard_path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(build_html(rep))
    return path, rep


def daily_text(rep):
    d, b = rep["desk"], rep["baseline"]
    sp = rep["llm_spend"]
    cost = f"{sp['usd_total']} USD" if sp["usd_total"] is not None else \
        f"{sp['input_tokens']}+{sp['output_tokens']} token"
    return ("📊 Günlük paper özeti\n"
            f"Masa: {d['closed_trades']} işlem, PnL {d['net_pnl_usd']:+} USD, "
            f"kazanma {d['win_rate_pct']}%, maks düşüş {d['max_drawdown_pct']}%\n"
            f"Baseline: {b['closed_trades']} işlem, PnL {b['net_pnl_usd']:+} USD, "
            f"kazanma {b['win_rate_pct']}%\n"
            f"Kararlar: {rep['decision_counts']}\nLLM: {cost}\n\n{rep['verdict']}"
            + (f"\nSeçim testi: {rep['selection_test']['verdict']}" if rep.get("selection_test") else ""))


def run_backtest(cfg, days):
    from desk import backtest as BT
    from desk.engine import DEFAULT_BASELINE
    bcfg = {**DEFAULT_BASELINE, **(cfg.get("baseline") or {})}
    m = cfg["markets"]
    jobs = []
    if m.get("crypto", {}).get("enabled"):
        c = m["crypto"]
        sec = D.BINANCE_INTERVAL_SEC[c["interval"]]
        for s in c["symbols"]:
            jobs.append((f"crypto:{s['symbol']}", sec, c["fee_bps"], 0,
                         lambda s=s, c=c, sec=sec: D.binance_history(
                             s["symbol"], c["interval"], days * 86400 // sec)))
    if m.get("dex", {}).get("enabled"):
        x = m["dex"]
        sec = D.GT_INTERVAL_SEC[x["interval"]]
        for p in x["pools"]:
            jobs.append((f"dex:{p['address'][:8]}", sec, x["fee_bps"], x.get("slippage_bps", 0),
                         lambda p=p, x=x, sec=sec: D.geckoterminal_history(
                             x["chain"], p["address"], x["interval"], days * 86400 // sec)))
    results = {}
    for mid, sec, fee, slip, fetch in jobs:
        try:
            candles = fetch()
        except Exception as e:  # noqa: BLE001
            results[mid] = {"error": str(e)}
            continue
        if len(candles) <= BT.WINDOW + 10:
            results[mid] = {"error": f"only {len(candles)} candles"}
            continue
        results[mid] = BT.run(candles, sec, bcfg, cfg["paper"], fee, slip,
                              cfg["data"].get("shadow_max_hours", 72))
    for r in results.values():
        r.pop("trade_list", None)
    print(json.dumps(results, indent=1, default=str))
    failed_all = bool(results) and all("error" in r for r in results.values())
    print("\nNot: Bu sadece LLM'siz baseline kuralının geçmiş testidir. LLM masası geçmişte test "
          "edilemez (model geleceği 'biliyor' olabilir, geçmiş haber/duygu verisi yok).")
    return 1 if failed_all else 0


def brief(rep):
    keys = ("result", "paper_order", "baseline_opened")
    out = {k: rep[k] for k in keys if k in rep}
    for a in ("ATLAS", "VEGA", "ORION", "LUNA", "TITAN", "NOVA"):
        if a in rep:
            v = rep[a]
            out[a] = v.get("verdict") or v.get("final_verdict") or v.get("decision") or v.get("top_pick")
    calls = rep.get("llm_calls") or []
    out["llm_calls"] = len(calls)
    out["tokens"] = sum(c.get("input_tokens", 0) + c.get("output_tokens", 0) for c in calls)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["once", "loop", "status", "data", "eval", "dashboard",
                                    "backtest", "keycheck"])
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--all", action="store_true", help="eval/dashboard: include all experiments")
    ap.add_argument("--days", type=int, default=180, help="backtest: history length in days")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_cfg(a.config)

    if a.cmd == "keycheck":
        sys.exit(keycheck())

    if a.cmd == "data":
        from desk.engine import Engine
        eng = Engine.__new__(Engine)
        eng.cfg = cfg
        ok_markets = 0
        for mid, (kind, item) in Engine.market_configs(eng).items():
            try:
                s = Engine._fetch_market(eng, kind, item)
                sm = summarize(s.candles)
                print(mid, s.label, "price", s.last_price, "stale_min", round(s.staleness_minutes(), 1),
                      "rsi", sm["rsi14"], "levels", sm["levels"], "missing", s.missing)
                ok_markets += 1
            except Exception as e:  # noqa: BLE001
                print(mid, "FAILED:", e)
        try:
            print("futures SOLUSDT:", D.binance_futures_positioning("SOLUSDT"))
        except Exception as e:  # noqa: BLE001
            print("futures positioning FAILED:", e)
        tok = os.environ.get("CRYPTOPANIC_TOKEN")
        if (cfg.get("cryptopanic") or {}).get("enabled") and tok:
            try:
                cp = D.cryptopanic_news(cfg["cryptopanic"]["url"], tok, ["SOL"], 48)
                print(f"cryptopanic SOL items: {len(cp)}")
            except Exception as e:  # noqa: BLE001
                print("cryptopanic FAILED:", e)
        news, errs = D.fetch_news(cfg["news_feeds"], cfg["data"]["news_max_age_hours"])
        print(f"news items: {len(news)} feed errors: {errs}")
        try:
            print("fear&greed:", D.fear_greed()["latest"])
        except Exception as e:  # noqa: BLE001
            print("fear&greed FAILED:", e)
        if not ok_markets:
            sys.exit("Hiçbir piyasadan veri alınamadı.")
        return

    if a.cmd == "status":
        d, b = books(cfg)
        print(json.dumps({"desk": d.summary(), "baseline": b.summary()}, indent=1, default=str))
        return

    if a.cmd == "backtest":
        sys.exit(run_backtest(cfg, a.days))

    if a.cmd == "eval":
        rep = eval_report(cfg, a.all)
        for k in ("desk", "baseline"):
            rep[k] = {x: y for x, y in rep[k].items() if x != "equity_curve"}
        print(json.dumps(rep, indent=1, default=str))
        return

    if a.cmd == "dashboard":
        from desk.notify import Notifier
        path, rep = write_dashboard(cfg, all_experiments=a.all)
        print("written:", path)
        n = Notifier()
        if n.enabled:
            n.send_document(path, daily_text(rep)[:1000])
        return

    _lock = acquire_lock(cfg)  # noqa: F841 — kept open for the process lifetime
    eng = make_engine(cfg)
    if a.cmd == "once":
        print(json.dumps({"closed": eng.monitor()}, default=str))
        print(json.dumps(brief(eng.decide()), indent=1, default=str))
        return

    mon = cfg["schedule"]["monitor_every_minutes"] * 60
    dec = cfg["schedule"]["decide_every_minutes"] * 60
    last_dec = 0.0
    logging.info("PAPER desk loop started (monitor %ss, decide %ss)", mon, dec)
    hour = cfg["schedule"].get("daily_summary_hour_utc", 18)
    last_err_alert = 0.0
    while True:
        try:
            eng.monitor()
            if time.time() - last_dec >= dec:
                last_dec = time.time()
                logging.info("decision: %s", json.dumps(brief(eng.decide()), default=str))
            if eng.daily_due(hour):
                path, rep = write_dashboard(cfg)
                eng.notify.send(daily_text(rep))
                eng.notify.send_document(path, "Paper dashboard")
        except Exception as e:  # noqa: BLE001 — never crash the loop; log and continue
            logging.exception("cycle error: %s", e)
            if time.time() - last_err_alert > 3600:          # at most one alert per hour
                last_err_alert = time.time()
                eng.notify.send(f"⚠️ Desk hata: {type(e).__name__}: {str(e)[:300]}")
        time.sleep(mon)


if __name__ == "__main__":
    main()
