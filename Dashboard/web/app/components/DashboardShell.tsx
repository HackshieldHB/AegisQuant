"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  CheckCircle2,
  Clock3,
  Command,
  Cpu,
  LineChart,
  RefreshCw,
  Shield,
  Sparkles,
  Wallet
} from "lucide-react";
import { DailyPnlChart, EquityChart, ScanConfidenceChart, SymbolPnlChart } from "./Charts";
import type { DashboardSnapshot } from "../types";

const tabs = ["Overview", "Markets", "Performance", "Trades", "Risk", "System"] as const;
type Tab = (typeof tabs)[number];

const fmtUsd = (value: number) => `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
const fmtIdr = (value: number) => `Rp ${Math.round(value).toLocaleString("id-ID")}`;

function Card({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail: string; tone?: string }) {
  return (
    <div className={`metric ${tone}`}>
      <div className="metricLabel">{label}</div>
      <div className="metricValue">{value}</div>
      <div className="metricDetail">{detail}</div>
    </div>
  );
}

function Panel({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="panel">
      <div className="panelTitle">{icon}<span>{title}</span></div>
      {children}
    </section>
  );
}

export default function DashboardShell() {
  const [data, setData] = useState<DashboardSnapshot | null>(null);
  const [error, setError] = useState<string>("");
  const [active, setActive] = useState<Tab>("Overview");
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const response = await fetch("/api/dashboard", { cache: "no-store" });
      if (!response.ok) throw new Error(`API ${response.status}`);
      setData(await response.json());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dashboard API unavailable");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    const refreshMs = Number(process.env.NEXT_PUBLIC_REFRESH_MS || 15000);
    const id = window.setInterval(load, refreshMs);
    return () => window.clearInterval(id);
  }, []);

  const winRate = useMemo(() => {
    if (!data) return 0;
    const total = data.trade_stats.wins + data.trade_stats.losses;
    return total ? (data.trade_stats.wins / total) * 100 : 0;
  }, [data]);

  if (loading && !data) {
    return <main className="shell"><div className="loading">Loading AegisQuant command center...</div></main>;
  }

  return (
    <main className="shell">
      <header className="hero">
        <div>
          <div className="eyebrow"><Shield size={16} /> AegisQuant Production</div>
          <h1>Command Center</h1>
          <p>Live trading telemetry, model health, execution state, and my recommendation layer in one place.</p>
        </div>
        <div className={`status ${data?.health === "OK" ? "good" : "watch"}`}>
          {data?.health === "OK" ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}
          {data?.health ?? "UNKNOWN"}
        </div>
      </header>

      {error && <div className="banner danger"><AlertTriangle size={18} /> {error}</div>}

      <nav className="tabs">
        {tabs.map((tab) => <button key={tab} className={active === tab ? "active" : ""} onClick={() => setActive(tab)}>{tab}</button>)}
        <button className="refresh" onClick={load}><RefreshCw size={15} /> Refresh</button>
      </nav>

      {data && (
        <>
          <div className="metricGrid">
            <Card label="Balance" value={fmtUsd(data.balance_usdt)} detail={fmtIdr(data.balance_idr)} tone="accent" />
            <Card label="Growth Target" value={`${data.growth_pct.toFixed(2)}%`} detail="Rp 300K -> Rp 10M" />
            <Card label="Cycle" value={`#${data.heartbeat_cycle ?? "-"}`} detail={`${data.heartbeat_age_sec ?? "-"}s heartbeat`} />
            <Card label="Last Cycle" value={`${data.cycles.last_candidates}/${data.cycles.last_executed}`} detail="candidates / executed" />
            <Card label="Trades" value={`${data.trade_stats.count}`} detail={`${winRate.toFixed(1)}% win rate`} />
            <Card label="Total PnL" value={fmtUsd(data.trade_stats.pnl)} detail="closed trade log" tone={data.trade_stats.pnl >= 0 ? "good" : "bad"} />
          </div>

          {active === "Overview" && <Overview data={data} />}
          {active === "Markets" && <Markets data={data} />}
          {active === "Performance" && <Performance data={data} />}
          {active === "Trades" && <Trades data={data} />}
          {active === "Risk" && <Risk data={data} />}
          {active === "System" && <System data={data} />}
        </>
      )}
    </main>
  );
}

function Overview({ data }: { data: DashboardSnapshot }) {
  return (
    <div className="layout">
      <Panel title="My Recommendation" icon={<Sparkles size={18} />}>
        <div className="recommendations">
          {data.recommendations.map((item) => (
            <div key={item.title} className={`recommendation ${item.level}`}>
              <strong>{item.title}</strong>
              <span>{item.detail}</span>
            </div>
          ))}
        </div>
      </Panel>
      <Panel title="Equity Curve" icon={<LineChart size={18} />}><EquityChart data={data.equity_curve} /></Panel>
      <Panel title="Latest Confidence" icon={<Brain size={18} />}><ScanConfidenceChart rows={data.scan_rows} /></Panel>
    </div>
  );
}

function Markets({ data }: { data: DashboardSnapshot }) {
  return (
    <Panel title="Live Symbol Scanner" icon={<Activity size={18} />}>
      <table><thead><tr><th>Time</th><th>Symbol</th><th>Signal</th><th>Confidence</th><th>Reason</th></tr></thead>
        <tbody>{data.scan_rows.map((row, index) => <tr key={`${row[0]}-${row[1]}-${index}`}><td>{row[0]}</td><td>{row[1]}</td><td><span className={`pill ${row[2].toLowerCase()}`}>{row[2]}</span></td><td>{row[3]}%</td><td>{row[4]}</td></tr>)}</tbody>
      </table>
    </Panel>
  );
}

function Performance({ data }: { data: DashboardSnapshot }) {
  return (
    <div className="layout two">
      <Panel title="Symbol PnL" icon={<BarChart3 size={18} />}><SymbolPnlChart data={data.symbol_stats} /></Panel>
      <Panel title="Daily PnL" icon={<Clock3 size={18} />}><DailyPnlChart data={data.daily_pnl} /></Panel>
    </div>
  );
}

function Trades({ data }: { data: DashboardSnapshot }) {
  return (
    <Panel title="Trade History" icon={<Wallet size={18} />}>
      <table><thead><tr><th>Time</th><th>Symbol</th><th>Side/Result</th><th>PnL</th><th>Confidence</th></tr></thead>
        <tbody>{data.trades.slice(-20).reverse().map((row, index) => <tr key={index}><td>{row.Timestamp || row.timestamp || "-"}</td><td>{row.Symbol || row.symbol || "-"}</td><td>{row.Side || row.side || row.Result || "-"}</td><td>{row.PnL || row.pnl || "-"}</td><td>{row.Confidence || row.confidence || "-"}</td></tr>)}</tbody>
      </table>
    </Panel>
  );
}

function Risk({ data }: { data: DashboardSnapshot }) {
  return (
    <div className="layout two">
      <Panel title="Model Health" icon={<Brain size={18} />}>
        <ul className="logList">{(data.model_alerts.length ? data.model_alerts : ["No recent model-health alerts."]).map((item) => <li key={item}>{item}</li>)}</ul>
      </Panel>
      <Panel title="Runtime Errors" icon={<AlertTriangle size={18} />}>
        <ul className="logList">{(data.errors.length ? data.errors : ["No recent runtime errors."]).map((item) => <li key={item}>{item}</li>)}</ul>
      </Panel>
    </div>
  );
}

function System({ data }: { data: DashboardSnapshot }) {
  return (
    <div className="layout two">
      <Panel title="Runtime Processes" icon={<Cpu size={18} />}><pre>{data.process.join("\n") || "No process data"}</pre></Panel>
      <Panel title="Deployment" icon={<Command size={18} />}>
        <div className="kv"><span>Revision</span><strong>{data.git_rev}</strong></div>
        <div className="kv"><span>Updated</span><strong>{data.updated_utc}</strong></div>
        <div className="kv"><span>Engine</span><strong>{data.engine_ok ? "Running" : "Down"}</strong></div>
        <div className="kv"><span>Watchdog</span><strong>{data.watchdog_ok ? "Running" : "Down"}</strong></div>
      </Panel>
    </div>
  );
}
