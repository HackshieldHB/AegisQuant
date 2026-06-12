export type ScanRow = [string, string, string, string, string];

export type DashboardSnapshot = {
  health: string;
  engine_ok: boolean;
  watchdog_ok: boolean;
  heartbeat_age_sec: number | null;
  heartbeat_cycle: number | null;
  balance_usdt: number;
  balance_idr: number;
  growth_pct: number;
  positions: number;
  git_rev: string;
  cycles: { cycle: number | null; last_candidates: number; last_executed: number };
  scan_rows: ScanRow[];
  model_alerts: string[];
  errors: string[];
  process: string[];
  trades: Record<string, string>[];
  trade_stats: { count: number; pnl: number; wins: number; losses: number };
  daily_pnl: { date: string; pnl: number }[];
  symbol_stats: { symbol: string; trades: number; wins: number; pnl: number; win_rate: number }[];
  equity_curve: { index: number; timestamp: string; pnl: number }[];
  recommendations: { level: "success" | "info" | "warning" | "critical"; title: string; detail: string }[];
  updated_utc: string;
};
