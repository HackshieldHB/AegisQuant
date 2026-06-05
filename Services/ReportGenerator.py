"""
ReportGenerator — Automated Weekly PDF Performance Report
==========================================================
Generates a one-page PDF summary of the week's trading:
  • Account balance (USDT + IDR)
  • Win rate, profit factor, Sharpe, expectancy
  • Best/worst trade
  • PnL by symbol breakdown table
  • Growth target progress

Designed to be called by a weekly scheduler inside AsyncEngine.
Output saved to  logs/reports/weekly_YYYYMMDD.pdf
Can also send via Telegram as a file attachment.

Requires: fpdf2 (pip install fpdf2)
"""

import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import pandas as pd

try:
    from fpdf import FPDF, XPos, YPos
    _FPDF_AVAILABLE = True
except ImportError:
    _FPDF_AVAILABLE = False

try:
    from Core.Logger import AG_LOGGER as _logger
except ImportError:
    import logging
    _logger = logging.getLogger("ReportGenerator")

try:
    from AegisQuantConfig import CONFIG as _CFG
    _IDR_RATE = _CFG.get("GROWTH_TARGET", {}).get("IDR_RATE", 16_000)
    _START_IDR = _CFG.get("GROWTH_TARGET", {}).get("STARTING_CAPITAL_IDR", 300_000)
    _TARGET_IDR = _CFG.get("GROWTH_TARGET", {}).get("TARGET_CAPITAL_IDR", 10_000_000)
except Exception:
    _IDR_RATE = 16_000
    _START_IDR = 300_000
    _TARGET_IDR = 10_000_000


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette (RGB tuples)
# ─────────────────────────────────────────────────────────────────────────────
C_HEADER  = (13,  27, 42)    # navy background
C_ACCENT  = (58, 123, 213)   # blue accent
C_TEAL    = (0,  200, 170)   # positive green/teal
C_RED     = (220, 50,  50)   # negative red
C_TEXT    = (30,  30,  30)   # body text
C_SUBTEXT = (120, 120, 120)  # secondary text
C_ROW_ALT = (245, 248, 252)  # alternating row bg


class ReportGenerator:
    """Generates weekly PDF reports from trades.csv."""

    def __init__(self, log_dir: str) -> None:
        self.log_dir = log_dir
        self.report_dir = os.path.join(log_dir, "reports")
        os.makedirs(self.report_dir, exist_ok=True)
        self.trades_file = os.path.join(log_dir, "trades.csv")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate_weekly_report(self, balance: float) -> Optional[str]:
        """
        Build PDF for the past 7 days of trading.
        Returns absolute path to generated PDF, or None on failure.
        """
        if not _FPDF_AVAILABLE:
            _logger.warning("ReportGenerator: fpdf2 not installed — pip install fpdf2")
            return None

        try:
            df_all, df_week, stats = self._load_data()
            path = self._build_pdf(balance, df_all, df_week, stats)
            _logger.info("Weekly report saved: %s", path)
            return path
        except Exception as exc:
            _logger.error("ReportGenerator failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------
    def _load_data(self) -> Tuple[pd.DataFrame, pd.DataFrame, dict]:
        if not os.path.exists(self.trades_file):
            empty = pd.DataFrame()
            return empty, empty, self._empty_stats()

        df = pd.read_csv(self.trades_file)
        if "Timestamp" in df.columns:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True, errors="coerce")
        for col in ("PnL", "Confidence", "Edge_Score"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        if "Timestamp" in df.columns:
            df_week = df[df["Timestamp"] >= cutoff].copy()
        else:
            df_week = df.copy()

        # Closed trades
        closed_mask = (
            df_week["Result"].str.upper().isin(["CLOSE", "CLOSED", "SELL"])
            if "Result" in df_week.columns else
            pd.Series(True, index=df_week.index)
        )
        closed = df_week[closed_mask].copy()
        stats = self._calc_stats(closed)
        return df, df_week, stats

    def _calc_stats(self, ct: pd.DataFrame) -> dict:
        if ct.empty or "PnL" not in ct.columns:
            return self._empty_stats()
        pnl = ct["PnL"].dropna()
        if len(pnl) == 0:
            return self._empty_stats()
        wins   = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        n = len(pnl)
        return dict(
            n=n,
            win_rate=len(wins) / n * 100 if n > 0 else 0,
            total_pnl=float(pnl.sum()),
            profit_factor=float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf"),
            expectancy=float(pnl.mean()),
            best=float(pnl.max()),
            worst=float(pnl.min()),
        )

    @staticmethod
    def _empty_stats() -> dict:
        return dict(n=0, win_rate=0.0, total_pnl=0.0, profit_factor=0.0,
                    expectancy=0.0, best=0.0, worst=0.0)

    # ------------------------------------------------------------------
    # PDF builder
    # ------------------------------------------------------------------
    def _build_pdf(
        self,
        balance: float,
        df_all: pd.DataFrame,
        df_week: pd.DataFrame,
        stats: dict,
    ) -> str:
        pdf = FPDF()
        pdf.set_margins(left=14, top=12, right=14)
        pdf.add_page()

        # ── Header banner ────────────────────────────────────────────
        pdf.set_fill_color(*C_HEADER)
        pdf.rect(0, 0, 210, 34, style="F")
        pdf.set_y(7)
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(100, 255, 218)
        pdf.cell(0, 10, "AegisQuant  |  Weekly Performance Report", align="C")
        pdf.ln(7)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(160, 180, 200)
        week_end = datetime.now(timezone.utc)
        week_start = week_end - timedelta(days=7)
        pdf.cell(
            0, 5,
            f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}   "
            f"Generated {week_end.strftime('%Y-%m-%d %H:%M UTC')}",
            align="C",
        )
        pdf.set_y(38)

        # ── Account summary ──────────────────────────────────────────
        self._section_title(pdf, "Account Summary")
        current_idr = balance * _IDR_RATE
        progress_pct = min(100, max(0, (current_idr - _START_IDR) / (_TARGET_IDR - _START_IDR) * 100))

        data_pairs = [
            ("Balance (USDT)", f"${balance:,.4f}"),
            ("Balance (IDR)",  f"Rp {current_idr:,.0f}"),
            ("Growth Target",  f"Rp {_START_IDR:,.0f}  →  Rp {_TARGET_IDR:,.0f}"),
            ("Progress",       f"{progress_pct:.1f}%"),
        ]
        self._key_value_grid(pdf, data_pairs, cols=2)
        pdf.ln(3)

        # ── Weekly performance ────────────────────────────────────────
        self._section_title(pdf, "7-Day Performance")
        pf_str = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] != float("inf") else "∞"
        perf_pairs = [
            ("Total Closed Trades", str(stats["n"])),
            ("Win Rate",            f"{stats['win_rate']:.1f}%"),
            ("Total PnL",           f"${stats['total_pnl']:+.4f}"),
            ("Profit Factor",       pf_str),
            ("Expectancy / Trade",  f"${stats['expectancy']:+.4f}"),
            ("Best Trade",          f"${stats['best']:+.4f}"),
            ("Worst Trade",         f"${stats['worst']:+.4f}"),
        ]
        self._key_value_grid(pdf, perf_pairs, cols=2)
        pdf.ln(3)

        # ── Symbol breakdown ──────────────────────────────────────────
        closed_week = pd.DataFrame()
        if not df_week.empty and "Result" in df_week.columns and "PnL" in df_week.columns:
            closed_mask = df_week["Result"].str.upper().isin(["CLOSE", "CLOSED", "SELL"])
            closed_week = df_week[closed_mask].copy()

        if not closed_week.empty and "Symbol" in closed_week.columns:
            self._section_title(pdf, "Performance by Symbol")
            grp = (
                closed_week.groupby("Symbol")["PnL"]
                .agg(trades="count", total="sum",
                     wr=lambda x: (x > 0).mean() * 100)
                .reset_index()
                .sort_values("total", ascending=False)
            )
            headers = ["Symbol", "Trades", "Win Rate", "Total PnL"]
            col_w   = [60, 30, 40, 50]
            self._table(pdf, headers, col_w, grp, lambda row: [
                row["Symbol"],
                str(int(row["trades"])),
                f"{row['wr']:.1f}%",
                f"${row['total']:+.4f}",
            ])
            pdf.ln(3)

        # ── All-time cumulative ───────────────────────────────────────
        self._section_title(pdf, "All-Time Performance")
        if not df_all.empty and "Result" in df_all.columns and "PnL" in df_all.columns:
            all_closed_mask = df_all["Result"].str.upper().isin(["CLOSE", "CLOSED", "SELL"])
            all_ct = df_all[all_closed_mask]
            all_stats = self._calc_stats(all_ct)
            at_pairs = [
                ("Total Trades (All Time)", str(all_stats["n"])),
                ("Total PnL (All Time)",    f"${all_stats['total_pnl']:+.4f}"),
                ("All-Time Win Rate",       f"{all_stats['win_rate']:.1f}%"),
                ("Profit Factor",           f"{all_stats['profit_factor']:.2f}" if all_stats["profit_factor"] != float("inf") else "∞"),
            ]
            self._key_value_grid(pdf, at_pairs, cols=2)
        pdf.ln(3)

        # ── Footer ────────────────────────────────────────────────────
        pdf.set_y(-20)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*C_SUBTEXT)
        pdf.cell(0, 5, "AegisQuant v3.1.0 — Institutional AI Trading System  |  This report is for informational purposes only.", align="C")

        fname = f"weekly_report_{week_end.strftime('%Y%m%d')}.pdf"
        fpath = os.path.join(self.report_dir, fname)
        pdf.output(fpath)
        return fpath

    # ------------------------------------------------------------------
    # PDF helpers
    # ------------------------------------------------------------------
    def _section_title(self, pdf: "FPDF", title: str) -> None:
        pdf.set_fill_color(*C_ACCENT)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, f"  {title.upper()}", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)
        pdf.set_text_color(*C_TEXT)

    def _key_value_grid(self, pdf: "FPDF", pairs: list, cols: int = 2) -> None:
        col_width = (pdf.w - pdf.l_margin - pdf.r_margin) / cols
        pdf.set_font("Helvetica", "", 9)
        for i, (label, value) in enumerate(pairs):
            pdf.set_fill_color(245, 248, 252)
            pdf.set_text_color(*C_SUBTEXT)
            pdf.set_font("Helvetica", "", 8)
            x_start = pdf.l_margin + (i % cols) * col_width
            pdf.set_xy(x_start, pdf.get_y())
            pdf.cell(col_width * 0.5, 5, label + ":", fill=True)
            pdf.set_text_color(*C_TEXT)
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(col_width * 0.5, 5, str(value), fill=True)
            if (i + 1) % cols == 0:
                pdf.ln(5)
        if len(pairs) % cols != 0:
            pdf.ln(5)

    def _table(self, pdf: "FPDF", headers: list, col_widths: list,
               df: pd.DataFrame, row_fn) -> None:
        # Header row
        pdf.set_fill_color(*C_ACCENT)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        for h, w in zip(headers, col_widths):
            pdf.cell(w, 7, h, border=0, fill=True)
        pdf.ln()
        # Data rows
        for idx, (_, row) in enumerate(df.iterrows()):
            pdf.set_fill_color(*C_ROW_ALT) if idx % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            pdf.set_text_color(*C_TEXT)
            pdf.set_font("Helvetica", "", 9)
            cells = row_fn(row)
            for cell, w in zip(cells, col_widths):
                pdf.cell(w, 6, str(cell), fill=True)
            pdf.ln()
        pdf.ln(1)
