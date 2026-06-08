import csv
import hashlib
import hmac
import html
import json
import os
import re
import subprocess
import time
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
<title>AegisQuant Live Dashboard</title>
<style>
:root{--bg:#070a12;--panel:#101622;--panel2:#151d2c;--line:#263246;--text:#edf3ff;--muted:#8d9ab5;--teal:#5ef4d3;--blue:#7aa2ff;--green:#44d48c;--red:#ff667d;--yellow:#ffd166}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#142338 0,#070a12 42%,#06080d 100%);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}
.wrap{max-width:1360px;margin:0 auto;padding:24px}.hero{display:grid;grid-template-columns:1.35fr .65fr;gap:18px;align-items:stretch}.panel{background:linear-gradient(180deg,rgba(21,29,44,.96),rgba(12,17,27,.96));border:1px solid var(--line);border-radius:10px;box-shadow:0 18px 60px rgba(0,0,0,.24)}
.title{padding:26px}.eyebrow{color:var(--teal);font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.title h1{margin:10px 0 8px;font-size:42px;line-height:1;letter-spacing:0}.title p{margin:0;color:var(--muted);max-width:780px}.status{padding:22px}.statusTop{display:flex;justify-content:space-between;gap:12px;align-items:center}.badge{border:1px solid var(--line);border-radius:999px;padding:8px 12px;font-size:12px;font-weight:900}.ok{color:var(--teal);background:rgba(94,244,211,.08)}.warn{color:var(--yellow);background:rgba(255,209,102,.08)}.bad{color:var(--red);background:rgba(255,102,125,.08)}
.ring{width:142px;height:142px;border-radius:50%;margin:20px auto 10px;background:conic-gradient(var(--teal) calc(var(--p)*1%),#223047 0);display:grid;place-items:center}.ring div{width:104px;height:104px;border-radius:50%;background:#0e1420;display:grid;place-items:center;text-align:center}.ring strong{font-size:25px}.ring span{display:block;color:var(--muted);font-size:11px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.card{padding:15px;background:rgba(16,22,34,.9);border:1px solid var(--line);border-radius:8px}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.value{margin-top:8px;font-size:23px;font-weight:900}.sub{color:var(--muted);font-size:12px;margin-top:4px}
.tabs{position:sticky;top:0;z-index:5;display:flex;gap:8px;flex-wrap:wrap;background:rgba(7,10,18,.9);backdrop-filter:blur(10px);padding:14px 0}.tabBtn{border:1px solid var(--line);background:#0f1624;color:var(--muted);border-radius:8px;padding:10px 13px;font-weight:800;cursor:pointer}.tabBtn.active{color:#06100d;background:var(--teal);border-color:var(--teal)}
.tab{display:none}.tab.active{display:block}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.section{padding:17px;margin-bottom:14px}.section h2{margin:0 0 12px;font-size:18px}.rec{display:grid;gap:10px}.recItem{border-left:4px solid var(--blue);background:#0c1320;border-radius:8px;padding:12px}.recItem.critical{border-color:var(--red)}.recItem.warning{border-color:var(--yellow)}.recItem.success{border-color:var(--green)}.recItem strong{display:block}.recItem span{color:var(--muted);font-size:13px}
canvas{width:100%;height:260px;background:#0b111d;border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}tr:hover td{background:rgba(94,244,211,.04)}.pos{color:var(--green)}.neg{color:var(--red)}pre{white-space:pre-wrap;color:var(--muted);font-size:12px;margin:0}.foot{color:var(--muted);font-size:12px;text-align:center;padding:18px}
@media(max-width:900px){.hero,.grid2{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.title h1{font-size:34px}}@media(max-width:560px){.wrap{padding:14px}.cards{grid-template-columns:1fr}.tabBtn{flex:1}.title h1{font-size:29px}}
</style></head><body><main class="wrap">
<section class="hero"><div class="panel title"><div class="eyebrow">AegisQuant Production</div><h1>Live Trading Command Center</h1><p>Root dashboard for aegisquant.web.id. Lightweight cPanel-safe UI with runtime health, growth target, signal scan, PnL analytics, and my recommendation panel.</p></div>
<aside class="panel status"><div class="statusTop"><span class="label">System Status</span><span id="healthBadge" class="badge">...</span></div><div id="growthRing" class="ring"><div><strong id="growthPct">0%</strong><span>Growth Target</span></div></div><div class="sub" id="updatedAt"></div></aside></section>
<section class="cards" id="cards"></section>
<nav class="tabs"><button class="tabBtn active" data-tab="overview">Overview</button><button class="tabBtn" data-tab="markets">Markets</button><button class="tabBtn" data-tab="performance">Performance</button><button class="tabBtn" data-tab="trades">Trades</button><button class="tabBtn" data-tab="system">System</button></nav>
<section id="overview" class="tab active"><div class="grid2"><div class="panel section"><h2>My Recommendation</h2><div id="recommendations" class="rec"></div></div><div class="panel section"><h2>Equity Curve</h2><canvas id="equityChart"></canvas></div></div><div class="panel section"><h2>Latest Symbol Scan</h2><div id="scanTable"></div></div></section>
<section id="markets" class="tab"><div class="grid2"><div class="panel section"><h2>Symbol Performance</h2><canvas id="symbolChart"></canvas></div><div class="panel section"><h2>Symbol Stats</h2><div id="symbolTable"></div></div></div></section>
<section id="performance" class="tab"><div class="grid2"><div class="panel section"><h2>Daily PnL</h2><canvas id="dailyChart"></canvas></div><div class="panel section"><h2>Risk Notes</h2><div id="riskNotes" class="rec"></div></div></div></section>
<section id="trades" class="tab"><div class="panel section"><h2>Trade History</h2><div id="tradeTable"></div></div></section>
<section id="system" class="tab"><div class="grid2"><div class="panel section"><h2>Runtime Errors</h2><div id="errors"></div></div><div class="panel section"><h2>Processes</h2><pre id="processes"></pre></div></div><div class="panel section"><h2>Model Health</h2><div id="modelAlerts"></div></div></section>
<div class="foot">Auto refresh every 30 seconds. Protected JSON remains available at /api/dashboard for Vercel or private clients.</div>
</main><script id="aegis-data" type="application/json">__DATA__</script><script>
const s=JSON.parse(document.getElementById("aegis-data").textContent);const $=id=>document.getElementById(id);const money=n=>"$"+Number(n||0).toFixed(4);const idr=n=>"Rp "+Number(n||0).toLocaleString("id-ID",{maximumFractionDigits:0});const pct=n=>Number(n||0).toFixed(2)+"%";
function cls(v){return Number(v||0)>=0?"pos":"neg"}function esc(v){return String(v??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]))}
function table(headers,rows,empty){if(!rows.length)return "<div class='sub'>"+empty+"</div>";return "<table><thead><tr>"+headers.map(h=>"<th>"+h+"</th>").join("")+"</tr></thead><tbody>"+rows.join("")+"</tbody></table>"}
function card(k,v,sub){return `<div class="card"><div class="label">${k}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`}
function drawLine(id,pts,color="#5ef4d3"){const c=$(id),x=c.getContext("2d"),w=c.width=c.offsetWidth*2,h=c.height=260*2;x.clearRect(0,0,w,h);x.strokeStyle="#263246";x.lineWidth=2;for(let i=1;i<5;i++){x.beginPath();x.moveTo(0,h*i/5);x.lineTo(w,h*i/5);x.stroke()}if(!pts.length)return;const vals=pts.map(Number),mn=Math.min(...vals,0),mx=Math.max(...vals,0),span=mx-mn||1;x.beginPath();vals.forEach((v,i)=>{const px=i/(vals.length-1||1)*w,py=h-((v-mn)/span*h*.82+h*.09);i?x.lineTo(px,py):x.moveTo(px,py)});x.strokeStyle=color;x.lineWidth=5;x.stroke()}
function drawBars(id,rows,key="pnl"){const c=$(id),x=c.getContext("2d"),w=c.width=c.offsetWidth*2,h=c.height=260*2;x.clearRect(0,0,w,h);if(!rows.length)return;const vals=rows.map(r=>Number(r[key]||0)),mx=Math.max(...vals.map(Math.abs),.01),bw=w/vals.length*.72;rows.forEach((r,i)=>{const v=Number(r[key]||0),bh=Math.abs(v)/mx*h*.42,base=h*.52;x.fillStyle=v>=0?"#44d48c":"#ff667d";x.fillRect(i*w/vals.length+(w/vals.length-bw)/2,v>=0?base-bh:base,bw,bh)})}
function render(){const t=s.trade_stats||{},c=s.cycles||{},wr=t.wins/Math.max(1,(t.wins||0)+(t.losses||0))*100,b=s.health==="OK"?"ok":s.health==="MODEL WATCH"?"warn":"bad";$("healthBadge").className="badge "+b;$("healthBadge").textContent=s.health;$("growthRing").style.setProperty("--p",Math.max(0,Math.min(100,s.growth_pct||0)));$("growthPct").textContent=pct(s.growth_pct);$("updatedAt").textContent="Updated "+s.updated_utc;
$("cards").innerHTML=[card("Balance",money(s.balance_usdt),idr(s.balance_idr)),card("Growth",pct(s.growth_pct),"Rp 300rb to Rp 10jt"),card("Cycle","#"+(s.heartbeat_cycle??"-"),"Heartbeat "+(s.heartbeat_age_sec??"-")+"s"),card("Last Scan",(c.last_candidates||0)+" / "+(c.last_executed||0),"Candidates / executed"),card("Trades",t.count||0,"Win rate "+wr.toFixed(1)+"%"),card("Closed PnL",`<span class="${cls(t.pnl)}">${money(t.pnl)}</span>`,"From trade log"),card("Positions",s.positions||0,"Open exposure"),card("Revision",s.git_rev||"unknown",s.engine_ok&&s.watchdog_ok?"Runtime online":"Check runtime")].join("");
$("recommendations").innerHTML=(s.recommendations||[]).map(r=>`<div class="recItem ${esc(r.level)}"><strong>${esc(r.title)}</strong><span>${esc(r.detail)}</span></div>`).join("");
$("riskNotes").innerHTML=[["warning","Micro sizing","Below 25 USDT, exchange minimum notional and fees can dominate edge."],["success","Best setup bias","Keep capital rotation conservative: only rotate for a clearly stronger edge."],["warning","Security","Rotate API/cPanel keys after deployment hardening because credentials were shared during setup."]].map(r=>`<div class="recItem ${r[0]}"><strong>${r[1]}</strong><span>${r[2]}</span></div>`).join("");
$("scanTable").innerHTML=table(["Time","Symbol","Signal","Confidence","Reason"],(s.scan_rows||[]).slice().reverse().map(r=>`<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td>${esc(r[2])}</td><td>${esc(r[3])}%</td><td>${esc(r[4])}</td></tr>`),"No scan rows yet.");
$("tradeTable").innerHTML=table(["Time","Symbol","Side/Result","PnL","Confidence"],(s.trades||[]).slice(-60).reverse().map(r=>{const p=Number(r.PnL||r.pnl||0);return `<tr><td>${esc(r.Timestamp||r.timestamp||"")}</td><td>${esc(r.Symbol||r.symbol||"")}</td><td>${esc(r.Side||r.side||r.Result||"")}</td><td class="${cls(p)}">${p.toFixed(6)}</td><td>${esc(r.Confidence||r.confidence||"")}</td></tr>`}),"No trades logged yet.");
$("symbolTable").innerHTML=table(["Symbol","Trades","Win Rate","PnL"],(s.symbol_stats||[]).map(r=>`<tr><td>${esc(r.symbol)}</td><td>${r.trades}</td><td>${pct(r.win_rate)}</td><td class="${cls(r.pnl)}">${money(r.pnl)}</td></tr>`),"No symbol stats yet.");
$("errors").innerHTML=(s.errors||[]).length?table(["Recent Error"],s.errors.map(e=>`<tr><td>${esc(String(e).slice(-280))}</td></tr>`),""):"<div class='sub'>No recent runtime errors.</div>";$("modelAlerts").innerHTML=(s.model_alerts||[]).length?table(["Model Alert"],s.model_alerts.map(e=>`<tr><td>${esc(String(e).slice(-280))}</td></tr>`),""):"<div class='sub'>No recent model-health alerts.</div>";$("processes").textContent=(s.process||[]).join("\\n")||"No engine processes found";
drawLine("equityChart",(s.equity_curve||[]).map(r=>r.pnl));drawBars("dailyChart",(s.daily_pnl||[]).slice(-45));drawBars("symbolChart",(s.symbol_stats||[]).slice(0,10));}
document.querySelectorAll(".tabBtn").forEach(btn=>btn.onclick=()=>{document.querySelectorAll(".tabBtn,.tab").forEach(e=>e.classList.remove("active"));btn.classList.add("active");$(btn.dataset.tab).classList.add("active");setTimeout(render,40)});render();setTimeout(()=>location.reload(),30000);
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
