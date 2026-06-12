"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";
import type { DashboardSnapshot } from "../types";

const grid = "rgba(136, 146, 176, 0.14)";
const text = "#8892b0";
const teal = "#64ffda";
const red = "#ff6b6b";
const blue = "#64b5f6";

export function EquityChart({ data }: { data: DashboardSnapshot["equity_curve"] }) {
  const rows = data.length ? data : [{ index: 0, pnl: 0, timestamp: "No trades" }];
  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
        <defs>
          <linearGradient id="equityFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={teal} stopOpacity={0.35} />
            <stop offset="100%" stopColor={teal} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={grid} vertical={false} />
        <XAxis dataKey="index" tick={{ fill: text, fontSize: 11 }} axisLine={false} tickLine={false} />
        <YAxis tick={{ fill: text, fontSize: 11 }} axisLine={false} tickLine={false} />
        <Tooltip contentStyle={{ background: "#101724", border: "1px solid #2d3548", borderRadius: 8 }} />
        <Area type="monotone" dataKey="pnl" stroke={teal} fill="url(#equityFill)" strokeWidth={2} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function SymbolPnlChart({ data }: { data: DashboardSnapshot["symbol_stats"] }) {
  const rows = data.length ? data : [{ symbol: "WAIT", pnl: 0, trades: 0, wins: 0, win_rate: 0 }];
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
        <CartesianGrid stroke={grid} vertical={false} />
        <XAxis dataKey="symbol" tick={{ fill: text, fontSize: 11 }} axisLine={false} tickLine={false} />
        <YAxis tick={{ fill: text, fontSize: 11 }} axisLine={false} tickLine={false} />
        <Tooltip contentStyle={{ background: "#101724", border: "1px solid #2d3548", borderRadius: 8 }} />
        <Bar dataKey="pnl" radius={[6, 6, 0, 0]}>
          {rows.map((row) => <Cell key={row.symbol} fill={row.pnl >= 0 ? teal : red} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function DailyPnlChart({ data }: { data: DashboardSnapshot["daily_pnl"] }) {
  const rows = data.length ? data : [{ date: "No trades", pnl: 0 }];
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
        <CartesianGrid stroke={grid} vertical={false} />
        <XAxis dataKey="date" tick={{ fill: text, fontSize: 10 }} axisLine={false} tickLine={false} />
        <YAxis tick={{ fill: text, fontSize: 11 }} axisLine={false} tickLine={false} />
        <Tooltip contentStyle={{ background: "#101724", border: "1px solid #2d3548", borderRadius: 8 }} />
        <Bar dataKey="pnl" radius={[5, 5, 0, 0]}>
          {rows.map((row) => <Cell key={row.date} fill={row.pnl >= 0 ? blue : red} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function ScanConfidenceChart({ rows }: { rows: DashboardSnapshot["scan_rows"] }) {
  const data = rows.map((row) => ({ symbol: row[1], confidence: Number(row[3]), signal: row[2] }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <ScatterChart margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
        <CartesianGrid stroke={grid} />
        <XAxis dataKey="symbol" type="category" tick={{ fill: text, fontSize: 11 }} axisLine={false} tickLine={false} />
        <YAxis dataKey="confidence" domain={[0, 100]} tick={{ fill: text, fontSize: 11 }} axisLine={false} tickLine={false} />
        <Tooltip contentStyle={{ background: "#101724", border: "1px solid #2d3548", borderRadius: 8 }} />
        <Scatter data={data} fill={teal} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}
