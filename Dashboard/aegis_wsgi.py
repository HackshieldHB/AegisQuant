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
MODEL_DIR = os.path.join(APP_ROOT, "Data", "Models")
SHADOW_SUMMARY = os.path.join(STATE_DIR, "..", "shadow_summary.json")
IDR_RATE = 16000
START_IDR = 300000
TARGET_IDR = 10000000
MARKET_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "SHIBUSDT", "PEPEUSDT")
_MARKET_CACHE = {"ts": 0.0, "rows": []}
_TICKER_CACHE = {"ts": 0.0, "payload": []}
_GAINERS_CACHE = {"ts": 0.0, "rows": []}
# Quote-volume floor so the "top gainers" list shows liquid movers, not illiquid
# micro-cap pumps that cannot be traded safely.
GAINER_MIN_QUOTE_VOL = 8_000_000.0
GAINER_LIMIT = 12
# Leveraged tokens / fiat-style pairs we never want in a spot gainers board.
_GAINER_EXCLUDE = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
_GAINER_STABLES = ("USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "DAIUSDT", "EURUSDT")


def _momentum_signal(change, price, high, low):
    """Transparent 24h momentum bias (NOT the ML trading signal).

    Combines the 24h % change with where the last price sits inside the 24h
    range so a coin that is up but fading off its highs is not over-rated.
    Returns (label, css_class, arrow).
    """
    try:
        rng = float(high) - float(low)
        pos = (float(price) - float(low)) / rng if rng > 0 else 0.5
    except Exception:
        pos = 0.5
    ch = float(change or 0)
    if ch >= 5 and pos >= 0.66:
        return ("STRONG BUY", "sig-strbuy", "▲▲")
    if ch >= 1.5 and pos >= 0.50:
        return ("BUY", "sig-buy", "▲")
    if ch <= -5 and pos <= 0.34:
        return ("STRONG SELL", "sig-strsell", "▼▼")
    if ch <= -1.5 and pos <= 0.50:
        return ("SELL", "sig-sell", "▼")
    return ("NEUTRAL", "sig-neutral", "—")


def _fetch_24hr():
    """Full Binance 24h ticker payload, cached 20s and shared by all market views."""
    if time.time() - _TICKER_CACHE["ts"] < 20 and _TICKER_CACHE["payload"]:
        return _TICKER_CACHE["payload"]
    try:
        request = urllib.request.Request(
            "https://api.binance.com/api/v3/ticker/24hr",
            headers={"User-Agent": "AegisQuant-Dashboard/1.0"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        _TICKER_CACHE.update({"ts": time.time(), "payload": payload})
    except Exception:
        pass
    return _TICKER_CACHE["payload"]


def _row_from_ticker(item):
    price = float(item.get("lastPrice") or 0)
    high = float(item.get("highPrice") or 0)
    low = float(item.get("lowPrice") or 0)
    change = float(item.get("priceChangePercent") or 0)
    label, sig_cls, arrow = _momentum_signal(change, price, high, low)
    return {
        "symbol": item["symbol"].replace("USDT", "/USDT"),
        "price": price,
        "change": change,
        "high": high,
        "low": low,
        "volume": float(item.get("quoteVolume") or 0),
        "signal": label,
        "signal_class": sig_cls,
        "signal_arrow": arrow,
    }


def _top_gainers():
    if time.time() - _GAINERS_CACHE["ts"] < 20 and _GAINERS_CACHE["rows"]:
        return _GAINERS_CACHE["rows"]
    payload = _fetch_24hr()
    movers = []
    for item in payload:
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym in _GAINER_STABLES or any(sym.endswith(x) for x in _GAINER_EXCLUDE):
            continue
        try:
            if float(item.get("quoteVolume") or 0) < GAINER_MIN_QUOTE_VOL:
                continue
        except Exception:
            continue
        movers.append(item)
    movers.sort(key=lambda it: float(it.get("priceChangePercent") or 0), reverse=True)
    rows = [_row_from_ticker(it) for it in movers[:GAINER_LIMIT]]
    if rows:
        _GAINERS_CACHE.update({"ts": time.time(), "rows": rows})
    return _GAINERS_CACHE["rows"]


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
    latest_signals = []
    for row in latest_rows:
        support_match = re.search(r"support=(\d+)/(\d+)", row[4])
        support = int(support_match.group(1)) if support_match else 0
        support_total = int(support_match.group(2)) if support_match else 5
        confidence = int(row[3])
        latest_signals.append({
            "symbol": row[1],
            "state": row[2],
            "confidence": confidence,
            "support": support,
            "support_total": support_total,
            "gap": max(0, required_confidence - confidence),
            "reason": row[4],
        })
    latest_signals.sort(key=lambda item: (item["confidence"], item["support"]), reverse=True)
    return {
        "samples": len(rows),
        "states": states,
        "blockers": blocker_rows[:6],
        "avg_confidence": round(sum(confidences) / len(confidences), 1) if confidences else 0.0,
        "best_symbol": best_row[1] if best_row else None,
        "best_confidence": best_confidence,
        "required_confidence": required_confidence,
        "confidence_gap": max(0, required_confidence - best_confidence),
        "latest_signals": latest_signals,
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
    """Watchlist rows (the configured MARKET_SYMBOLS) with momentum signals."""
    if time.time() - _MARKET_CACHE["ts"] < 20 and _MARKET_CACHE["rows"]:
        return _MARKET_CACHE["rows"]
    payload = _fetch_24hr()
    wanted = set(MARKET_SYMBOLS)
    rows = []
    for item in payload:
        if item.get("symbol") not in wanted:
            continue
        rows.append(_row_from_ticker(item))
    if rows:
        rows.sort(key=lambda row: MARKET_SYMBOLS.index(row["symbol"].replace("/", "")))
        _MARKET_CACHE.update({"ts": time.time(), "rows": rows})
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


def _model_health():
    """Per-symbol model card from training metadata: accuracy, calibration, age."""
    rows = []
    now = datetime.now(timezone.utc)
    for sym in MARKET_SYMBOLS:
        base = sym.replace("USDT", "")
        path = os.path.join(MODEL_DIR, f"RandomForest_CRYPTO_{sym}_metadata.json")
        meta = _json(path, {})
        if not meta:
            rows.append({"symbol": base, "status": "missing"})
            continue
        wf = meta.get("walk_forward", {}) or {}
        cal = meta.get("calibration", {}) or {}
        ver = meta.get("version", "")
        age_days = None
        try:
            age_days = round((now - datetime.fromisoformat(ver)).total_seconds() / 86400, 1)
        except Exception:
            pass
        acc = meta.get("global_test_accuracy")
        rows.append({
            "symbol": base,
            "status": "ok",
            "accuracy": round(acc * 100, 1) if isinstance(acc, (int, float)) else None,
            "wf_mean": round(wf.get("mean_accuracy") * 100, 1) if isinstance(wf.get("mean_accuracy"), (int, float)) else None,
            "wf_consistent": bool(wf.get("consistent")),
            "brier": round(cal.get("multiclass_brier"), 3) if isinstance(cal.get("multiclass_brier"), (int, float)) else None,
            "ece": round(cal.get("expected_calibration_error"), 3) if isinstance(cal.get("expected_calibration_error"), (int, float)) else None,
            "calibrated": bool(cal),
            "age_days": age_days,
            "version": ver[:16] if ver else "",
        })
    return rows


def _shadow_scorecard():
    """Shadow-evaluator honesty scorecard: win-rate / expectancy by threshold."""
    summ = _json(os.path.normpath(SHADOW_SUMMARY), {})
    if not summ:
        return {}
    rows = []
    for t in summ.get("thresholds", []):
        if (t.get("samples") or 0) <= 0:
            continue
        rows.append({
            "threshold": t.get("threshold"),
            "samples": t.get("samples"),
            "win_rate": round((t.get("win_rate") or 0) * 100, 1),
            "expectancy": round((t.get("expectancy") or 0) * 100, 3),
            "profit_factor": round(t.get("profit_factor"), 2) if isinstance(t.get("profit_factor"), (int, float)) else None,
        })
    return {
        "completed_samples": summ.get("completed_samples", 0),
        "pending_samples": summ.get("pending_samples", 0),
        "horizon_bars": summ.get("horizon_bars"),
        "recommendation_ready": summ.get("recommendation_ready", False),
        "recommended_threshold": summ.get("recommended_threshold"),
        "rows": rows,
    }


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
    model_status = "WATCH" if model_alerts else "OK"
    snapshot = {
        "health": health,
        "model_status": model_status,
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
        "gainers": _top_gainers(),
        "model_health": _model_health(),
        "shadow": _shadow_scorecard(),
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
<title>AegisQuant Dashboard</title><script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#f4f7fb;--ink:#182230;--muted:#667085;--panel:#fff;--panel2:#f8fafc;--line:#e4e9f0;--cyan:#087f8c;--gold:#b7791f;--green:#087a55;--red:#c9363e;--blue:#2563eb;--nav:#101828}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,Segoe UI,Arial,sans-serif;letter-spacing:0}.appShell{min-height:100vh;display:grid;grid-template-columns:224px minmax(0,1fr)}.sidebar{position:sticky;top:0;height:100vh;background:var(--nav);color:#fff;padding:22px 14px;display:flex;flex-direction:column}.logo{padding:0 10px 22px;border-bottom:1px solid #344054}.logo strong{display:block;font-size:20px}.logo span{color:#98a2b3;font-size:11px}.tabs{display:grid;gap:5px;margin-top:20px}.tab{appearance:none;border:0;background:transparent;color:#98a2b3;text-align:left;font-weight:750;padding:11px 12px;border-radius:6px;cursor:pointer}.tab:hover,.tab.active{color:#fff;background:#24324a}.sideFoot{margin-top:auto;color:#98a2b3;font-size:11px;padding:12px}.main{min-width:0}.topbar{height:72px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 26px}.topbar h1{font-size:20px;margin:0}.topbar p{margin:4px 0 0;color:var(--muted);font-size:12px}.statusLine{display:flex;align-items:center;gap:9px}.shell{max-width:1500px;margin:0 auto;padding:22px 26px}.badge{display:inline-flex;border-radius:999px;border:1px solid var(--line);padding:6px 9px;font-size:11px;font-weight:850}.ok{color:var(--green);background:#ecfdf3}.warn{color:var(--gold);background:#fffaeb}.bad{color:var(--red);background:#fff1f2}
.ticker{display:flex;gap:8px;overflow-x:auto;margin-bottom:14px;scrollbar-width:thin}.tick{flex:1;min-width:145px;border:1px solid var(--line);background:#fff;border-radius:7px;padding:10px 12px}.tick b{display:block}.tick span{font-size:12px}.pos{color:var(--green)}.neg{color:var(--red)}.muted{color:var(--muted)}
.sig{display:inline-flex;align-items:center;gap:4px;border-radius:999px;padding:3px 9px;font-size:10px;font-weight:850;letter-spacing:.04em;border:1px solid var(--line);white-space:nowrap}.sig-strbuy{color:#fff;background:var(--green);border-color:var(--green)}.sig-buy{color:var(--green);background:#ecfdf3;border-color:#aee5cf}.sig-neutral{color:var(--muted);background:#f1f4f8}.sig-sell{color:var(--red);background:#fff1f2;border-color:#f3c0c4}.sig-strsell{color:#fff;background:var(--red);border-color:var(--red)}
.gain{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.gainCell{border:1px solid var(--line);border-radius:8px;padding:12px;background:#fff;position:relative;overflow:hidden}.gainCell .rank{position:absolute;top:8px;right:9px;font-size:10px;font-weight:850;color:var(--muted)}.gainCell b{display:block;font-size:13px}.gainCell .gp{font-size:20px;font-weight:850;margin:6px 0 2px}.gainCell .gpx{font-size:11px;color:var(--muted)}.gainCell .sig{margin-top:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px}.label{font-size:10px;color:var(--muted);font-weight:850;text-transform:uppercase;letter-spacing:.1em}.value{font-size:25px;font-weight:850;margin-top:8px}.sub{font-size:12px;color:var(--muted);margin-top:4px}.layout{display:grid;grid-template-columns:1.4fr .6fr;gap:14px;margin-top:14px}.panel{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px}.panel h2{margin:0 0 14px;font-size:16px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.chartBox{position:relative;height:270px;max-height:270px;overflow:hidden}.chart{display:block;width:100%!important;height:270px!important;max-height:270px!important}.heat{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.heatCell{min-height:100px;min-width:0;border-radius:7px;padding:13px;border:1px solid var(--line);background:var(--panel2);overflow:hidden}.heatCell b,.heatCell span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.heatCell span{font-size:11px;margin-top:5px}.heatCell strong{display:block;font-size:21px;margin-top:10px}.view{display:none}.view.active{display:block}.scrollTable{max-height:560px;overflow:auto}.verdict{border:1px solid #f4cf7a;background:#fffaeb;padding:13px;border-radius:7px;margin-bottom:12px}.verdict strong{color:#8a5b12;font-size:14px}.verdict span{display:block;color:var(--muted);font-size:12px;margin-top:4px}.rec{display:grid;gap:9px;max-height:225px;overflow:auto}.recItem{border:1px solid var(--line);border-left:4px solid var(--blue);background:#fff;border-radius:7px;padding:11px}.recItem.critical{border-left-color:var(--red)}.recItem.warning{border-left-color:var(--gold)}.recItem.success{border-left-color:var(--green)}.recItem strong{display:block}.recItem span{display:block;color:var(--muted);font-size:12px;margin-top:3px}.funnel{display:grid;gap:9px}.funnelRow{display:grid;grid-template-columns:130px 1fr 34px;gap:8px;align-items:center;font-size:12px}.funnelTrack{height:8px;background:#eef2f6;border-radius:999px;overflow:hidden}.funnelFill{height:100%;background:linear-gradient(90deg,#f0b429,var(--red));border-radius:999px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{border-bottom:1px solid var(--line);padding:11px 9px;text-align:left;vertical-align:top}th{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em;background:#f8fafc;position:sticky;top:0}tr:hover td{background:#f8fafc}.terminal{font-family:Consolas,Menlo,monospace;font-size:11px;color:#344054;white-space:pre-wrap}.bar{height:8px;background:#eef2f6;border-radius:999px;overflow:hidden;margin-top:10px}.fill{height:100%;background:var(--blue);width:0}.foot{text-align:center;color:var(--muted);font-size:11px;padding:18px}
@media(max-width:1050px){.appShell{grid-template-columns:1fr}.sidebar{position:static;height:auto;padding:12px}.logo{border:0;padding:4px 8px}.tabs{display:flex;overflow-x:auto;margin-top:8px}.tab{white-space:nowrap}.sideFoot{display:none}.layout,.grid2{grid-template-columns:1fr}.topbar{padding:0 16px}.shell{padding:16px}.cards{grid-template-columns:repeat(3,1fr)}}@media(max-width:650px){.cards,.heat{grid-template-columns:1fr 1fr}.statusLine .muted,#modelBadge{display:none}.topbar{height:72px}.topbar p{max-width:210px}.funnelRow{grid-template-columns:105px 1fr 28px}}
</style></head><body><div class="appShell"><aside class="sidebar"><div class="logo"><strong>AegisQuant</strong><span>Automated trading</span></div><nav class="tabs"><button class="tab active" data-view="overview">Overview</button><button class="tab" data-view="markets">Markets</button><button class="tab" data-view="signals">Signals</button><button class="tab" data-view="models">Models</button><button class="tab" data-view="trades">Trades</button><button class="tab" data-view="system">System</button></nav><div class="sideFoot">Live production<br>Binance Spot</div></aside><main class="main"><header class="topbar"><div><h1 id="pageTitle">Overview</h1><p>Portfolio, market and execution monitoring</p></div><div class="statusLine"><span id="healthBadge" class="badge">LOADING</span><span id="modelBadge" class="badge">MODEL</span><span id="updatedAt" class="muted"></span></div></header><div class="shell">
<section class="ticker" id="ticker"></section><section class="cards" id="cards"></section>
<section id="overview" class="view active"><section class="layout"><div><div class="grid2"><section class="panel"><h2 id="equityTitle">Strategy Equity</h2><div class="chartBox"><canvas id="equityChart" class="chart"></canvas></div></section><section class="panel"><h2 id="dailyTitle">Daily PnL</h2><div class="chartBox"><canvas id="dailyChart" class="chart"></canvas></div></section></div><section class="panel" style="margin-top:14px"><h2>Latest signal readiness</h2><div id="readinessTable"></div></section></div><aside><section class="panel"><h2>Execution status</h2><div id="executionVerdict"></div><div id="executionDiagnosis"></div></section><section class="panel" style="margin-top:14px"><h2>Recommendations</h2><div id="recommendations" class="rec"></div></section></aside></section></section>
<section id="markets" class="view"><section class="panel"><h2>🔥 Top Gainers (24h) · Binance Spot</h2><p class="sub" style="margin:-8px 0 12px">Most significant 24h movers with liquidity over $8M. Signal = 24h momentum bias (naik / turun), not the ML trading signal.</p><div id="gainers" class="gain"></div></section><div class="grid2" style="margin-top:14px"><section class="panel"><h2>⭐ Watchlist overview</h2><div id="heatmap" class="heat"></div></section><section class="panel"><h2>24h performance</h2><div class="chartBox"><canvas id="symbolChart" class="chart"></canvas></div></section></div><section class="panel" style="margin-top:14px"><h2>⭐ Watchlist details</h2><div id="marketTable"></div></section></section>
<section id="signals" class="view"><section class="panel"><h2>Signal history</h2><div id="scanTable" class="scrollTable"></div></section></section>
<section id="models" class="view"><section class="panel"><h2>🧠 Model health &amp; calibration</h2><p class="sub" style="margin:-8px 0 12px">Per-symbol training metadata. Lower Brier = better; ECE near 0 = well-calibrated (confidence matches reality).</p><div id="modelGrid" class="gain"></div></section><section class="panel" style="margin-top:14px"><h2>📊 Shadow scorecard — should we trade?</h2><p class="sub" style="margin:-8px 0 12px">Out-of-sample paper outcomes by confidence threshold. This decides go-live: expectancy must be positive over 100+ samples.</p><div id="shadowVerdict"></div><div id="shadowTable"></div></section></section>
<section id="trades" class="view"><section class="panel"><h2>Trade history</h2><div id="tradeTable" class="scrollTable"></div></section></section>
<section id="system" class="view"><div class="grid2"><div class="panel"><h2>Model and runtime alerts</h2><div id="alerts" class="scrollTable"></div></div><div class="panel"><h2>Processes</h2><pre id="processes" class="terminal"></pre></div></div></section>
<div class="foot">Refreshes every 30 seconds · Private API protected</div>
</div></main></div><script id="aegis-data" type="application/json">__DATA__</script><script>
const s=JSON.parse(document.getElementById("aegis-data").textContent),$=id=>document.getElementById(id);
const money=n=>"$"+Number(n||0).toFixed(4),idr=n=>"Rp "+Number(n||0).toLocaleString("id-ID",{maximumFractionDigits:0}),pct=n=>Number(n||0).toFixed(2)+"%",cls=v=>Number(v||0)>=0?"pos":"neg";
const esc=v=>String(v??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));
function table(h,r,e){return r.length?`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join("")}</tr></thead><tbody>${r.join("")}</tbody></table>`:`<div class="sub">${e}</div>`}
function card(k,v,sub){return `<div class="card"><div class="label">${k}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`}
function renderShell(){const t=s.trade_stats||{},c=s.cycles||{},dx=s.execution_diagnostics||{},b=s.health==="OK"?"ok":"bad",mb=s.model_status==="OK"?"ok":"warn";$("healthBadge").className="badge "+b;$("healthBadge").textContent=s.health==="OK"?"ENGINE ONLINE":"ENGINE OFFLINE";$("modelBadge").className="badge "+mb;$("modelBadge").textContent="MODEL "+(s.model_status||"UNKNOWN");$("updatedAt").textContent=s.updated_utc;
$("ticker").innerHTML=(s.market||[]).map(m=>`<div class="tick"><b>${esc(m.symbol)}</b><span>${Number(m.price).toLocaleString("en-US",{maximumFractionDigits:m.price<1?8:2})}</span><span class="${cls(m.change)}"> ${pct(m.change)}</span></div>`).join("")||"<div class='sub'>Live Binance ticker unavailable.</div>";
$("cards").innerHTML=[card("Portfolio",money(s.balance_usdt),idr(s.balance_idr)),card("Target progress",pct(s.growth_pct),"Rp 300K to Rp 10M"),card("Open positions",s.positions||0,"Binance Spot"),card("Latest cycle","#"+(s.heartbeat_cycle??"-"),"Heartbeat "+(s.heartbeat_age_sec??"-")+"s"),card("Best setup",(dx.best_symbol||"—").replace("/USDT",""),(dx.best_confidence||0)+"% confidence"),card("Closed PnL",`<span class="${cls(t.pnl)}">${money(t.pnl)}</span>`,(t.count||0)+" trades")].join("");
$("recommendations").innerHTML=(s.recommendations||[]).map(r=>`<div class="recItem ${esc(r.level)}"><strong>${esc(r.title)}</strong><span>${esc(r.detail)}</span></div>`).join("");
const blocks=dx.blockers||[],maxBlock=Math.max(1,...blocks.map(b=>b.count));$("executionVerdict").innerHTML=dx.best_symbol?`<div class="verdict"><strong>Waiting for a qualified setup</strong><span>${esc(dx.best_symbol)} leads at ${dx.best_confidence||0}%. AI-only entry requires ${dx.required_confidence||"—"}%, a ${dx.confidence_gap||0}-point gap. No order reached Binance.</span></div>`:"<div class='sub'>Waiting for complete scan diagnostics.</div>";$("executionDiagnosis").innerHTML=`<div class="funnel">${blocks.slice(0,4).map(b=>`<div class="funnelRow"><span>${esc(b.reason)}</span><div class="funnelTrack"><div class="funnelFill" style="width:${b.count/maxBlock*100}%"></div></div><b>${b.count}</b></div>`).join("")||"<div class='sub'>No execution diagnostics yet.</div>"}</div>`;
$("readinessTable").innerHTML=table(["Asset","Confidence","TA support","Gap","State"],(dx.latest_signals||[]).map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td>${r.confidence}%</td><td>${r.support}/${r.support_total}</td><td>${r.gap} pts</td><td><span class="badge warn">${esc(r.state)}</span></td></tr>`),"No signal readiness data yet.");
const fnum=v=>Number(v).toLocaleString("en-US",{maximumFractionDigits:Number(v)<1?8:2});
const sigBadge=m=>`<span class="sig ${esc(m.signal_class||"sig-neutral")}">${esc(m.signal_arrow||"—")} ${esc(m.signal||"NEUTRAL")}</span>`;
$("gainers").innerHTML=(s.gainers||[]).map((m,i)=>{const ch=Number(m.change||0),a=Math.min(Math.abs(ch)/15,.8),bg=`linear-gradient(180deg,rgba(8,122,85,${.05+a*.18}),#fff)`;return `<div class="gainCell" style="background:${bg}"><span class="rank">#${i+1}</span><b>${esc(m.symbol)}</b><div class="gp ${cls(ch)}">${ch>=0?"+":""}${pct(ch)}</div><div class="gpx">${fnum(m.price)} · vol $${Number(m.volume||0).toLocaleString("en-US",{maximumFractionDigits:0,notation:"compact"})}</div>${sigBadge(m)}</div>`}).join("")||"<div class='sub'>Live gainers unavailable.</div>";
$("heatmap").innerHTML=(s.market||[]).map(m=>{const ch=Number(m.change||0),a=Math.min(Math.abs(ch)/8,.85),bg=ch>=0?`rgba(22,199,132,${.12+a*.55})`:`rgba(234,57,67,${.12+a*.55})`;return `<div class="heatCell" style="background:${bg}"><b>${esc(m.symbol)}</b><span class="muted">${fnum(m.price)}</span><strong class="${cls(ch)}">${pct(ch)}</strong>${sigBadge(m)}</div>`}).join("");
$("marketTable").innerHTML=table(["Asset","Last price","24h","Signal","High","Low","Quote volume"],(s.market||[]).map(m=>`<tr><td><b>${esc(m.symbol)}</b></td><td>${fnum(m.price)}</td><td class="${cls(m.change)}">${pct(m.change)}</td><td>${sigBadge(m)}</td><td>${fnum(m.high)}</td><td>${fnum(m.low)}</td><td>$${Number(m.volume||0).toLocaleString("en-US",{maximumFractionDigits:0})}</td></tr>`),"Live market data unavailable.");
$("scanTable").innerHTML=table(["Time","Pair","Signal","Conf","Reason"],(s.scan_rows||[]).slice().reverse().map(r=>`<tr><td>${esc(r[0])}</td><td><b>${esc(r[1])}</b></td><td>${esc(r[2])}</td><td>${esc(r[3])}%</td><td>${esc(r[4])}</td></tr>`),"No scan rows yet.");
$("tradeTable").innerHTML=table(["Time","Symbol","Side","PnL","Conf"],(s.trades||[]).slice(-42).reverse().map(r=>{const p=Number(r.PnL||r.pnl||0);return `<tr><td>${esc(r.Timestamp||r.timestamp||"")}</td><td><b>${esc(r.Symbol||r.symbol||"")}</b></td><td>${esc(r.Side||r.side||r.Result||"")}</td><td class="${cls(p)}">${p.toFixed(6)}</td><td>${esc(r.Confidence||r.confidence||"")}</td></tr>`}),"No trades logged yet.");
const allAlerts=[...(s.model_alerts||[]),...(s.errors||[])].slice(-12);$("alerts").innerHTML=allAlerts.length?table(["Latest Alert"],allAlerts.map(e=>`<tr><td class="terminal">${esc(String(e).slice(-320))}</td></tr>`),""):"<div class='sub'>No recent model or runtime alerts.</div>";$("processes").textContent=(s.process||[]).join("\\n")||"No engine processes found";
renderModels();}
function renderModels(){const mh=s.model_health||[];$("modelGrid").innerHTML=mh.map(m=>{if(m.status!=="ok")return `<div class="gainCell"><b>${esc(m.symbol)}</b><div class="gpx">model missing</div></div>`;const fresh=(m.age_days!=null&&m.age_days<2),accCls=m.accuracy>=55?"pos":(m.accuracy<50?"neg":""),wfTag=m.wf_consistent?`<span class="sig sig-buy">consistent</span>`:`<span class="sig sig-neutral">unstable</span>`;return `<div class="gainCell"><span class="rank">${fresh?"🟢 fresh":(m.age_days!=null?m.age_days+"d":"")}</span><b>${esc(m.symbol)}</b><div class="gp ${accCls}">${m.accuracy!=null?m.accuracy+"%":"—"}</div><div class="gpx">acc · WF ${m.wf_mean!=null?m.wf_mean+"%":"—"}</div><div style="margin-top:8px;font-size:11px;color:var(--muted)">Brier ${m.brier??"—"} · ECE ${m.ece??"—"}</div><div style="margin-top:6px">${wfTag} ${m.calibrated?'<span class="sig sig-buy">calibrated</span>':'<span class="sig sig-sell">uncalibrated</span>'}</div></div>`}).join("")||"<div class='sub'>No model metadata found.</div>";
const sh=s.shadow||{};if(!sh.rows||!sh.rows.length){$("shadowVerdict").innerHTML="<div class='sub'>No shadow samples yet. The evaluator is collecting paper outcomes.</div>";$("shadowTable").innerHTML="";return}
const ready=sh.recommendation_ready,vCls=ready?"recItem success":"recItem warning",vMsg=ready?`Recommended threshold: <b>${sh.recommended_threshold}</b> — expectancy positive.`:`<b>Not ready to trade live.</b> No threshold yet shows positive expectancy over the minimum sample size.`;$("shadowVerdict").innerHTML=`<div class="${vCls}" style="margin-bottom:12px"><strong>Go-live verdict</strong><span>${vMsg} (${sh.completed_samples||0} completed, ${sh.pending_samples||0} pending · horizon ${sh.horizon_bars||"?"} bars)</span></div>`;
$("shadowTable").innerHTML=table(["Conf threshold","Samples","Win rate","Expectancy","Profit factor"],(sh.rows||[]).map(r=>{const good=r.expectancy>0;return `<tr><td><b>${r.threshold}</b></td><td>${r.samples}</td><td>${r.win_rate}%</td><td class="${good?'pos':'neg'}">${r.expectancy}%</td><td class="${(r.profit_factor>=1)?'pos':'neg'}">${r.profit_factor??"—"}</td></tr>`}),"No populated thresholds.");}
function miniLine(id,vals,color="#00e5c3"){const c=$(id),x=c.getContext("2d"),w=c.width=c.clientWidth*2,h=c.height=260*2;x.clearRect(0,0,w,h);x.strokeStyle="#263247";for(let i=1;i<4;i++){x.beginPath();x.moveTo(0,h*i/4);x.lineTo(w,h*i/4);x.stroke()}if(!vals.length)return;const mn=Math.min(...vals,0),mx=Math.max(...vals,0),sp=mx-mn||1;x.beginPath();vals.forEach((v,i)=>{const px=i/Math.max(1,vals.length-1)*w,py=h-((v-mn)/sp*h*.78+h*.11);i?x.lineTo(px,py):x.moveTo(px,py)});x.strokeStyle=color;x.lineWidth=5;x.stroke()}
function miniBars(id,rows,key="pnl"){const c=$(id),x=c.getContext("2d"),w=c.width=c.clientWidth*2,h=c.height=260*2;x.clearRect(0,0,w,h);if(!rows.length)return;const vals=rows.map(r=>Number(r[key]||0)),mx=Math.max(...vals.map(v=>Math.abs(v)),.01),bw=w/rows.length*.68,base=h*.52;rows.forEach((r,i)=>{const v=Number(r[key]||0),bh=Math.abs(v)/mx*h*.42;x.fillStyle=v>=0?"#16c784":"#ea3943";x.fillRect(i*w/rows.length+(w/rows.length-bw)/2,v>=0?base-bh:base,bw,bh)})}
function makeCharts(){const scan=(s.scan_rows||[]).map(r=>Number(r[3]||0)),equityRaw=(s.equity_curve||[]).map(r=>Number(r.pnl||0)),dailyRaw=(s.daily_pnl||[]).slice(-45),symRaw=(s.symbol_stats||[]).slice(0,10),market=(s.market||[]);
const equity=equityRaw.length?equityRaw:scan,daily=dailyRaw.length?dailyRaw:market.map(m=>({date:m.symbol,pnl:m.change})),sym=symRaw.length?symRaw:market.map(m=>({symbol:m.symbol,pnl:m.change,win_rate:Math.abs(m.change)}));
$("equityTitle").textContent=equityRaw.length?"Strategy Equity":"Live AI Confidence";$("dailyTitle").textContent=dailyRaw.length?"Daily PnL Pulse":"Binance 24h Market Pulse";
if(!window.Chart){miniLine("equityChart",equity);miniBars("dailyChart",daily);miniBars("symbolChart",sym);return}Chart.defaults.color="#9aa8bf";Chart.defaults.borderColor="#263247";
new Chart($("equityChart"),{type:"line",data:{labels:equity.map((_,i)=>i+1),datasets:[{label:equityRaw.length?"Equity PnL":"Scan Confidence %",data:equity,borderColor:"#00e5c3",backgroundColor:"rgba(0,229,195,.16)",fill:true,tension:.35,pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:true,labels:{boxWidth:10}}},scales:{x:{display:false},y:{ticks:{callback:v=>equityRaw.length?"$"+v:v+"%"}}}}});
new Chart($("dailyChart"),{type:"bar",data:{labels:daily.map(r=>r.date),datasets:[{label:dailyRaw.length?"Daily PnL":"24h Market %",data:daily.map(r=>r.pnl),backgroundColor:daily.map(r=>Number(r.pnl)>=0?"#16c784":"#ea3943"),borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:true,labels:{boxWidth:10}}},scales:{x:{display:false},y:{ticks:{callback:v=>dailyRaw.length?"$"+v:v+"%"}}}}});
new Chart($("symbolChart"),{type:"bar",data:{labels:sym.map(r=>r.symbol),datasets:[{label:symRaw.length?"PnL":"24h %",data:sym.map(r=>r.pnl),backgroundColor:sym.map(r=>Number(r.pnl)>=0?"#16c784":"#ea3943"),borderRadius:5},{label:symRaw.length?"Win %":"Abs move",data:sym.map(r=>symRaw.length?r.win_rate/100:r.win_rate),backgroundColor:"#6ea8ff",borderRadius:5}]},options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{boxWidth:10}}},scales:{x:{ticks:{callback:v=>Number(v).toFixed(2)}}}}});}
function setupTabs(){const titles={overview:"Overview",markets:"Markets",signals:"Signals",models:"Model health",trades:"Trades",system:"System health"},saved=localStorage.getItem("aegis-view")||"overview";document.querySelectorAll(".tab").forEach(btn=>btn.addEventListener("click",()=>{document.querySelectorAll(".tab,.view").forEach(el=>el.classList.remove("active"));btn.classList.add("active");$(btn.dataset.view).classList.add("active");$("pageTitle").textContent=titles[btn.dataset.view]||"AegisQuant";localStorage.setItem("aegis-view",btn.dataset.view);window.dispatchEvent(new Event("resize"))}));const btn=document.querySelector(`[data-view="${saved}"]`)||document.querySelector('[data-view="overview"]');btn.click()}
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

