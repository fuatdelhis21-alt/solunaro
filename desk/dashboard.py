"""Static, self-contained HTML dashboard (no external scripts). Built from evaluate.full_report()."""
from __future__ import annotations

import html
import json
import time
from datetime import datetime, timezone

CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--card:#ffffff;--border:#e4e3df;--text:#0b0b0b;
--text2:#52514e;--muted:#8a8984;--grid:#ecebe7;--s1:#2a78d6;--s2:#eb6834}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--surface:#1a1a19;--card:#232322;--border:#3a3a38;--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;
--grid:#2f2f2d;--s1:#3987e5;--s2:#d95926}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--card:#232322;--border:#3a3a38;
--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;--grid:#2f2f2d;--s1:#3987e5;--s2:#d95926}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0}
h2{font-size:15px;margin:24px 0 8px}.sub{color:var(--text2);font-size:12px}
.verdict{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:12px 14px;
margin:14px 0;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.tile{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.tile .k{color:var(--text2);font-size:12px}.tile .v{font-size:20px;font-weight:650;
font-variant-numeric:tabular-nums}.tile .b{color:var(--muted);font-size:12px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--text2);font-weight:600}td.n{text-align:right}th.n{text-align:right}
.legend{display:flex;gap:16px;color:var(--text2);font-size:12px;margin-bottom:6px}
svg text{fill:var(--muted);font-size:11px}.tip{position:absolute;pointer-events:none;
background:var(--card);border:1px solid var(--border);border-radius:8px;padding:6px 8px;font-size:12px;
display:none;box-shadow:0 2px 8px rgba(0,0,0,.12)}.wrap{position:relative}
.note{color:var(--muted);font-size:12px;margin-top:6px}
.scroll{overflow-x:auto}.scroll svg{min-width:620px}
"""


def _fmt(v, suf=""):
    return "—" if v is None else f"{v:,.2f}{suf}" if isinstance(v, float) else f"{v}{suf}"


def _tile(k, desk, base, suf=""):
    return (f'<div class="tile"><div class="k">{html.escape(k)}</div>'
            f'<div class="v"><span class="sw" style="background:var(--s1)"></span>{_fmt(desk, suf)}</div>'
            f'<div class="b"><span class="sw" style="background:var(--s2)"></span>baseline {_fmt(base, suf)}</div></div>')


def _equity_svg(desk_curve, base_curve, start_ts) -> tuple[str, list]:
    W, H, L, R, T, Bm = 900, 260, 64, 70, 14, 28
    series = []
    for name, curve in (("Masa", desk_curve), ("Baseline", base_curve)):
        pts = [(start_ts, curve[0][1])] + [(ts, v) for ts, v in curve[1:]]
        series.append((name, pts))
    now = int(time.time())
    xs = [p[0] for _, s in series for p in s] + [now]
    ys = [p[1] for _, s in series for p in s]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    pad = max((y1 - y0) * 0.1, abs(y1) * 0.002, 1)
    y0, y1 = y0 - pad, y1 + pad
    sx = lambda x: L + (x - x0) / max(x1 - x0, 1) * (W - L - R)  # noqa: E731
    sy = lambda y: T + (y1 - y) / (y1 - y0) * (H - T - Bm)         # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Equity curves">']
    for i in range(5):
        v = y0 + (y1 - y0) * i / 4
        y = sy(v)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--grid)"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{v:,.0f}</text>')
    for i in range(4):
        t = x0 + (x1 - x0) * i / 3
        lbl = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d.%m %H:%M")
        out.append(f'<text x="{sx(t):.1f}" y="{H - 8}" text-anchor="middle">{lbl}</text>')
    js_series = []
    for idx, (name, pts) in enumerate(series):
        col = f"var(--s{idx + 1})"
        step = [pts[0]]
        for p in pts[1:]:
            step += [(p[0], step[-1][1]), p]
        step.append((now, step[-1][1]))
        d = "M" + " L".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in step)
        out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2" '
                   f'stroke-linejoin="round"/>')
        lx, ly = step[-1]
        out.append(f'<text x="{sx(lx) + 6:.1f}" y="{sy(ly) + 4 + (idx * 12 if len(series) > 1 else 0):.1f}" '
                   f'style="fill:var(--text2)">{name}</text>')
        js_series.append({"name": name, "pts": [[x, y] for x, y in step]})
    out.append(f'<line id="xh" x1="0" x2="0" y1="{T}" y2="{H - Bm}" stroke="var(--muted)" '
               f'stroke-dasharray="3 3" visibility="hidden"/>')
    out.append(f'<rect id="hit" x="{L}" y="{T}" width="{W - L - R}" height="{H - T - Bm}" '
               f'fill="transparent"/></svg>')
    meta = {"x0": x0, "x1": x1, "L": L, "R": R, "W": W}
    return "".join(out), [js_series, meta]


JS = """
(function(){const [S,M]=DATA;const svg=document.querySelector('#eq svg');if(!svg)return;
const hit=svg.querySelector('#hit'),xh=svg.querySelector('#xh'),tip=document.getElementById('tip');
function val(pts,t){let v=pts[0][1];for(const p of pts){if(p[0]<=t)v=p[1];else break}return v}
function move(e){const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)/r.width*M.W;
const t=M.x0+(px-M.L)/(M.W-M.L-M.R)*(M.x1-M.x0);if(px<M.L||px>M.W-M.R){out();return}
xh.setAttribute('x1',px);xh.setAttribute('x2',px);xh.setAttribute('visibility','visible');
const d=new Date(t*1000).toISOString().slice(0,16).replace('T',' ');
tip.innerHTML='<b>'+d+' UTC</b><br>'+S.map((s,i)=>'<span class="sw" style="background:var(--s'+(i+1)+')"></span>'+s.name+': '+val(s.pts,t).toLocaleString(undefined,{maximumFractionDigits:2})+' USD').join('<br>');
tip.style.display='block';const wr=document.getElementById('eq').getBoundingClientRect();
const w=tip.offsetWidth;let x=e.clientX-wr.left+12;if(x+w>wr.width)x-=w+24;if(x<4)x=4;
tip.style.left=x+'px';tip.style.top='8px'}
function out(){xh.setAttribute('visibility','hidden');tip.style.display='none'}
hit.addEventListener('pointermove',move);hit.addEventListener('pointerleave',out)})();
"""


def _table(headers, rows, numeric=()):
    th = "".join(f'<th class="{"n" if i in numeric else ""}">{html.escape(h)}</th>'
                 for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(
        f'<td class="{"n" if i in numeric else ""}">{html.escape(_fmt(c))}</td>' for i, c in enumerate(r))
        + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body or '<tr><td>Henüz veri yok</td></tr>'}</tbody></table>"


def _selection(rep) -> str:
    t = rep.get("selection_test")
    if not t:
        return '<div class="note">Seçim testi: henüz yeterli sonuçlanmış setup yok.</div>'
    return (f'<div class="note">Seçim testi (aynı setup\'lar içinde alınan ve alınmayan): '
            f'{t["diff"]:+} R, %95 GA [{t["ci95"][0]}, {t["ci95"][1]}], '
            f'n={t["n_taken"]}/{t["n_skipped"]}. Sonuç: {html.escape(t["verdict"])}</div>')


def build_html(rep: dict) -> str:
    d, b = rep["desk"], rep["baseline"]
    start = min([c[0] for c in d["equity_curve"][1:] + b["equity_curve"][1:]] or [int(time.time()) - 3600])
    start -= 3600
    svg, data = _equity_svg(d["equity_curve"], b["equity_curve"], start)
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tiles = "".join([
        _tile("Kapanan işlem", d["closed_trades"], b["closed_trades"]),
        _tile("Net PnL (USD)", d["net_pnl_usd"], b["net_pnl_usd"]),
        _tile("Getiri", d["return_pct"], b["return_pct"], "%"),
        _tile("Kazanma oranı", d["win_rate_pct"], b["win_rate_pct"], "%"),
        _tile("Beklenti / işlem", d["expectancy_usd"], b["expectancy_usd"]),
        _tile("Ort. R", d["avg_r"], b["avg_r"]),
        _tile("Profit factor", d["profit_factor"], b["profit_factor"]),
        _tile("Maks. düşüş", d["max_drawdown_pct"], b["max_drawdown_pct"], "%"),
    ])
    sh = rep["shadows_by_decision"]
    sh_rows = [[k, v["setups"], v["hit_target"], v["hit_stop"], v["expired"], v["still_open"],
                v["target_rate_pct"], v["avg_r"]] for k, v in sorted(sh.items())]
    acc_rows = []
    for agent in ("ATLAS", "VEGA", "ORION", "LUNA", "TITAN", "NOVA", "CODE"):
        for verdict, g in sorted((rep["agent_accuracy"].get(agent) or {}).items()):
            acc_rows.append([agent, verdict, g["resolved"], g["target_rate_pct"], g["avg_r"]])
    sp = rep["llm_spend"]
    sp_rows = [[r["agent"], r["model"], r["calls"], r["input_tokens"], r["output_tokens"], r["usd"]]
               for r in sp["per_agent"]]
    val_rows = [[a, v.get("n_pos"), v.get("n_neg"), v.get("diff_r"),
                 (f"[{v['ci95'][0]}, {v['ci95'][1]}]" if v.get("ci95") else None), v["verdict"]]
                for a, v in (rep.get("agent_value") or {}).items()]
    dc_rows = [[k, v] for k, v in sorted(rep["decision_counts"].items(), key=lambda x: -x[1])]
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading Desk Paper</title>
<style>{CSS}</style></head><body><main>
<h1>Solunaro: paper raporu</h1><div class="sub">Oluşturulma {gen} · sadece PAPER · tüm sayılar bot günlüğünden · deney: {html.escape(str(rep.get("experiment")))}</div>
<div class="verdict">{html.escape(rep["verdict"])}</div>{_selection(rep)}
<h2>Masa ve LLM'siz baseline</h2><div class="tiles">{tiles}</div>
<h2>Bakiye eğrisi (USD)</h2><div class="card wrap" id="eq">
<div class="legend"><span><span class="sw" style="background:var(--s1)"></span>Masa (6 ajan)</span>
<span><span class="sw" style="background:var(--s2)"></span>Baseline (kural)</span></div><div class="scroll">{svg}</div>
<div class="tip" id="tip"></div></div>
<h2>Alınmayan setup'lar ne oldu? (karşı-olgusal)</h2><div class="card">
{_table(["NOVA kararı", "Setup", "Hedef", "Stop", "Süresi doldu", "Açık", "Hedef oranı %", "Ort. R"], sh_rows, (1, 2, 3, 4, 5, 6, 7))}
<div class="note">REJECT/WAIT kararlarının hedef oranı TRADE'den yüksekse masa iyi fırsatları eliyor demektir.</div></div>
<h2>Ajan isabeti</h2><div class="card">
{_table(["Ajan", "Görüş", "Sonuçlanan", "Hedef oranı %", "Ort. R"], acc_rows, (2, 3, 4))}
<div class="note">Bir ajanın olumlu ve olumsuz görüşleri arasında fark yoksa o ajan karara bilgi katmıyor.</div></div>
<h2>Ajan değeri (olumlu ve olumsuz görüş farkı)</h2><div class="card">
{_table(["Ajan", "Olumlu n", "Olumsuz n", "Fark (R)", "%95 GA", "Sonuç"], val_rows, (1, 2, 3))}
<div class="note">Güven aralığı sıfırı içeriyorsa ajanın görüşü sonuçla ilişkili değil; bu ajan maliyet olabilir.</div></div>
<h2>Karar dağılımı</h2><div class="card">{_table(["Sonuç", "Adet"], dc_rows, (1,))}</div>
<h2>LLM kullanımı</h2><div class="card">
{_table(["Ajan", "Model", "Çağrı", "Girdi token", "Çıktı token", "USD"], sp_rows, (2, 3, 4, 5))}
<div class="note">{html.escape(sp.get("usd_note") or f"Toplam: {sp['usd_total']} USD")}</div></div>
</main><script>const DATA={json.dumps(data)};{JS}</script></body></html>"""
