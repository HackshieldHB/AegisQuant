import csv
import hashlib
import hmac
import html
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

APP_ROOT = os.path.expanduser("~/aegisquant_app")
STATE_DIR = os.path.join(APP_ROOT, "logs", "state")
LOG_DIR = os.path.join(APP_ROOT, "logs")
ENGINE_LOG = os.path.join(LOG_DIR, "engine_subprocess.log")
WATCHDOG_LOG = os.path.join(LOG_DIR, "watchdog_prod.log")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
HEARTBEAT_FILE = os.path.join(LOG_DIR, "engine_heartbeat.json")
IDR_RATE = 16000
START_IDR = 300000
TARGET_IDR = 10000000
MARKET_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "SHIBUSDT", "PEPEUSDT")
_MARKET_CACHE = {"ts": 0.0, "rows": []}


def _env_value(name):
    path = os.path.join(APP_ROOT, ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip("\"'")
    except Exception:
        pass
    return os.getenv(name, "")


def _authorized(environ):
    expected = _env_value("AEGIS_DASHBOARD_TOKEN")
    supplied = environ.get("HTTP_X_AEGIS_TOKEN", "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _tail(path, limit=70000):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit), os.SEEK_SET)
            return fh.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _git_rev():
    try:
        return subprocess.check_output(
            ["git", "-C", APP_ROOT, "rev-parse", "--short", "HEAD"],
            text=True,
            timeout=2,
        ).strip()
    except Exception:
        return "unknown"


def _processes():
    try:
        out = subprocess.check_output(
            ["ps", "-u", os.environ.get("USER", "aegisqua"), "-o", "pid,etime,stat,%cpu,%mem,rss,cmd"],
            text=True,
            timeout=3,
        )
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if "WatchdogSupervisor.py" in line or "Main.py" in line]


def _scan_rows(log_text):
    rows = []
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[SCAN\]\s+([A-Z]+/USDT)\s+\|\s+(\w+)\s+\|\s+conf=(\d+)%\s+\|\s+(.+)"
    )
    for match in pattern.finditer(log_text):
        rows.append(match.groups())
    return rows[-21:]


def _cycle_stats(log_text):
    cycles = re.findall(r"\[SCAN\] Cycle #(\d+) starting", log_text)
    done = re.findall(r"\[SCAN\] Done .*candidates=(\d+) executed=(\d+)", log_text)
    return {
        "cycle": int(cycles[-1]) if cycles else None,
        "last_candidates": int(done[-1][0]) if done else 0,
        "last_executed": int(done[-1][1]) if done else 0,
    }


def _execution_diagnostics(log_text):
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[SCAN\]\s+"
        r"([A-Z]+/USDT)\s+\|\s+(\w+)\s+\|\s+conf=(\d+)%\s+\|\s+(.+)"
    )
    unique = {}
    for match in pattern.finditer(log_text):
        row = match.groups()
        unique[(row[0], row[1], row[2], row[4])] = row
    rows = list(unique.values())[-140:]
    states = {}
    blockers = {}
    confidences = []
    for _, _, state, confidence, reason in rows:
        states[state] = states.get(state, 0) + 1
        confidences.append(int(confidence))
        if reason.startswith("Signal conflict"):
            label = "TA vs AI conflict"
        elif reason.startswith("MTF conflict"):
            label = "1H trend conflict"
        elif reason.startswith("NEWS_PANIC_BLOCK"):
            label = "News panic gate"
        elif reason.startswith("AI-only gate"):
            label = "AI-only confidence gate"
        elif reason == "No actionable signal":
            label = "No TA/AI setup"
        else:
            label = reason[:80]
        blockers[label] = blockers.get(label, 0) + 1
    blocker_rows = [
        {"reason": reason, "count": count}
        for reason, count in sorted(blockers.items(), key=lambda item: item[1], reverse=True)
    ]
    latest_by_symbol = {}
    required_confidence = 0
    for row in rows:
        latest_by_symbol[row[1]] = row
        gate_match = re.search(r"directional conf (\d+)% < (\d+)%", row[4])
        if gate_match:
            required_confidence = max(required_confidence, int(gate_match.group(2)))
    latest_rows = list(latest_by_symbol.values())
    best_row = max(latest_rows, key=lambda row: int(row[3]), default=None)
    best_confidence = int(best_row[3]) if best_row else 0
    return {
        "samples": len(rows),
        "states": states,
        "blockers": blocker_rows[:6],
        "avg_confidence": round(sum(confidences) / len(confidences), 1) if confidences else 0.0,
        "best_symbol": best_row[1] if best_row else None,
        "best_confidence": best_confidence,
        "required_confidence": required_confidence,
        "confidence_gap": max(0, required_confidence - best_confidence),
        "signal_collapse": "Signal Collapse" in log_text,
    }


def _trades():
    if not os.path.exists(TRADES_CSV):
        return [], {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
    rows = []
    try:
        with open(TRADES_CSV, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        rows = []
    pnl_values = []
    for row in rows:
        try:
            pnl_values.append(float(row.get("PnL") or row.get("pnl") or 0.0))
        except Exception:
            pnl_values.append(0.0)
    wins = sum(1 for value in pnl_values if value > 0)
    losses = sum(1 for value in pnl_values if value < 0)
    return rows, {"count": len(rows), "pnl": sum(pnl_values), "wins": wins, "losses": losses}


def _trade_analytics(rows):
    daily = {}
    symbols = {}
    equity = []
    cumulative = 0.0
    for index, row in enumerate(rows):
        raw_pnl = row.get("PnL") or row.get("pnl") or 0
        try:
            pnl = float(raw_pnl)
        except Exception:
            pnl = 0.0
        raw_ts = row.get("Timestamp") or row.get("timestamp") or ""
        day = str(raw_ts)[:10] or "unknown"
        symbol = row.get("Symbol") or row.get("symbol") or "UNKNOWN"
        daily[day] = daily.get(day, 0.0) + pnl
        stats = symbols.setdefault(symbol, {"symbol": symbol, "trades": 0, "wins": 0, "pnl": 0.0})
        stats["trades"] += 1
        stats["wins"] += int(pnl > 0)
        stats["pnl"] += pnl
        cumulative += pnl
        equity.append({"index": index + 1, "timestamp": raw_ts, "pnl": round(cumulative, 8)})
    daily_rows = [{"date": day, "pnl": round(value, 8)} for day, value in sorted(daily.items())]
    symbol_rows = []
    for stats in symbols.values():
        stats["win_rate"] = round(stats["wins"] / max(1, stats["trades"]) * 100, 2)
        stats["pnl"] = round(stats["pnl"], 8)
        symbol_rows.append(stats)
    symbol_rows.sort(key=lambda item: item["pnl"], reverse=True)
    return daily_rows[-90:], symbol_rows, equity[-200:]


def _market_snapshot():
    if time.time() - _MARKET_CACHE["ts"] < 20 and _MARKET_CACHE["rows"]:
        return _MARKET_CACHE["rows"]
    try:
        request = urllib.request.Request(
            "https://api.binance.com/api/v3/ticker/24hr",
            headers={"User-Agent": "AegisQuant-Dashboard/1.0"},
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
        wanted = set(MARKET_SYMBOLS)
        rows = []
        for item in payload:
            if item.get("symbol") not in wanted:
                continue
            rows.append({
                "symbol": item["symbol"].replace("USDT", "/USDT"),
                "price": float(item.get("lastPrice") or 0),
                "change": float(item.get("priceChangePercent") or 0),
                "high": float(item.get("highPrice") or 0),
                "low": float(item.get("lowPrice") or 0),
                "volume": float(item.get("quoteVolume") or 0),
            })
        rows.sort(key=lambda row: MARKET_SYMBOLS.index(row["symbol"].replace("/", "")))
        _MARKET_CACHE.update({"ts": time.time(), "rows": rows})
    except Exception:
        pass
    return _MARKET_CACHE["rows"]


def _recommendations(snapshot):
    items = []
    if not snapshot["engine_ok"] or not snapshot["watchdog_ok"]:
        items.append({"level": "critical", "title": "Runtime process missing", "detail": "Restore WatchdogSupervisor and Main.py before accepting signals."})
    if snapshot["heartbeat_age_sec"] is None or snapshot["heartbeat_age_sec"] > 1800:
        items.append({"level": "critical", "title": "Heartbeat stale", "detail": "Engine cycle has not reported within the expected 30-minute window."})
    if snapshot["cycles"]["last_candidates"] == 0:
        items.append({"level": "info", "title": "No qualified setup", "detail": "The latest scan produced no candidate. Keep risk gates intact; do not force an entry."})
    if snapshot["balance_usdt"] < 25:
        items.append({"level": "warning", "title": "Micro-account constraints", "detail": "Fees and minimum notional materially affect trade selection below 25 USDT."})
    if snapshot["model_alerts"]:
        items.append({"level": "warning", "title": "Review model drift", "detail": "Recent model-health alerts were detected. Compare live confidence distribution with training metadata."})
    if snapshot.get("execution_diagnostics", {}).get("signal_collapse"):
        items.append({"level": "critical", "title": "Signal pipeline collapsed", "detail": "Recent scans produced no actionable candidates. Review directional confidence and TA/AI consensus before changing risk."})
    if not items:
        items.append({"level": "success", "title": "System nominal", "detail": "Runtime, heartbeat, and recent logs are healthy."})
    return items


def _snapshot():
    balance = _json(os.path.join(STATE_DIR, "balance.json"), {})
    positions = _json(os.path.join(STATE_DIR, "positions.json"), {})
    heartbeat = _json(HEARTBEAT_FILE, {})
    log_text = _tail(ENGINE_LOG)
    watch_text = _tail(WATCHDOG_LOG, 30000)
    process = _processes()
    trades, trade_stats = _trades()
    daily_pnl, symbol_stats, equity_curve = _trade_analytics(trades)

    hb_ts = float(heartbeat.get("ts") or 0)
    hb_age = time.time() - hb_ts if hb_ts else None
    bal = float(balance.get("balance") or balance.get("usdt") or 0.0) if isinstance(balance, dict) else 0.0
    pos_count = int(positions.get("count") or len(positions.get("positions", []))) if isinstance(positions, dict) else 0
    errors = []
    for line in (log_text + "\n" + watch_text).splitlines():
        if any(key in line for key in ("CRITICAL", "Traceback", "exited with code", "TERMINAL HALT", "Killed", "ERROR")):
            errors.append(line)
    model_alerts = [
        line for line in errors
        if "ModelHealthMonitor" in line or "Probability Indecision" in line or "Signal Collapse" in line
    ]
    engine_ok = any("Main.py" in line for line in process)
    watchdog_ok = any("WatchdogSupervisor.py" in line for line in process)
    heartbeat_ok = hb_age is not None and hb_age < 1800
    health = "OK" if engine_ok and watchdog_ok and heartbeat_ok else "WARN"
    if model_alerts:
        health = "MODEL WATCH"
    snapshot = {
        "health": health,
        "engine_ok": engine_ok,
        "watchdog_ok": watchdog_ok,
        "heartbeat_age_sec": round(hb_age, 1) if hb_age is not None else None,
        "heartbeat_cycle": heartbeat.get("cycle"),
        "balance_usdt": bal,
        "balance_idr": bal * IDR_RATE,
        "growth_pct": max(0.0, min(100.0, ((bal * IDR_RATE) - START_IDR) / (TARGET_IDR - START_IDR) * 100)),
        "positions": pos_count,
        "git_rev": _git_rev(),
        "cycles": _cycle_stats(log_text),
        "scan_rows": _scan_rows(log_text),
        "model_alerts": model_alerts[-8:],
        "errors": errors[-10:],
        "process": process,
        "trades": trades,
        "trade_stats": trade_stats,
        "daily_pnl": daily_pnl,
        "symbol_stats": symbol_stats,
        "equity_curve": equity_curve,
        "market": _market_snapshot(),
        "execution_diagnostics": _execution_diagnostics(log_text),
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    snapshot["recommendations"] = _recommendations(snapshot)
    return snapshot


def _e(value):
    return html.escape(str(value))


def _safe_json(data):
    return (
        json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_page():
    s = _snapshot()
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AegisQuant Terminal</title><script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#05070b;--ink:#eef4ff;--muted:#8793aa;--panel:#0b111b;--panel2:#101827;--line:#263247;--cyan:#00e5c3;--gold:#f7c948;--green:#16c784;--red:#ea3943;--blue:#6ea8ff}
*{box-sizing:border-box}body{margin:0;background:#05070b;color:var(--ink);font-family:Inter,Segoe UI,Arial,sans-serif;letter-spacing:0}body:before{content:"";position:fixed;inset:0;background:radial-gradient(circle at 15% 0,#15385a88,transparent 34%),radial-gradient(circle at 90% 10%,#1b3f2f88,transparent 28%),linear-gradient(180deg,#07111e,#05070b 38%);pointer-events:none}
.shell{position:relative;max-width:1560px;margin:0 auto;padding:14px}.top{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:stretch}.brand{border:1px solid #30405a;background:linear-gradient(135deg,#101827,#07101c);border-radius:6px;padding:14px 18px}.eyebrow{color:var(--gold);font-size:10px;font-weight:900;letter-spacing:.18em;text-transform:uppercase}.brand h1{font-size:30px;line-height:1;margin:6px 0}.brand p{margin:0;color:var(--muted);max-width:920px;font-size:13px}.status{min-width:280px;border:1px solid #30405a;border-radius:6px;background:#090f18;padding:13px}.badge{display:inline-flex;border-radius:999px;border:1px solid #30405a;padding:6px 9px;font-size:11px;font-weight:900}.ok{color:var(--cyan);background:#00e5c315}.warn{color:var(--gold);background:#f7c94815}.bad{color:var(--red);background:#ea394315}
.ticker{display:flex;gap:7px;overflow-x:auto;margin:9px 0;scrollbar-width:thin}.tick{flex:1;min-width:145px;border:1px solid #263247;background:#09111d;border-radius:5px;padding:8px 10px}.tick b{display:block}.tick span{font-size:12px}.pos{color:var(--green)}.neg{color:var(--red)}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.card{background:linear-gradient(180deg,#101827,#0a1019);border:1px solid #263247;border-radius:7px;padding:13px}.label{font-size:10px;color:var(--muted);font-weight:900;text-transform:uppercase;letter-spacing:.12em}.value{font-size:24px;font-weight:950;margin-top:7px}.sub{font-size:12px;color:var(--muted);margin-top:3px}.layout{display:grid;grid-template-columns:1.35fr .65fr;gap:12px;margin-top:12px}.panel{background:#090f18;border:1px solid #263247;border-radius:8px;padding:14px}.panel h2{margin:0 0 12px;font-size:17px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.chartBox{position:relative;height:260px;max-height:260px;overflow:hidden}.chart{display:block;width:100%!important;height:260px!important;max-height:260px!important}.heat{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:8px}.heatCell{min-height:86px;min-width:0;border-radius:7px;padding:10px;border:1px solid #263247;background:#101827;overflow:hidden}.heatCell b,.heatCell span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.heatCell b{font-size:14px}.heatCell span{font-size:11px;margin-top:3px}.heatCell strong{display:block;font-size:21px;margin-top:8px}
.tabs{display:flex;gap:4px;margin:10px 0;border-bottom:1px solid var(--line)}.tab{appearance:none;border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);font-weight:850;padding:9px 14px;cursor:pointer}.tab.active{color:var(--cyan);border-color:var(--cyan)}.view{display:none}.view.active{display:block}.scrollTable{max-height:430px;overflow:auto}.verdict{border:1px solid #574718;background:#1a170b;padding:11px;border-radius:6px;margin-bottom:10px}.verdict strong{color:var(--gold);font-size:15px}.verdict span{display:block;color:var(--muted);font-size:12px;margin-top:3px}
.rec{display:grid;gap:9px;max-height:205px;overflow:auto;padding-right:3px}.recItem{border:1px solid #263247;border-left:4px solid var(--blue);background:#0d1522;border-radius:7px;padding:11px}.recItem.critical{border-left-color:var(--red)}.recItem.warning{border-left-color:var(--gold)}.recItem.success{border-left-color:var(--green)}.recItem strong{display:block}.recItem span{display:block;color:var(--muted);font-size:13px;margin-top:3px}
.funnel{display:grid;gap:9px}.funnelRow{display:grid;grid-template-columns:130px 1fr 34px;gap:8px;align-items:center;font-size:12px}.funnelTrack{height:8px;background:#172235;border-radius:999px;overflow:hidden}.funnelFill{height:100%;background:linear-gradient(90deg,var(--gold),var(--red));border-radius:999px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{border-bottom:1px solid #1d293c;padding:8px;text-align:left;vertical-align:top}th{color:#9aa8bf;font-size:10px;text-transform:uppercase;letter-spacing:.1em}tr:hover td{background:#101827}.terminal{font-family:Consolas,Menlo,monospace;font-size:12px;color:#aebbd1}.bar{height:9px;background:#172235;border-radius:999px;overflow:hidden;margin-top:10px}.fill{height:100%;background:linear-gradient(90deg,var(--cyan),var(--green));width:0}.foot{text-align:center;color:var(--muted);font-size:12px;padding:16px}
@media(max-width:1120px){.layout,.grid2,.top{grid-template-columns:1fr}.cards{grid-template-columns:repeat(3,1fr)}}@media(max-width:680px){.shell{padding:8px}.brand h1{font-size:25px}.cards,.heat{grid-template-columns:1fr 1fr}.status{min-width:0}.tab{padding:8px;font-size:11px}.funnelRow{grid-template-columns:110px 1fr 28px}}
</style></head><body><main class="shell">
<section class="top"><div class="brand"><div class="eyebrow">AegisQuant / Live Production Terminal</div><h1>Trading Desk Monitor</h1><p>Actual engine logs, Binance 24h market data, closed PnL analytics, model alerts, and my operating recommendation in one root dashboard.</p></div><aside class="status"><div id="healthBadge" class="badge">LOADING</div><div class="value" id="growthPct">0%</div><div class="sub">Growth target: Rp 300,000 to Rp 10,000,000</div><div class="bar"><div id="growthFill" class="fill"></div></div><div class="sub" id="updatedAt"></div></aside></section>
<section class="ticker" id="ticker"></section><section class="cards" id="cards"></section>
<nav class="tabs"><button class="tab active" data-view="desk">Desk</button><button class="tab" data-view="signals">Signals</button><button class="tab" data-view="runtime">Runtime</button></nav>
<section id="desk" class="view active"><section class="layout"><div><div class="grid2"><section class="panel"><h2 id="equityTitle">Strategy Equity</h2><div class="chartBox"><canvas id="equityChart" class="chart"></canvas></div></section><section class="panel"><h2 id="dailyTitle">Daily PnL Pulse</h2><div class="chartBox"><canvas id="dailyChart" class="chart"></canvas></div></section></div><section class="panel" style="margin-top:12px"><h2>Market Heatmap</h2><div id="heatmap" class="heat"></div></section></div>
<aside><section class="panel"><h2>Execution Verdict</h2><div id="executionVerdict"></div><div id="executionDiagnosis"></div></section><section class="panel" style="margin-top:12px"><h2>Operator Recommendation</h2><div id="recommendations" class="rec"></div></section><section class="panel" style="margin-top:12px"><h2>Symbol Edge Board</h2><div class="chartBox"><canvas id="symbolChart" class="chart"></canvas></div></section></aside></section></section>
<section id="signals" class="view"><div class="grid2"><section class="panel"><h2>Latest Signal Tape</h2><div id="scanTable" class="scrollTable"></div></section><section class="panel"><h2>Trade Blotter</h2><div id="tradeTable" class="scrollTable"></div></section></div></section>
<section id="runtime" class="view"><div class="grid2"><div class="panel"><h2>Model / Runtime Alerts</h2><div id="alerts" class="scrollTable"></div></div><div class="panel"><h2>Process Monitor</h2><pre id="processes" class="terminal"></pre></div></div></section>
<div class="foot">Root dashboard refreshes every 30 seconds. Private JSON API remains protected at /api/dashboard.</div>
</main><script id="aegis-data" type="application/json">__DATA__</script><script>
const s=JSON.parse(document.getElementById("aegis-data").textContent),$=id=>document.getElementById(id);
const money=n=>"$"+Number(n||0).toFixed(4),idr=n=>"Rp "+Number(n||0).toLocaleString("id-ID",{maximumFractionDigits:0}),pct=n=>Number(n||0).toFixed(2)+"%",cls=v=>Number(v||0)>=0?"pos":"neg";
const esc=v=>String(v??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));
function table(h,r,e){return r.length?`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join("")}</tr></thead><tbody>${r.join("")}</tbody></table>`:`<div class="sub">${e}</div>`}
function card(k,v,sub){return `<div class="card"><div class="label">${k}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`}
function renderShell(){const t=s.trade_stats||{},c=s.cycles||{},wr=(t.wins||0)/Math.max(1,(t.wins||0)+(t.losses||0))*100,b=s.health==="OK"?"ok":s.health==="MODEL WATCH"?"warn":"bad";$("healthBadge").className="badge "+b;$("healthBadge").textContent=s.health;$("growthPct").textContent=pct(s.growth_pct);$("growthFill").style.width=Math.max(0,Math.min(100,s.growth_pct||0))+"%";$("updatedAt").textContent="Updated "+s.updated_utc;
$("ticker").innerHTML=(s.market||[]).map(m=>`<div class="tick"><b>${esc(m.symbol)}</b><span>${Number(m.price).toLocaleString("en-US",{maximumFractionDigits:m.price<1?8:2})}</span><span class="${cls(m.change)}"> ${pct(m.change)}</span></div>`).join("")||"<div class='sub'>Live Binance ticker unavailable.</div>";
$("cards").innerHTML=[card("USDT Balance",money(s.balance_usdt),idr(s.balance_idr)),card("Growth",pct(s.growth_pct),"Target runway"),card("Open Positions",s.positions||0,"Live portfolio"),card("Cycle","#"+(s.heartbeat_cycle??"-"),"Heartbeat "+(s.heartbeat_age_sec??"-")+"s"),card("Candidates",c.last_candidates||0,"Latest cycle"),card("Closed PnL",`<span class="${cls(t.pnl)}">${money(t.pnl)}</span>`,(t.count||0)+" closed trades")].join("");
$("recommendations").innerHTML=(s.recommendations||[]).map(r=>`<div class="recItem ${esc(r.level)}"><strong>${esc(r.title)}</strong><span>${esc(r.detail)}</span></div>`).join("");
const dx=s.execution_diagnostics||{},blocks=dx.blockers||[],maxBlock=Math.max(1,...blocks.map(b=>b.count));$("executionVerdict").innerHTML=dx.best_symbol?`<div class="verdict"><strong>WAIT — ${esc(dx.best_symbol)} ${dx.best_confidence||0}%</strong><span>AI-only entry requires ${dx.required_confidence||"—"}%. Current gap: ${dx.confidence_gap||0} points. No exchange order was attempted.</span></div>`:"<div class='sub'>Waiting for complete scan diagnostics.</div>";$("executionDiagnosis").innerHTML=`<div class="cards" style="grid-template-columns:1fr 1fr;margin-bottom:12px">${card("Samples",dx.samples||0,"Recent unique scans")}${card("Avg Confidence",pct(dx.avg_confidence||0),dx.signal_collapse?"Signal collapse active":"Pipeline responsive")}</div><div class="funnel">${blocks.map(b=>`<div class="funnelRow"><span>${esc(b.reason)}</span><div class="funnelTrack"><div class="funnelFill" style="width:${b.count/maxBlock*100}%"></div></div><b>${b.count}</b></div>`).join("")||"<div class='sub'>No execution diagnostics yet.</div>"}</div>`;
$("heatmap").innerHTML=(s.market||[]).map(m=>{const ch=Number(m.change||0),a=Math.min(Math.abs(ch)/8,.85),bg=ch>=0?`rgba(22,199,132,${.12+a*.55})`:`rgba(234,57,67,${.12+a*.55})`;return `<div class="heatCell" style="background:${bg}"><b>${esc(m.symbol)}</b><span class="muted">${Number(m.price).toLocaleString("en-US",{maximumFractionDigits:m.price<1?8:2})}</span><strong class="${cls(ch)}">${pct(ch)}</strong></div>`}).join("");
$("scanTable").innerHTML=table(["Time","Pair","Signal","Conf","Reason"],(s.scan_rows||[]).slice().reverse().map(r=>`<tr><td>${esc(r[0])}</td><td><b>${esc(r[1])}</b></td><td>${esc(r[2])}</td><td>${esc(r[3])}%</td><td>${esc(r[4])}</td></tr>`),"No scan rows yet.");
$("tradeTable").innerHTML=table(["Time","Symbol","Side","PnL","Conf"],(s.trades||[]).slice(-42).reverse().map(r=>{const p=Number(r.PnL||r.pnl||0);return `<tr><td>${esc(r.Timestamp||r.timestamp||"")}</td><td><b>${esc(r.Symbol||r.symbol||"")}</b></td><td>${esc(r.Side||r.side||r.Result||"")}</td><td class="${cls(p)}">${p.toFixed(6)}</td><td>${esc(r.Confidence||r.confidence||"")}</td></tr>`}),"No trades logged yet.");
const allAlerts=[...(s.model_alerts||[]),...(s.errors||[])].slice(-12);$("alerts").innerHTML=allAlerts.length?table(["Latest Alert"],allAlerts.map(e=>`<tr><td class="terminal">${esc(String(e).slice(-320))}</td></tr>`),""):"<div class='sub'>No recent model or runtime alerts.</div>";$("processes").textContent=(s.process||[]).join("\\n")||"No engine processes found";}
function miniLine(id,vals,color="#00e5c3"){const c=$(id),x=c.getContext("2d"),w=c.width=c.clientWidth*2,h=c.height=260*2;x.clearRect(0,0,w,h);x.strokeStyle="#263247";for(let i=1;i<4;i++){x.beginPath();x.moveTo(0,h*i/4);x.lineTo(w,h*i/4);x.stroke()}if(!vals.length)return;const mn=Math.min(...vals,0),mx=Math.max(...vals,0),sp=mx-mn||1;x.beginPath();vals.forEach((v,i)=>{const px=i/Math.max(1,vals.length-1)*w,py=h-((v-mn)/sp*h*.78+h*.11);i?x.lineTo(px,py):x.moveTo(px,py)});x.strokeStyle=color;x.lineWidth=5;x.stroke()}
function miniBars(id,rows,key="pnl"){const c=$(id),x=c.getContext("2d"),w=c.width=c.clientWidth*2,h=c.height=260*2;x.clearRect(0,0,w,h);if(!rows.length)return;const vals=rows.map(r=>Number(r[key]||0)),mx=Math.max(...vals.map(v=>Math.abs(v)),.01),bw=w/rows.length*.68,base=h*.52;rows.forEach((r,i)=>{const v=Number(r[key]||0),bh=Math.abs(v)/mx*h*.42;x.fillStyle=v>=0?"#16c784":"#ea3943";x.fillRect(i*w/rows.length+(w/rows.length-bw)/2,v>=0?base-bh:base,bw,bh)})}
function makeCharts(){const scan=(s.scan_rows||[]).map(r=>Number(r[3]||0)),equityRaw=(s.equity_curve||[]).map(r=>Number(r.pnl||0)),dailyRaw=(s.daily_pnl||[]).slice(-45),symRaw=(s.symbol_stats||[]).slice(0,10),market=(s.market||[]);
const equity=equityRaw.length?equityRaw:scan,daily=dailyRaw.length?dailyRaw:market.map(m=>({date:m.symbol,pnl:m.change})),sym=symRaw.length?symRaw:market.map(m=>({symbol:m.symbol,pnl:m.change,win_rate:Math.abs(m.change)}));
$("equityTitle").textContent=equityRaw.length?"Strategy Equity":"Live AI Confidence";$("dailyTitle").textContent=dailyRaw.length?"Daily PnL Pulse":"Binance 24h Market Pulse";
if(!window.Chart){miniLine("equityChart",equity);miniBars("dailyChart",daily);miniBars("symbolChart",sym);return}Chart.defaults.color="#9aa8bf";Chart.defaults.borderColor="#263247";
new Chart($("equityChart"),{type:"line",data:{labels:equity.map((_,i)=>i+1),datasets:[{label:equityRaw.length?"Equity PnL":"Scan Confidence %",data:equity,borderColor:"#00e5c3",backgroundColor:"rgba(0,229,195,.16)",fill:true,tension:.35,pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:true,labels:{boxWidth:10}}},scales:{x:{display:false},y:{ticks:{callback:v=>equityRaw.length?"$"+v:v+"%"}}}}});
new Chart($("dailyChart"),{type:"bar",data:{labels:daily.map(r=>r.date),datasets:[{label:dailyRaw.length?"Daily PnL":"24h Market %",data:daily.map(r=>r.pnl),backgroundColor:daily.map(r=>Number(r.pnl)>=0?"#16c784":"#ea3943"),borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:true,labels:{boxWidth:10}}},scales:{x:{display:false},y:{ticks:{callback:v=>dailyRaw.length?"$"+v:v+"%"}}}}});
new Chart($("symbolChart"),{type:"bar",data:{labels:sym.map(r=>r.symbol),datasets:[{label:symRaw.length?"PnL":"24h %",data:sym.map(r=>r.pnl),backgroundColor:sym.map(r=>Number(r.pnl)>=0?"#16c784":"#ea3943"),borderRadius:5},{label:symRaw.length?"Win %":"Abs move",data:sym.map(r=>symRaw.length?r.win_rate/100:r.win_rate),backgroundColor:"#6ea8ff",borderRadius:5}]},options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{boxWidth:10}}},scales:{x:{ticks:{callback:v=>Number(v).toFixed(2)}}}}});}
function setupTabs(){const saved=localStorage.getItem("aegis-view")||"desk";document.querySelectorAll(".tab").forEach(btn=>btn.addEventListener("click",()=>{document.querySelectorAll(".tab,.view").forEach(el=>el.classList.remove("active"));btn.classList.add("active");$(btn.dataset.view).classList.add("active");localStorage.setItem("aegis-view",btn.dataset.view)}));const btn=document.querySelector(`[data-view="${saved}"]`);if(btn)btn.click()}
try{renderShell();makeCharts();setupTabs()}catch(e){document.body.insertAdjacentHTML("afterbegin",`<div style="position:sticky;top:0;z-index:9;background:#ea3943;color:white;padding:10px;font-family:Consolas">Dashboard render error: ${String(e.message||e)}</div>`)}setTimeout(()=>location.reload(),30000);
</script></body></html>""".replace("__DATA__", _safe_json(s))


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    if path == "/health":
        snapshot = _snapshot()
        public_health = {
            "status": "ok" if snapshot["health"] == "OK" else "degraded",
            "service": "aegisquant-web",
            "engine": snapshot["engine_ok"],
            "watchdog": snapshot["watchdog_ok"],
        }
        body = json.dumps(public_health, separators=(",", ":")).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
        return [body]
    if path == "/api/dashboard":
        if not _authorized(environ):
            body = b'{"error":"unauthorized"}'
            start_response("401 Unauthorized", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
            return [body]
        body = json.dumps(_snapshot(), separators=(",", ":")).encode("utf-8")
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]
    body = render_page().encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))])
    return [body]
