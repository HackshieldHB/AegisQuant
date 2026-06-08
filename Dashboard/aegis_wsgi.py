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


def _cards(s):
    c = s["cycles"]
    t = s["trade_stats"]
    win_rate = (t["wins"] / max(1, t["wins"] + t["losses"])) * 100
    data = [
        ("Balance", f"${s['balance_usdt']:.4f}", f"Rp {s['balance_idr']:,.0f}"),
        ("Growth", f"{s['growth_pct']:.2f}%", "Rp 300,000 -> Rp 10,000,000"),
        ("Cycle", f"#{s['heartbeat_cycle']}", f"Heartbeat age: {s['heartbeat_age_sec']}s"),
        ("Last Cycle", f"{c['last_candidates']} / {c['last_executed']}", "Candidates / Executed"),
        ("Trades", str(t["count"]), f"Win rate {win_rate:.1f}%"),
        ("Total PnL", f"${t['pnl']:+.4f}", "Closed trade log"),
        ("Positions", str(s["positions"]), "Open positions"),
        ("Revision", s["git_rev"], s["updated_utc"]),
    ]
    return "".join(
        f"<div class='card'><div class='label'>{_e(k)}</div><div class='val'>{_e(v)}</div><div class='small'>{_e(sub)}</div></div>"
        for k, v, sub in data
    )


def _scan_table(rows):
    body = "".join(
        f"<tr><td>{_e(t)}</td><td>{_e(sym)}</td><td>{_e(sig)}</td><td>{_e(conf)}%</td><td>{_e(reason)}</td></tr>"
        for t, sym, sig, conf, reason in rows
    )
    return body or "<tr><td colspan='5'>No scan rows yet.</td></tr>"


def _trade_table(rows):
    if not rows:
        return "<tr><td colspan='5'>No trades logged yet.</td></tr>"
    body = []
    for row in rows[-12:]:
        body.append(
            "<tr>"
            f"<td>{_e(row.get('Timestamp', row.get('timestamp', '')))}</td>"
            f"<td>{_e(row.get('Symbol', row.get('symbol', '')))}</td>"
            f"<td>{_e(row.get('Side', row.get('side', row.get('Result', ''))))}</td>"
            f"<td>{_e(row.get('PnL', row.get('pnl', '')))}</td>"
            f"<td>{_e(row.get('Confidence', row.get('confidence', '')))}</td>"
            "</tr>"
        )
    return "".join(body)


def render_page():
    s = _snapshot()
    badge = "ok" if s["health"] == "OK" else "warn" if s["health"] == "MODEL WATCH" else "bad"
    alerts = "".join(f"<li>{_e(a[-240:])}</li>" for a in s["model_alerts"]) or "<li>No recent model-health alerts.</li>"
    errors = "".join(f"<li>{_e(a[-240:])}</li>" for a in s["errors"]) or "<li>No recent runtime errors.</li>"
    procs = "\n".join(s["process"]) or "No engine processes found"
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='30'><title>AegisQuant Dashboard</title>
<style>
:root{{--bg:#0e1117;--panel:#141824;--line:#2d3548;--text:#ccd6f6;--muted:#8892b0;--teal:#64ffda;--yellow:#ffd740;--red:#ff6b6b}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}}.wrap{{max-width:1240px;margin:0 auto;padding:28px 18px 42px}}
.top{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}}h1{{margin:0;color:var(--teal);font-size:34px}}p{{color:var(--muted)}}
.badge{{border:1px solid var(--line);border-radius:8px;padding:10px 14px;font-weight:800;white-space:nowrap}}.ok{{color:var(--teal)}}.warn{{color:var(--yellow)}}.bad{{color:var(--red)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:18px 0}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}}
.label{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}}.val{{margin-top:8px;font-size:22px;font-weight:800}}.small{{font-size:12px;color:var(--muted)}}
section{{margin-top:16px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}}h2{{margin:0 0 12px;font-size:18px;color:#e6f1ff}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{border-bottom:1px solid var(--line);padding:9px;text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:11px;text-transform:uppercase}}
ul{{margin:0;padding-left:18px;color:var(--muted)}}li{{margin:7px 0}}pre{{white-space:pre-wrap;color:var(--muted);margin:0;font-size:12px}}code{{color:var(--teal)}}
</style></head><body><main class='wrap'>
<div class='top'><div><h1>AegisQuant Dashboard</h1><p>Production monitor for cPanel Passenger. Auto-refreshes every 30 seconds.</p></div><div class='badge {badge}'>{_e(s['health'])}</div></div>
<div class='grid'>{_cards(s)}</div>
<section><h2>Latest Symbol Scan</h2><table><thead><tr><th>Time</th><th>Symbol</th><th>Signal</th><th>Confidence</th><th>Reason</th></tr></thead><tbody>{_scan_table(s['scan_rows'])}</tbody></table></section>
<section><h2>Trade History</h2><table><thead><tr><th>Time</th><th>Symbol</th><th>Side/Result</th><th>PnL</th><th>Confidence</th></tr></thead><tbody>{_trade_table(s['trades'])}</tbody></table></section>
<section><h2>Model Health</h2><ul>{alerts}</ul></section>
<section><h2>Runtime Errors</h2><ul>{errors}</ul></section>
<section><h2>Processes</h2><pre>{_e(procs)}</pre></section>
<p class='small'>JSON status: <code>/health</code>. Full Streamlit UI still requires a websocket-capable runtime; this WSGI dashboard is the cPanel-safe version.</p>
</main></body></html>"""


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
