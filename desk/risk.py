"""Hard risk rules enforced in code. The LLM TITAN can only make things stricter."""
from __future__ import annotations

ATR_MULTIPLES = (1, 1.5, 2, 2.5, 3)


def allowed_levels(summary: dict) -> list[float]:
    vals = [summary.get(k) for k in ("ema20", "ema50", "ema200", "high_24", "low_24",
                                     "high_lookback", "low_lookback")]
    lv = summary.get("levels", {})
    vals += lv.get("resistance_above", []) + lv.get("support_below", [])
    for c in summary.get("last_12_candles", []):
        vals += [c[2], c[3]]
    last, a = summary.get("last_close"), summary.get("atr14")
    if last and a:
        for k in ATR_MULTIPLES:
            vals += [last - k * a, last + k * a]
    return [float(v) for v in vals if v]


def grounded(value: float, levels: list[float], tol_pct: float = 0.1) -> bool:
    return any(abs(value - lv) / lv * 100 <= tol_pct for lv in levels if lv > 0)


def evaluate(setup: dict | None, price: float, summary: dict, cfg: dict,
             account: dict, fee_bps: float, slippage_bps: float = 0) -> dict:
    """Return sizing + PASS/FAIL list. `account` = balance, open_positions,
    trades_today, pnl_today, in_cooldown, staleness_min, max_staleness_min."""
    checks = []

    def chk(name, ok, issue=""):
        checks.append({"name": name, "result": "PASS" if ok else "FAIL", "issue": "" if ok else issue})

    chk("data_fresh", account["staleness_min"] <= account["max_staleness_min"],
        f"last candle is {account['staleness_min']:.0f} min old")
    chk("max_open_positions", account["open_positions"] < cfg["max_open_positions"],
        "position limit reached")
    chk("max_trades_per_day", account["trades_today"] < cfg["max_trades_per_day"],
        "daily trade limit reached")
    kill = -account["pnl_today"] / account["balance"] * 100 if account["balance"] else 0
    chk("daily_kill", kill < cfg["daily_kill_loss_pct"], f"daily loss {kill:.2f}% >= limit")
    chk("cooldown", not account["in_cooldown"], "market is in cooldown")

    result = {"checks": checks, "entry": None, "stop": None, "target": None, "qty": None,
              "notional": None, "max_loss_usd": None, "reward_risk": None}
    if not setup:
        chk("setup_present", False, "VEGA proposed no setup")
        result["all_pass"] = False
        return result

    cost = (fee_bps * 2 + slippage_bps * 2) / 10000          # round trip
    entry = price * (1 + slippage_bps / 10000)
    try:
        stop, target = float(setup["stop"]), float(setup["target"])
    except (KeyError, TypeError, ValueError):
        chk("setup_numbers", False, "stop/target missing or not numeric")
        result["all_pass"] = False
        return result

    levels = allowed_levels(summary)
    chk("long_only_geometry", stop < entry < target, f"need stop<entry<target, got {stop}/{entry}/{target}")
    chk("stop_grounded_in_data", grounded(stop, levels), f"stop {stop} not found in supplied data")
    chk("target_grounded_in_data", grounded(target, levels), f"target {target} not found in supplied data")
    stop_d = (entry - stop) / entry * 100
    tgt_d = (target - entry) / entry * 100
    chk("stop_distance", 0 < stop_d <= cfg["max_stop_distance_pct"], f"stop {stop_d:.2f}% away")
    chk("target_distance", 0 < tgt_d <= cfg["max_target_distance_pct"], f"target {tgt_d:.2f}% away")

    risk_per_unit = entry - stop + entry * cost
    reward_per_unit = target - entry - entry * cost
    rr = reward_per_unit / risk_per_unit if risk_per_unit > 0 else 0
    chk("reward_risk", rr >= cfg["min_reward_risk"], f"R:R {rr:.2f} < {cfg['min_reward_risk']}")

    max_loss = account["balance"] * cfg["max_loss_pct_per_trade"] / 100
    qty = max_loss / risk_per_unit if risk_per_unit > 0 else 0
    cap = account["balance"] * cfg["max_position_pct"] / 100
    if qty * entry > cap:
        qty = cap / entry
    chk("size_positive", qty > 0, "computed size is zero")

    result.update(entry=round(entry, 10), stop=stop, target=target, qty=qty,
                  notional=round(qty * entry, 2), max_loss_usd=round(qty * risk_per_unit, 2),
                  reward_risk=round(rr, 3))
    result["all_pass"] = all(c["result"] == "PASS" for c in checks)
    return result
