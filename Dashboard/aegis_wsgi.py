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
_LOSERS_CACHE = {"ts": 0.0, "rows": []}
STALE_AFTER_SEC = 1800  # heartbeat older than this → data considered stale
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


def _top_losers():
    if time.time() - _LOSERS_CACHE["ts"] < 20 and _LOSERS_CACHE["rows"]:
        return _LOSERS_CACHE["rows"]
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
    movers.sort(key=lambda it: float(it.get("priceChangePercent") or 0))
    rows = [_row_from_ticker(it) for it in movers[:GAINER_LIMIT]]
    if rows:
        _LOSERS_CACHE.update({"ts": time.time(), "rows": rows})
    return _LOSERS_CACHE["rows"]


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


def _max_drawdown_usd(equity_curve):
    """Max peak-to-trough drawdown in USDT from the cumulative-PnL equity curve."""
    try:
        vals = [float(r.get("pnl", 0)) for r in (equity_curve or [])]
        if not vals:
            return 0.0
        peak, mdd = vals[0], 0.0
        for v in vals:  # vals are already cumulative equity
            peak = max(peak, v)
            mdd = max(mdd, peak - v)
        return round(mdd, 4)
    except Exception:
        return 0.0


def _risk_overview(trade_stats, trades, equity_curve, scan_rows, positions, errors, balance):
    failed = sum(1 for r in (scan_rows or []) if str(r[2] if len(r) > 2 else "").upper() == "FAILED")
    rejected = [e for e in (errors or []) if "reject" in e.lower()]
    wins, losses = int(trade_stats.get("wins") or 0), int(trade_stats.get("losses") or 0)
    decided = wins + losses
    wlist, llist = [], []
    for row in (trades or []):
        try:
            p = float(row.get("PnL") or row.get("pnl") or 0)
        except Exception:
            p = 0.0
        (wlist if p > 0 else llist if p < 0 else []).append(p) if p != 0 else None
    return {
        "max_drawdown_usd": _max_drawdown_usd(equity_curve),
        "drawdown_halt_pct": 20,  # mirrors RISK.DRAWDOWN_HALT_PCT on the engine
        "open_positions": positions,
        "failed_signals": failed,
        "rejected_orders": len(rejected),
        "last_rejected": (rejected[-1][-160:] if rejected else ""),
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "wins": wins,
        "losses": losses,
        "avg_win": round(sum(wlist) / len(wlist), 4) if wlist else 0.0,
        "avg_loss": round(sum(llist) / len(llist), 4) if llist else 0.0,
        "closed_pnl": round(float(trade_stats.get("pnl") or 0), 4),
        "trades_count": int(trade_stats.get("count") or 0),
        "balance_usdt": round(float(balance or 0), 4),
    }


def _safety(flags, market, model_health, hb_age):
    fresh_models = bool(model_health) and all(
        (m.get("status") == "ok" and (m.get("age_days") is None or m.get("age_days") < 8))
        for m in model_health if m.get("status") == "ok"
    )
    has_market = bool(market)
    return [
        {"label": "Trading Engine", "ok": flags["engine_ok"] and flags["watchdog_ok"],
         "on": "Running", "off": "Stopped"},
        {"label": "Exchange API", "ok": has_market, "on": "Connected", "off": "Degraded"},
        {"label": "Models", "ok": fresh_models, "on": "Fresh", "off": "Stale"},
        {"label": "Heartbeat", "ok": (hb_age is not None and hb_age < STALE_AFTER_SEC),
         "on": "Live", "off": "Stale"},
        {"label": "Risk Guard", "ok": True, "on": "Armed (-20% halt)", "off": "Off"},
    ]


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
        "losers": _top_losers(),
        "model_health": _model_health(),
        "shadow": _shadow_scorecard(),
        "execution_diagnostics": _execution_diagnostics(log_text),
        "stale": bool(hb_age is None or hb_age > STALE_AFTER_SEC),
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    snapshot["risk"] = _risk_overview(
        trade_stats, trades, equity_curve, snapshot["scan_rows"], pos_count, errors, bal
    )
    snapshot["safety"] = _safety(
        {"engine_ok": engine_ok, "watchdog_ok": watchdog_ok}, snapshot["market"],
        snapshot["model_health"], hb_age,
    )
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
<title>AegisQuant — Automated Quant Trading Intelligence</title>
<meta name="description" content="AegisQuant monitors market movement, evaluates ML-driven signals, tracks model health, and supervises execution readiness across Binance Spot.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js" defer></script>
<style>
:root{--bg:#070b12;--bg2:#0a0f18;--panel:#0f1724;--panel2:#0c1320;--line:#1b2740;--line2:#26344f;--ink:#e9eef8;--muted:#7e8da8;--faint:#56627e;--green:#27d98c;--greenbg:#0e2c22;--red:#ff5d6e;--redbg:#2c1219;--amber:#ffba54;--amberbg:#2c2210;--cyan:#43c8f0;--cyanbg:#0b2632;--blue:#6a93ff;--purple:#b18cff;--purplebg:#191333}
*{box-sizing:border-box}::-webkit-scrollbar{height:8px;width:8px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:8px}::-webkit-scrollbar-track{background:transparent}
body{margin:0;background:radial-gradient(1200px 600px at 78% -8%,rgba(67,200,240,.06),transparent 60%),radial-gradient(900px 500px at 0% 0%,rgba(177,140,255,.05),transparent 55%),var(--bg);color:var(--ink);font-family:Inter,Segoe UI,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.mono{font-family:'JetBrains Mono',Consolas,monospace;font-variant-numeric:tabular-nums}
.pos{color:var(--green)}.neg{color:var(--red)}.muted{color:var(--muted)}.faint{color:var(--faint)}
a{color:inherit}
/* Header */
.hdr{position:sticky;top:0;z-index:40;background:rgba(8,12,20,.82);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:18px;padding:0 22px;height:62px}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.mark{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--cyan),var(--purple));display:grid;place-items:center;font-weight:900;color:#07101a;font-size:16px;box-shadow:0 0 18px rgba(67,200,240,.35)}
.brand b{font-size:16px;font-weight:800;letter-spacing:.2px;display:block;line-height:1.1}
.brand span{font-size:10.5px;color:var(--muted);letter-spacing:.3px}
.nav{display:flex;gap:3px;margin-left:6px;overflow-x:auto;scrollbar-width:none}.nav::-webkit-scrollbar{display:none}
.tab{appearance:none;border:0;background:transparent;color:var(--muted);font-weight:650;font-size:13px;padding:9px 13px;border-radius:8px;cursor:pointer;white-space:nowrap;transition:.15s}
.tab:hover{color:var(--ink);background:var(--panel)}.tab.active{color:var(--ink);background:var(--panel);box-shadow:inset 0 -2px 0 var(--cyan)}
.hdrRight{margin-left:auto;display:flex;align-items:center;gap:9px}
.envs{display:flex;gap:7px}@media(max-width:1180px){.envs{display:none}}
.refresh{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--muted);white-space:nowrap}
.live{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(39,217,140,.6);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(39,217,140,.5)}70%{box-shadow:0 0 0 7px rgba(39,217,140,0)}100%{box-shadow:0 0 0 0 rgba(39,217,140,0)}}
/* Badges */
.b{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 10px;font-size:11px;font-weight:700;border:1px solid transparent;white-space:nowrap}
.b .dot{width:6px;height:6px;border-radius:50%;background:currentColor;box-shadow:0 0 7px currentColor}
.b-ok{color:var(--green);background:var(--greenbg);border-color:#1c5a45}.b-warn{color:var(--amber);background:var(--amberbg);border-color:#5a4418}
.b-bad{color:var(--red);background:var(--redbg);border-color:#5a2230}.b-info{color:var(--cyan);background:var(--cyanbg);border-color:#1c4a5e}
.b-mut{color:var(--muted);background:var(--panel);border-color:var(--line)}
/* Layout */
.shell{max-width:1560px;margin:0 auto;padding:18px 22px 40px}
.staleBar{display:none;align-items:center;gap:10px;background:var(--amberbg);border:1px solid #5a4418;color:var(--amber);padding:10px 14px;border-radius:10px;margin-bottom:14px;font-size:13px;font-weight:600}
.strip{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:18px}
.kpi{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px;padding:15px 16px;position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--accent,var(--cyan));opacity:.9}
.kpi .kl{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:800;display:flex;align-items:center;gap:5px}
.kpi .kv{font-size:25px;font-weight:850;margin:9px 0 3px;letter-spacing:-.5px}
.kpi .kd{font-size:11.5px;color:var(--muted)}
.kpi .kt{position:absolute;top:14px;right:14px;font-size:11px;font-weight:800}
.section{display:grid;gap:16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.layout{display:grid;grid-template-columns:1.5fr .85fr;gap:16px}
.panel{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px;padding:17px}
.panel.tight{padding:14px}
.ph{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:13px}
.ph h2{margin:0;font-size:15px;font-weight:750;display:flex;align-items:center;gap:7px}
.ph .sub{margin:4px 0 0;font-size:11.5px;color:var(--muted);max-width:62ch;line-height:1.45}
.view{display:none}.view.active{display:grid;gap:16px;animation:fade .35s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
/* Tooltip */
.tip{display:inline-grid;place-items:center;width:14px;height:14px;border-radius:50%;border:1px solid var(--line2);color:var(--muted);font-size:9px;font-weight:800;cursor:help;position:relative}
.tip:hover::after{content:attr(data-tip);position:absolute;left:50%;transform:translateX(-50%);bottom:150%;width:230px;background:#060a11;border:1px solid var(--line2);color:var(--ink);padding:9px 11px;border-radius:9px;font-size:11px;font-weight:500;line-height:1.45;box-shadow:0 12px 30px rgba(0,0,0,.6);z-index:60;text-transform:none;letter-spacing:0}
/* Charts */
.chartBox{position:relative;height:248px;overflow:hidden}.chart{display:block;width:100%!important;height:248px!important}
/* Tiles (gainers/losers/models) */
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:11px}
.tile{border:1px solid var(--line);border-radius:12px;padding:13px;background:var(--panel2);position:relative;overflow:hidden;transition:.15s}
.tile:hover{border-color:var(--line2);transform:translateY(-1px)}
.tile .rk{position:absolute;top:10px;right:11px;font-size:10px;font-weight:800;color:var(--faint)}
.tile b{font-size:13.5px;font-weight:700}.tile .bp{font-size:20px;font-weight:850;margin:7px 0 1px}
.tile .bx{font-size:10.5px;color:var(--muted)}
/* Heat */
.heat{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.heatCell{border-radius:12px;padding:14px;border:1px solid var(--line);overflow:hidden}
.heatCell b{font-size:13px}.heatCell .hp{font-size:11px;color:var(--muted);margin-top:4px}.heatCell strong{display:block;font-size:20px;margin:9px 0 8px;letter-spacing:-.4px}
/* Signal pills */
.sig{display:inline-flex;align-items:center;gap:4px;border-radius:999px;padding:3px 9px;font-size:10px;font-weight:800;letter-spacing:.03em;border:1px solid var(--line);white-space:nowrap}
.sig-strbuy{color:#06120d;background:var(--green);border-color:var(--green)}.sig-buy{color:var(--green);background:var(--greenbg);border-color:#1c5a45}
.sig-neutral{color:var(--muted);background:var(--panel);border-color:var(--line)}.sig-sell{color:var(--red);background:var(--redbg);border-color:#5a2230}.sig-strsell{color:#fff;background:var(--red);border-color:var(--red)}
/* Tables */
.scrollTable{max-height:560px;overflow:auto;border-radius:10px;border:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{border-bottom:1px solid var(--line);padding:10px 12px;text-align:left;vertical-align:middle}
th{color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.08em;font-weight:800;background:var(--panel2);position:sticky;top:0;z-index:1}
td.num,th.num{text-align:right;font-family:'JetBrains Mono',monospace;font-variant-numeric:tabular-nums}
tbody tr:hover td{background:rgba(67,200,240,.04)}
/* Recommendations / verdict */
.rec{display:grid;gap:9px;max-height:330px;overflow:auto}
.recItem{border:1px solid var(--line);border-left:3px solid var(--blue);background:var(--panel2);border-radius:10px;padding:11px 13px}
.recItem.critical{border-left-color:var(--red)}.recItem.warning{border-left-color:var(--amber)}.recItem.success{border-left-color:var(--green)}.recItem.info{border-left-color:var(--cyan)}
.recItem strong{display:block;font-size:13px}.recItem span{display:block;color:var(--muted);font-size:11.5px;margin-top:3px;line-height:1.45}
.verdict{border:1px solid var(--line2);border-radius:12px;padding:14px;margin-bottom:13px;background:var(--panel2)}
.verdict.go{border-color:#1c5a45;background:var(--greenbg)}.verdict.wait{border-color:#5a4418;background:var(--amberbg)}
.verdict h3{margin:0 0 4px;font-size:14px}.verdict p{margin:0;font-size:12px;color:var(--muted);line-height:1.5}
.funnel{display:grid;gap:9px}.funnelRow{display:grid;grid-template-columns:1fr 90px 30px;gap:9px;align-items:center;font-size:11.5px}
.funnelTrack{height:7px;background:var(--bg2);border-radius:999px;overflow:hidden}.funnelFill{height:100%;background:linear-gradient(90deg,var(--amber),var(--red));border-radius:999px}
/* Stat rows (risk) */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:11px}
.stat{border:1px solid var(--line);border-radius:12px;padding:13px 14px;background:var(--panel2)}
.stat .sl{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:800}
.stat .sv{font-size:20px;font-weight:850;margin-top:6px}
.terminal{font-family:'JetBrains Mono',Consolas,monospace;font-size:11px;color:var(--muted);white-space:pre-wrap;line-height:1.6;max-height:520px;overflow:auto}
.empty{color:var(--muted);font-size:12.5px;padding:18px;text-align:center;border:1px dashed var(--line2);border-radius:10px}
.skel{background:linear-gradient(90deg,var(--panel2) 25%,var(--line) 37%,var(--panel2) 63%);background-size:400% 100%;animation:sh 1.3s infinite;border-radius:8px;height:64px}
@keyframes sh{0%{background-position:100% 0}100%{background-position:-100% 0}}
.foot{text-align:center;color:var(--faint);font-size:11px;padding:26px 10px 6px;line-height:1.7;border-top:1px solid var(--line);margin-top:26px}
@media(max-width:1100px){.kpis{grid-template-columns:repeat(3,1fr)}.layout,.grid2{grid-template-columns:1fr}}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)}.shell{padding:14px}.hdr{padding:0 12px;gap:10px}.brand span{display:none}}
</style></head><body>
<header class="hdr">
  <div class="brand"><div class="mark">A</div><div><b>AegisQuant</b><span>Automated Quant Trading Intelligence</span></div></div>
  <nav class="nav" id="nav">
    <button class="tab active" data-view="overview">Overview</button>
    <button class="tab" data-view="markets">Markets</button>
    <button class="tab" data-view="signals">Signals</button>
    <button class="tab" data-view="models">Models</button>
    <button class="tab" data-view="trades">Trades</button>
    <button class="tab" data-view="risk">Risk</button>
    <button class="tab" data-view="system">System</button>
  </nav>
  <div class="hdrRight">
    <div class="envs">
      <span class="b b-ok"><span class="dot"></span>Live Production</span>
      <span class="b b-info">Binance Spot</span>
      <span class="b b-mut">🔒 Protected API</span>
    </div>
    <div class="refresh"><span class="live"></span><span id="refreshTxt">live</span></div>
  </div>
</header>
<div class="shell">
  <div class="staleBar" id="staleBar">⚠ <span id="staleMsg">Data may be stale.</span></div>
  <section class="strip" id="safetyStrip"></section>
  <section class="kpis" id="kpis"><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div></section>

  <section id="overview" class="view active">
    <div class="layout">
      <div class="section">
        <div class="grid2">
          <section class="panel"><div class="ph"><h2 id="equityTitle">Portfolio Performance</h2></div><div class="chartBox"><canvas id="equityChart" class="chart"></canvas></div></section>
          <section class="panel"><div class="ph"><h2 id="dailyTitle">Daily PnL</h2></div><div class="chartBox"><canvas id="dailyChart" class="chart"></canvas></div></section>
        </div>
        <section class="panel"><div class="ph"><h2>Win / Loss Summary</h2></div><div id="winloss" class="stats"></div></section>
        <section class="panel"><div class="ph"><div><h2>Signal Readiness <span class="tip" data-tip="How close each symbol is to a tradeable setup — directional confidence vs the required threshold.">i</span></h2><p class="sub">Per-symbol distance to a qualified entry. No order is sent until confidence clears the gate.</p></div></div><div id="readinessTable"></div></section>
      </div>
      <aside class="section">
        <section class="panel"><div class="ph"><h2>Execution Status</h2></div><div id="executionVerdict"></div><div id="executionDiagnosis"></div></section>
        <section class="panel"><div class="ph"><h2>Recommendations</h2></div><div id="recommendations" class="rec"></div></section>
      </aside>
    </div>
  </section>

  <section id="markets" class="view">
    <div class="grid2">
      <section class="panel"><div class="ph"><div><h2>📈 Market Movers — Top Gainers (24h)</h2><p class="sub">Strongest 24h movers with liquidity above $8M. Momentum bias shows short-term direction and is separate from the ML execution signal. <span class="tip" data-tip="Momentum bias blends 24h % change with where price sits in its daily range. It is a market context cue, not the model's trade decision.">i</span></p></div></div><div id="gainers" class="tiles"></div></section>
      <section class="panel"><div class="ph"><div><h2>📉 Market Movers — Top Losers (24h)</h2><p class="sub">Weakest liquid movers over the last 24 hours. Useful for risk-off context and short-bias awareness.</p></div></div><div id="losers" class="tiles"></div></section>
    </div>
    <div class="grid2">
      <section class="panel"><div class="ph"><h2>⭐ Watchlist Overview</h2></div><div id="heatmap" class="heat"></div></section>
      <section class="panel"><div class="ph"><h2>24h Performance</h2></div><div class="chartBox"><canvas id="symbolChart" class="chart"></canvas></div></section>
    </div>
    <section class="panel"><div class="ph"><h2>⭐ Watchlist Details</h2></div><div class="scrollTable" id="marketTable"></div></section>
  </section>

  <section id="signals" class="view">
    <section class="panel"><div class="ph"><div><h2>Signal Recommendations</h2><p class="sub">Action guidance from the live engine. Momentum bias is market context; the ML execution signal is what governs orders.</p></div></div><div id="recommendations2" class="rec"></div></section>
    <section class="panel"><div class="ph"><div><h2>Signal Readiness</h2><p class="sub">Confidence and technical support per symbol versus the entry gate.</p></div></div><div id="readinessTable2"></div></section>
    <section class="panel"><div class="ph"><h2>Signal History</h2></div><div class="scrollTable" id="scanTable"></div></section>
  </section>

  <section id="models" class="view">
    <section class="panel"><div class="ph"><div><h2>🧠 Model Calibration</h2><p class="sub">Per-symbol training health. Lower <b>Brier</b> = better probabilistic accuracy; <b>ECE</b> near zero = confidence matches reality.</p></div></div><div id="modelGrid" class="tiles"></div></section>
    <section class="panel"><div class="ph"><div><h2>📊 Shadow Trading Scorecard <span class="tip" data-tip="Evaluates out-of-sample paper outcomes by confidence threshold before any live execution. Go-live requires positive expectancy and sufficient samples.">i</span></h2><p class="sub">Out-of-sample paper outcomes by confidence threshold. Go-live requires positive expectancy over a sufficient sample.</p></div></div><div id="shadowVerdict"></div><div class="scrollTable" id="shadowTable"></div></section>
  </section>

  <section id="trades" class="view">
    <section class="panel"><div class="ph"><h2>Execution Health</h2></div><div id="execHealth" class="stats"></div></section>
    <section class="panel"><div class="ph"><h2>Trade History</h2></div><div class="scrollTable" id="tradeTable"></div></section>
  </section>

  <section id="risk" class="view">
    <section class="panel"><div class="ph"><div><h2>🛡️ Risk Overview</h2><p class="sub">Capital protection and exposure. A -20% drawdown auto-halt and 5-consecutive-loss halt guard the account.</p></div></div><div id="riskStats" class="stats"></div></section>
    <section class="panel"><div class="ph"><h2>Safety Indicators</h2></div><div id="safetyGrid" class="stats"></div></section>
    <section class="panel"><div class="ph"><h2>Last Rejected / Blocked</h2></div><div id="riskLast" class="terminal"></div></section>
  </section>

  <section id="system" class="view">
    <div class="grid2">
      <section class="panel"><div class="ph"><h2>Model &amp; Runtime Alerts</h2></div><div class="scrollTable" id="alerts"></div></section>
      <section class="panel"><div class="ph"><h2>Runtime Processes</h2></div><pre id="processes" class="terminal"></pre></section>
    </div>
    <section class="panel"><div class="ph"><h2>Connection &amp; Refresh</h2></div><div id="connGrid" class="stats"></div></section>
  </section>

  <div class="foot">AegisQuant · Automated Quant Trading Intelligence · Binance Spot · Auto-refresh every 30 seconds · Private API protected<br>This dashboard is for monitoring and research purposes only. Trading decisions involve risk. <span id="rev" class="faint"></span></div>
</div>
<script id="aegis-data" type="application/json">__DATA__</script><script>
const s=JSON.parse(document.getElementById("aegis-data").textContent),$=id=>document.getElementById(id);
const num=n=>Number(n||0),esc=v=>String(v??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));
const usd=n=>"$"+num(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:Math.abs(num(n))<1?4:2});
const idr=n=>"Rp "+num(n).toLocaleString("id-ID",{maximumFractionDigits:0});
const pct=n=>num(n).toFixed(2)+"%",spct=n=>(num(n)>=0?"+":"")+num(n).toFixed(2)+"%",cls=v=>num(v)>=0?"pos":"neg";
const fnum=v=>num(v).toLocaleString("en-US",{maximumFractionDigits:num(v)<1?8:2});
const comp=v=>num(v).toLocaleString("en-US",{maximumFractionDigits:1,notation:"compact"});
function table(h,r,e){return r.length?`<table><thead><tr>${h.map(x=>`<th class="${/^(num|#)/.test(x)?'num':''}">${x.replace(/^num /,'')}</th>`).join("")}</tr></thead><tbody>${r.join("")}</tbody></table>`:`<div class="empty">${e}</div>`}
function sigBadge(m){return `<span class="sig ${esc(m.signal_class||"sig-neutral")}">${esc(m.signal_arrow||"—")} ${esc(m.signal||"NEUTRAL")}</span>`}
function kpi(o){return `<div class="kpi" style="--accent:${o.accent||'var(--cyan)'}"><div class="kl">${o.label}${o.tip?`<span class="tip" data-tip="${esc(o.tip)}">i</span>`:""}</div><div class="kv mono">${o.value}</div><div class="kd">${o.desc||""}</div>${o.badge?`<span class="kt b ${o.badge.cls}">${o.badge.text}</span>`:(o.delta!=null?`<span class="kt ${cls(o.delta)}">${spct(o.delta)}</span>`:"")}</div>`}
function stat(l,v,c){return `<div class="stat"><div class="sl">${l}</div><div class="sv ${c||''}">${v}</div></div>`}

function renderTop(){
  const stale=s.stale,age=s.heartbeat_age_sec;
  if(stale){$("staleBar").style.display="flex";$("staleMsg").textContent=`Engine heartbeat is stale (last beat ${age==null?"unknown":Math.round(age)+"s ago"}). Data below may not reflect live state.`;}
  $("refreshTxt").innerHTML=`updated ${esc((s.updated_utc||"").replace(" UTC",""))} · <span id="cd">30</span>s`;
  $("rev").textContent="build "+esc(s.git_rev||"");
  const sf=s.safety||[];
  $("safetyStrip").innerHTML=sf.map(x=>`<span class="b ${x.ok?'b-ok':'b-bad'}"><span class="dot"></span>${esc(x.label)}: ${esc(x.ok?x.on:x.off)}</span>`).join("");
}
function renderKpis(){
  const t=s.trade_stats||{},dx=s.execution_diagnostics||{},rk=s.risk||{};
  const eq=(s.equity_curve||[]),lastEq=eq.length?num(eq[eq.length-1].pnl):num(t.pnl);
  const today=(s.daily_pnl||[]).slice(-1)[0],dToday=today?num(today.pnl):0;
  const best=dx.best_confidence||0,req=dx.required_confidence||74,gap=dx.confidence_gap!=null?dx.confidence_gap:(req-best);
  const engineOk=s.engine_ok&&s.watchdog_ok&&!s.stale;
  const readyBadge=best>=req?{cls:"b-ok",text:"READY"}:{cls:"b-warn",text:"ARMED"};
  $("kpis").innerHTML=[
    kpi({label:"Portfolio Value",value:usd(s.balance_usdt),desc:idr(s.balance_idr),accent:"var(--cyan)",badge:{cls:engineOk?"b-ok":"b-warn",text:engineOk?"LIVE":"CHECK"}}),
    kpi({label:"Daily PnL",value:usd(dToday),desc:"realized today",accent:dToday>=0?"var(--green)":"var(--red)",delta:dToday}),
    kpi({label:"Strategy Equity",value:usd(lastEq),desc:(t.count||0)+" closed trades",accent:"var(--purple)",delta:lastEq}),
    kpi({label:"Open Positions",value:(s.positions||0),desc:"Binance Spot",accent:"var(--blue)",badge:{cls:(s.positions||0)>0?"b-info":"b-mut",text:(s.positions||0)>0?"ACTIVE":"FLAT"}}),
    kpi({label:"Signal Readiness",value:best+"%",desc:`gate ${req}% · gap ${gap} pts`,accent:"var(--amber)",badge:readyBadge,tip:"Best directional confidence across symbols vs the entry gate. No order is sent until it clears the gate."}),
    kpi({label:"Execution Health",value:engineOk?"Online":"Degraded",desc:`${rk.failed_signals||0} failed · cycle #${s.heartbeat_cycle??"-"}`,accent:engineOk?"var(--green)":"var(--red)",badge:{cls:engineOk?"b-ok":"b-bad",text:engineOk?"OK":"WARN"}}),
  ].join("");
}
function recHtml(r){return `<div class="recItem ${esc(r.level)}"><strong>${esc(r.title)}</strong><span>${esc(r.detail)}</span></div>`}
function readinessHtml(dx){return table(["Asset","num Confidence","num TA support","num Gap","State"],(dx.latest_signals||[]).map(r=>{const ready=num(r.gap)<=0;return `<tr><td><b>${esc(r.symbol)}</b></td><td class="num">${r.confidence}%</td><td class="num">${r.support}/${r.support_total}</td><td class="num">${r.gap} pts</td><td><span class="b ${ready?'b-ok':'b-warn'}">${esc(r.state)}</span></td></tr>`}),"No signal readiness data yet — the engine is scanning.")}
function renderOverview(){
  const dx=s.execution_diagnostics||{},rk=s.risk||{};
  $("recommendations").innerHTML=(s.recommendations||[]).map(recHtml).join("")||"<div class='empty'>No recommendations.</div>";
  $("recommendations2").innerHTML=$("recommendations").innerHTML;
  const blocks=dx.blockers||[],mb=Math.max(1,...blocks.map(b=>b.count));
  $("executionVerdict").innerHTML=dx.best_symbol?`<div class="verdict wait"><h3>Waiting for a qualified setup</h3><p>${esc(dx.best_symbol)} leads at ${dx.best_confidence||0}%. Entry requires ${dx.required_confidence||"—"}% (a ${dx.confidence_gap||0}-pt gap). No order has reached the exchange.</p></div>`:"<div class='empty'>Awaiting complete scan diagnostics.</div>";
  $("executionDiagnosis").innerHTML=`<div class="funnel">${blocks.slice(0,5).map(b=>`<div class="funnelRow"><span>${esc(b.reason)}</span><div class="funnelTrack"><div class="funnelFill" style="width:${b.count/mb*100}%"></div></div><b class="mono">${b.count}</b></div>`).join("")||"<div class='empty'>No blockers recorded.</div>"}</div>`;
  $("readinessTable").innerHTML=readinessHtml(dx);$("readinessTable2").innerHTML=readinessHtml(dx);
  const w=rk.wins||0,l=rk.losses||0,wr=rk.win_rate;
  $("winloss").innerHTML=[stat("Win Rate",wr!=null?wr+"%":"—",wr>=50?"pos":(wr!=null?"neg":"")),stat("Wins",w,"pos"),stat("Losses",l,"neg"),stat("Avg Win",usd(rk.avg_win),"pos"),stat("Avg Loss",usd(rk.avg_loss),"neg"),stat("Closed PnL",usd(rk.closed_pnl),cls(rk.closed_pnl))].join("");
}
function tileMover(m,i,up){const ch=num(m.change),a=Math.min(Math.abs(ch)/15,.85),col=up?`rgba(39,217,140,${.05+a*.16})`:`rgba(255,93,110,${.05+a*.16})`;return `<div class="tile" style="background:linear-gradient(180deg,${col},var(--panel2))"><span class="rk">#${i+1}</span><b>${esc(m.symbol)}</b><div class="bp mono ${cls(ch)}">${spct(ch)}</div><div class="bx mono">${fnum(m.price)} · vol $${comp(m.volume)}</div><div style="margin-top:8px">${sigBadge(m)}</div></div>`}
function renderMarkets(){
  $("gainers").innerHTML=(s.gainers||[]).map((m,i)=>tileMover(m,i,true)).join("")||"<div class='empty'>Live gainers unavailable.</div>";
  $("losers").innerHTML=(s.losers||[]).map((m,i)=>tileMover(m,i,false)).join("")||"<div class='empty'>Live losers unavailable.</div>";
  $("heatmap").innerHTML=(s.market||[]).map(m=>{const ch=num(m.change),a=Math.min(Math.abs(ch)/8,.85),bg=ch>=0?`rgba(39,217,140,${.08+a*.4})`:`rgba(255,93,110,${.08+a*.4})`;return `<div class="heatCell" style="background:linear-gradient(180deg,${bg},var(--panel2))"><b>${esc(m.symbol)}</b><div class="hp mono">${fnum(m.price)}</div><strong class="mono ${cls(ch)}">${spct(ch)}</strong>${sigBadge(m)}</div>`}).join("")||"<div class='empty'>Live market data unavailable.</div>";
  $("marketTable").innerHTML=table(["Asset","num Last","num 24h","Signal","num High","num Low","num Quote vol"],(s.market||[]).map(m=>`<tr><td><b>${esc(m.symbol)}</b></td><td class="num">${fnum(m.price)}</td><td class="num ${cls(m.change)}">${spct(m.change)}</td><td>${sigBadge(m)}</td><td class="num">${fnum(m.high)}</td><td class="num">${fnum(m.low)}</td><td class="num">$${comp(m.volume)}</td></tr>`),"Live market data unavailable.");
}
function renderSignals(){
  $("scanTable").innerHTML=table(["Time","Pair","Signal","num Conf","Reason"],(s.scan_rows||[]).slice().reverse().map(r=>{const sg=String(r[2]||""),sc=sg==="BUY"||sg==="SELL"?"b-ok":(sg==="FAILED"?"b-bad":"b-mut");return `<tr><td class="mono">${esc(r[0])}</td><td><b>${esc(r[1])}</b></td><td><span class="b ${sc}">${esc(sg)}</span></td><td class="num">${esc(r[3])}%</td><td class="faint">${esc(r[4])}</td></tr>`}),"No scan rows yet.");
}
function renderModels(){
  $("modelGrid").innerHTML=(s.model_health||[]).map(m=>{if(m.status!=="ok")return `<div class="tile"><b>${esc(m.symbol)}</b><div class="bx">model not admitted</div></div>`;const fresh=(m.age_days!=null&&m.age_days<2),acc=m.accuracy,accCls=acc>=55?"pos":(acc<45?"neg":"");return `<div class="tile"><span class="rk">${fresh?"🟢":""} ${m.age_days!=null?m.age_days+"d":""}</span><b>${esc(m.symbol)}</b><div class="bp mono ${accCls}">${acc!=null?acc+"%":"—"}</div><div class="bx mono">acc · WF ${m.wf_mean!=null?m.wf_mean+"%":"—"}</div><div class="bx mono" style="margin-top:6px">Brier ${m.brier??"—"} · ECE ${m.ece??"—"}</div><div style="margin-top:8px;display:flex;gap:5px;flex-wrap:wrap">${m.wf_consistent?'<span class="sig sig-buy">consistent</span>':'<span class="sig sig-neutral">unstable</span>'}${m.calibrated?'<span class="sig sig-buy">calibrated</span>':'<span class="sig sig-sell">uncalibrated</span>'}</div></div>`}).join("")||"<div class='empty'>No model metadata found.</div>";
  const sh=s.shadow||{};
  if(!sh.rows||!sh.rows.length){$("shadowVerdict").innerHTML="<div class='empty'>No shadow samples yet — the evaluator is collecting out-of-sample paper outcomes.</div>";$("shadowTable").innerHTML="";return}
  const ready=sh.recommendation_ready;
  $("shadowVerdict").innerHTML=`<div class="verdict ${ready?'go':'wait'}"><h3>${ready?'✅ Ready':'⏳ Watch only — not ready to go live'}</h3><p>${ready?`Recommended threshold <b>${sh.recommended_threshold}</b> shows positive expectancy.`:`No confidence threshold yet shows positive expectancy over the minimum sample size.`} ${sh.completed_samples||0} completed · ${sh.pending_samples||0} pending · horizon ${sh.horizon_bars||"?"} bars.</p></div>`;
  $("shadowTable").innerHTML=table(["num Threshold","num Samples","num Win rate","num Expectancy","num Profit factor"],(sh.rows||[]).map(r=>`<tr><td class="num"><b>${r.threshold}</b></td><td class="num">${r.samples}</td><td class="num">${r.win_rate}%</td><td class="num ${r.expectancy>0?'pos':'neg'}">${r.expectancy}%</td><td class="num ${r.profit_factor>=1?'pos':'neg'}">${r.profit_factor??"—"}</td></tr>`),"No populated thresholds.");
}
function renderTrades(){
  const rk=s.risk||{};
  $("execHealth").innerHTML=[stat("Closed PnL",usd(rk.closed_pnl),cls(rk.closed_pnl)),stat("Total Trades",rk.trades_count||0),stat("Win Rate",rk.win_rate!=null?rk.win_rate+"%":"—",rk.win_rate>=50?"pos":(rk.win_rate!=null?"neg":"")),stat("Failed Signals",rk.failed_signals||0,(rk.failed_signals>0?"neg":"")),stat("Rejected Orders",rk.rejected_orders||0,(rk.rejected_orders>0?"neg":""))].join("");
  $("tradeTable").innerHTML=table(["Time","Symbol","Side","num PnL","num Conf"],(s.trades||[]).slice(-60).reverse().map(r=>{const p=num(r.PnL||r.pnl);return `<tr><td class="mono">${esc(r.Timestamp||r.timestamp||"")}</td><td><b>${esc(r.Symbol||r.symbol||"")}</b></td><td>${esc(r.Side||r.side||r.Result||"")}</td><td class="num ${cls(p)}">${p.toFixed(6)}</td><td class="num">${esc(r.Confidence||r.confidence||"")}</td></tr>`}),"No trades logged yet — the engine has not executed a live trade under current gates.");
}
function renderRisk(){
  const rk=s.risk||{};
  $("riskStats").innerHTML=[
    stat("Max Drawdown",usd(rk.max_drawdown_usd),"neg"),
    stat("Drawdown Halt","-"+(rk.drawdown_halt_pct||20)+"%"),
    stat("Open Positions",rk.open_positions||0),
    stat("Account Balance",usd(rk.balance_usdt)),
    stat("Failed Signals",rk.failed_signals||0,(rk.failed_signals>0?"neg":"")),
    stat("Rejected Orders",rk.rejected_orders||0,(rk.rejected_orders>0?"neg":"")),
    stat("Avg Win",usd(rk.avg_win),"pos"),
    stat("Avg Loss",usd(rk.avg_loss),"neg"),
  ].join("");
  $("safetyGrid").innerHTML=(s.safety||[]).map(x=>`<div class="stat"><div class="sl">${esc(x.label)}</div><div class="sv"><span class="b ${x.ok?'b-ok':'b-bad'}"><span class="dot"></span>${esc(x.ok?x.on:x.off)}</span></div></div>`).join("");
  $("riskLast").textContent=rk.last_rejected||"No rejected or blocked orders recorded.";
}
function renderSystem(){
  const a=[...(s.model_alerts||[]),...(s.errors||[])].slice(-14);
  $("alerts").innerHTML=a.length?table(["Recent alerts"],a.map(e=>`<tr><td class="terminal">${esc(String(e).slice(-340))}</td></tr>`),""):"<div class='empty'>No recent model or runtime alerts. System nominal.</div>";
  $("processes").textContent=(s.process||[]).join("\\n")||"No engine processes found.";
  $("connGrid").innerHTML=[
    stat("Exchange",(s.market&&s.market.length)?'<span class="b b-ok"><span class="dot"></span>Connected</span>':'<span class="b b-bad">Degraded</span>'),
    stat("Engine",s.engine_ok?'<span class="b b-ok"><span class="dot"></span>Running</span>':'<span class="b b-bad">Stopped</span>'),
    stat("Watchdog",s.watchdog_ok?'<span class="b b-ok"><span class="dot"></span>Running</span>':'<span class="b b-bad">Stopped</span>'),
    stat("Heartbeat",(s.heartbeat_age_sec!=null?Math.round(s.heartbeat_age_sec)+"s":"—"),s.stale?"neg":"pos"),
    stat("Refresh","30s auto"),
  ].join("");
}
function makeCharts(){
  const eqRaw=(s.equity_curve||[]).map(r=>num(r.pnl)),scan=(s.scan_rows||[]).map(r=>num(r[3])),dailyRaw=(s.daily_pnl||[]).slice(-45),symRaw=(s.symbol_stats||[]).slice(0,10),mk=(s.market||[]);
  const equity=eqRaw.length?eqRaw:scan,daily=dailyRaw.length?dailyRaw:mk.map(m=>({date:m.symbol,pnl:m.change})),sym=symRaw.length?symRaw:mk.map(m=>({symbol:m.symbol,pnl:m.change,win_rate:Math.abs(m.change)}));
  $("equityTitle").textContent=eqRaw.length?"Strategy Equity":"Live AI Confidence";$("dailyTitle").textContent=dailyRaw.length?"Daily PnL":"24h Market Pulse";
  if(!window.Chart)return;Chart.defaults.color="#7e8da8";Chart.defaults.borderColor="rgba(38,52,79,.6)";Chart.defaults.font.family="Inter";
  const grd=$("equityChart").getContext("2d").createLinearGradient(0,0,0,248);grd.addColorStop(0,"rgba(67,200,240,.28)");grd.addColorStop(1,"rgba(67,200,240,0)");
  new Chart($("equityChart"),{type:"line",data:{labels:equity.map((_,i)=>i+1),datasets:[{label:eqRaw.length?"Equity ($)":"Confidence (%)",data:equity,borderColor:"#43c8f0",backgroundColor:grd,fill:true,tension:.35,pointRadius:0,borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{display:false},y:{grid:{color:"rgba(38,52,79,.5)"},ticks:{callback:v=>eqRaw.length?"$"+v:v+"%"}}}}});
  new Chart($("dailyChart"),{type:"bar",data:{labels:daily.map(r=>r.date),datasets:[{data:daily.map(r=>r.pnl),backgroundColor:daily.map(r=>num(r.pnl)>=0?"#27d98c":"#ff5d6e"),borderRadius:3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{display:false},y:{grid:{color:"rgba(38,52,79,.5)"},ticks:{callback:v=>dailyRaw.length?"$"+v:v+"%"}}}}});
  new Chart($("symbolChart"),{type:"bar",data:{labels:sym.map(r=>r.symbol),datasets:[{label:symRaw.length?"PnL":"24h %",data:sym.map(r=>r.pnl),backgroundColor:sym.map(r=>num(r.pnl)>=0?"#27d98c":"#ff5d6e"),borderRadius:4}]},options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{color:"rgba(38,52,79,.5)"},ticks:{callback:v=>num(v).toFixed(1)}},y:{grid:{display:false}}}}});
}
function setupTabs(){const titles={overview:"Overview",markets:"Market Movers",signals:"Signal Engine",models:"Model Calibration",trades:"Execution & Trades",risk:"Risk & Capital",system:"System Runtime"},saved=localStorage.getItem("aegis-view")||"overview";
  document.querySelectorAll(".tab").forEach(btn=>btn.addEventListener("click",()=>{document.querySelectorAll(".tab,.view").forEach(el=>el.classList.remove("active"));btn.classList.add("active");const v=$(btn.dataset.view);if(v)v.classList.add("active");document.title="AegisQuant — "+(titles[btn.dataset.view]||"");localStorage.setItem("aegis-view",btn.dataset.view);window.dispatchEvent(new Event("resize"))}));
  const btn=document.querySelector(`.tab[data-view="${saved}"]`)||document.querySelector('.tab[data-view="overview"]');btn.click();}
function countdown(){let n=30;const tick=()=>{const el=$("cd");if(el)el.textContent=n;if(n<=0){location.reload();return}n--;setTimeout(tick,1000)};tick();}
document.addEventListener("DOMContentLoaded",()=>{try{renderTop();renderKpis();renderOverview();renderMarkets();renderSignals();renderModels();renderTrades();renderRisk();renderSystem();makeCharts();setupTabs();countdown();}catch(e){document.body.insertAdjacentHTML("afterbegin",`<div style="position:sticky;top:0;z-index:99;background:#ff5d6e;color:#0a0e16;padding:10px;font-family:monospace;font-weight:700">Dashboard render error: ${String(e.message||e)}</div>`);setTimeout(()=>location.reload(),30000);}});
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

