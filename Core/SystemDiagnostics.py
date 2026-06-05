"""
SystemDiagnostics — Comprehensive startup validation system.
============================================================

Runs before trading begins to validate:
- Configuration integrity
- Environment readiness
- Python dependencies
- Exchange connectivity
- API credentials
- Model files
- Telegram service
- Disk space
- System clock
- No duplicate running instances

Exits cleanly if any CRITICAL check fails.
"""

import os
import sys
import json
import time
import psutil
import socket
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

from AegisQuantConfig import CONFIG, assert_asset_enabled, validate_config, validate_config_log_level
from Core.Logger import AG_LOGGER


class SystemDiagnosticsException(Exception):
    """Raised when CRITICAL check fails."""
    pass


class SystemDiagnostics:
    def __init__(self) -> None:
        self.logger = AG_LOGGER
        self.checks_passed = 0
        self.checks_failed = 0
        self.checks_skipped = 0
        self.results: List[Dict[str, Any]] = []

    def _record(self, name: str, status: str, message: str, severity: str = "INFO") -> None:
        """Record a check result."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "check": name,
            "status": status,
            "severity": severity,
            "message": message,
        }
        self.results.append(entry)
        
        if status == "PASS":
            self.checks_passed += 1
            self.logger.info("✓ %s: %s", name, message)
        elif status == "FAIL":
            self.checks_failed += 1
            if severity == "CRITICAL":
                self.logger.critical("✗ CRITICAL: %s: %s", name, message)
            else:
                self.logger.warning("✗ %s: %s", name, message)
        elif status == "SKIP":
            self.checks_skipped += 1
            self.logger.info("⊘ SKIPPED: %s: %s", name, message)

    def run_all(self) -> bool:
        """
        Run complete diagnostic suite.
        Returns True if all CRITICAL checks pass.
        Returns False (and logs) if any CRITICAL check fails.
        """
        self.logger.info("=" * 70)
        self.logger.info("STARTING SYSTEM DIAGNOSTICS")
        self.logger.info("=" * 70)
        
        try:
            # ===== CONFIG CHECKS =====
            self._check_config_integrity()
            self._check_config_log_level()
            
            # ===== ENVIRONMENT CHECKS =====
            self._check_python_version()
            self._check_dependencies()
            self._check_disk_space()
            self._check_system_clock()
            self._check_duplicate_instances()
            
            # ===== EXCHANGE CHECKS =====
            self._check_binance_api() if CONFIG["PROJECT"]["CRYPTO_ENABLED"] else None
            self._check_forex_api() if CONFIG["PROJECT"]["FOREX_ENABLED"] else None
            self._check_stocks_api() if CONFIG["PROJECT"]["STOCKS_ENABLED"] else None
            
            # ===== MODEL CHECKS =====
            self._check_models()
            
            # ===== TELEGRAM CHECKS =====
            self._check_telegram()
            
            # ===== SUMMARY =====
            self._print_summary()
            
            # Fail if any CRITICAL check failed
            for result in self.results:
                if result["status"] == "FAIL" and result["severity"] == "CRITICAL":
                    return False
            
            return True
            
        except SystemDiagnosticsException as e:
            self.logger.critical("Diagnostics aborted: %s", e)
            self._print_summary()
            return False
        except Exception as e:
            self.logger.critical("Unexpected error in diagnostics: %s", e)
            self._print_summary()
            return False

    def _check_config_integrity(self) -> None:
        """Validate configuration schema."""
        check_name = "Config Integrity"
        try:
            validate_config()
            self._record(check_name, "PASS", "All config parameters valid", "CRITICAL")
        except ValueError as e:
            self._record(check_name, "FAIL", str(e), "CRITICAL")
            raise SystemDiagnosticsException(f"Config validation failed: {e}")

    def _check_config_log_level(self) -> None:
        """Validate logging configuration."""
        check_name = "Log Level Configuration"
        try:
            validate_config_log_level()
            level = CONFIG.get("LOGGING", {}).get("LEVEL", "INFO")
            self._record(check_name, "PASS", f"Log level: {level}", "INFO")
        except ValueError as e:
            self._record(check_name, "FAIL", str(e), "WARNING")

    def _check_python_version(self) -> None:
        """Verify Python >= 3.8."""
        check_name = "Python Version"
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info >= (3, 8):
            self._record(check_name, "PASS", f"Python {version}", "CRITICAL")
        else:
            self._record(check_name, "FAIL", f"Python {version} < 3.8", "CRITICAL")
            raise SystemDiagnosticsException(f"Python {version} is not supported")

    def _check_dependencies(self) -> None:
        """Verify critical Python packages are installed."""
        check_name = "Python Dependencies"
        required = [
            ("ccxt", "CCXT"),
            ("pandas", "Pandas"),
            ("numpy", "NumPy"),
            ("sklearn", "Scikit-Learn"),
            ("joblib", "Joblib"),
            ("ta", "TA-Lib"),
            ("requests", "Requests"),
            ("websocket", "WebSocket-Client"),
            ("streamlit", "Streamlit"),
            ("plotly", "Plotly"),
            ("psutil", "PSUtil"),
        ]
        
        missing = []
        for module, name in required:
            try:
                __import__(module)
            except ImportError:
                missing.append(name)
        
        if not missing:
            self._record(check_name, "PASS", f"All {len(required)} dependencies available", "CRITICAL")
        else:
            msg = f"Missing: {', '.join(missing)}"
            self._record(check_name, "FAIL", msg, "CRITICAL")
            raise SystemDiagnosticsException(msg)

    def _check_disk_space(self) -> None:
        """Verify sufficient disk space."""
        check_name = "Disk Space"
        try:
            # os.path.abspath(".") resolves to the working directory's drive root
            # on Windows ("C:\..." → works) AND "/" on Linux — unlike a bare "/"
            # which causes FileNotFoundError on Windows.
            usage = psutil.disk_usage(os.path.abspath("."))
            available_gb = usage.free / (1024 ** 3)
            required_gb = 5.0
            
            if available_gb >= required_gb:
                self._record(check_name, "PASS", f"{available_gb:.1f} GB available (need {required_gb} GB)", "CRITICAL")
            else:
                self._record(check_name, "FAIL", f"Only {available_gb:.1f} GB available (need {required_gb} GB)", "CRITICAL")
                raise SystemDiagnosticsException(f"Insufficient disk space: {available_gb:.1f} GB")
        except Exception as e:
            self._record(check_name, "FAIL", str(e), "WARNING")

    def _check_system_clock(self) -> None:
        """Verify system clock is synchronized."""
        check_name = "System Clock"
        try:
            # Try to get time from NTP pool
            try:
                ntp_time = self._get_ntp_time()
                local_time = time.time()
                drift = abs(ntp_time - local_time)
                
                if drift < 5:  # Less than 5 seconds drift
                    self._record(check_name, "PASS", f"Clock drift: {drift:.1f}s", "CRITICAL")
                else:
                    msg = f"Clock drift: {drift:.1f}s (consider syncing)"
                    self._record(check_name, "FAIL", msg, "WARNING")
            except Exception:
                # If NTP fails, just check local time is reasonable
                now = datetime.now(timezone.utc)
                if now.year >= 2024:  # System time in reasonable range
                    self._record(check_name, "PASS", f"System time: {now.isoformat()}", "CRITICAL")
                else:
                    self._record(check_name, "FAIL", f"System time appears incorrect: {now.isoformat()}", "CRITICAL")
                    raise SystemDiagnosticsException(f"System clock appears incorrect")
        except SystemDiagnosticsException:
            raise
        except Exception as e:
            self._record(check_name, "FAIL", str(e), "WARNING")

    def _get_ntp_time(self) -> float:
        """Get time from NTP server (with timeout)."""
        import socket
        import struct
        
        ntp_server = "pool.ntp.org"
        ntp_port = 123
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        
        try:
            sock.sendto(b"\x1b" + 47 * b"\0", (ntp_server, ntp_port))
            data, _ = sock.recvfrom(1024)
            timestamp = struct.unpack("!12I", data)[10]
            return timestamp - 2208988800  # NTP epoch offset
        finally:
            sock.close()

    def _check_duplicate_instances(self) -> None:
        """Detect if another AegisQuant instance is already running."""
        check_name = "Duplicate Instance Check"
        try:
            current_pid = os.getpid()

            # Build exclusion set: self + parent + all children
            exclude_pids = {current_pid}
            try:
                me = psutil.Process(current_pid)
                parent_pid = me.ppid()
                if parent_pid:
                    exclude_pids.add(parent_pid)
                for child in me.children(recursive=True):
                    exclude_pids.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            other_instances = []

            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    pid = proc.info["pid"]
                    if pid in exclude_pids:
                        continue

                    cmdline = proc.info.get("cmdline") or []
                    cmd_str = " ".join(cmdline)

                    if (
                        "Main_Production.py" in cmd_str
                        and "python" in cmd_str.lower()
                    ):
                        other_instances.append(pid)

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if other_instances:
                msg = f"Found {len(other_instances)} external instance(s): {other_instances}"
                self._record(check_name, "FAIL", msg, "WARNING")
            else:
                self._record(check_name, "PASS", "No duplicate instances found", "CRITICAL")

        except Exception as e:
            self._record(check_name, "SKIP", f"Could not check for duplicates: {e}", "INFO")

    def _make_binance_exchange(self):
        """Create a fresh ccxt.binance exchange object for diagnostics."""
        import ccxt
        binance_config = CONFIG["BROKERS"]["BINANCE"]
        ex = ccxt.binance({
            "apiKey":          binance_config["API_KEY"],
            "secret":          binance_config["SECRET"],
            "enableRateLimit": True,
            "timeout":         10000,   # 10 s (was 5 s — too tight on cold start)
            "options": {
                "defaultType":             "spot",
                "adjustForTimeDifference": True,
            },
        })
        if binance_config.get("TESTNET"):
            ex.set_sandbox_mode(True)
        return ex

    def _check_binance_api(self) -> None:
        """Verify Binance API connectivity and credentials.

        Why it crashes on every first cold-start
        ─────────────────────────────────────────
        CCXT's fetch_balance() triggers load_markets() internally on the first
        call.  load_markets() for Binance calls /sapi/v1/capital/config/getall,
        which is rate-limited and timestamp-sensitive.  On a freshly created
        exchange object the time-difference hasn't been synced yet, so the
        signed request can be rejected by Binance.  The second attempt (after
        the watchdog 30 s restart) always succeeds because the CCXT object or
        network stack has warmed up.

        Fix: retry up to 3 times, recreating the exchange object each time
        (a stale object with a failed load_markets() will not self-heal) and
        using a 3-step verification:
          1. Public ping   — pure connectivity, no auth
          2. Ticker fetch  — public market data
          3. Account fetch — auth check via /api/v3/account (lighter than
                             fetch_balance which triggers load_markets)
        """
        check_name   = "Binance API Connectivity"
        MAX_ATTEMPTS = 3
        RETRY_DELAY  = 3   # seconds between attempts

        try:
            import ccxt
            binance_config = CONFIG["BROKERS"]["BINANCE"]
            last_exc = None
            balance  = None

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    # Fresh exchange object every attempt — stale CCXT state
                    # (a half-loaded markets cache) will reproduce the same error.
                    exchange = self._make_binance_exchange()

                    # ── Step 1: public ping — pure network check, no auth ──
                    exchange.publicGetPing()   # GET /api/v3/ping

                    # ── Step 2: lightweight auth check ─────────────────────
                    # AVOID fetch_balance() — it calls loadMarkets() which
                    # internally calls /sapi/v1/capital/config/getall.
                    # That endpoint requires withdrawal/transfer permissions
                    # that a safe trading-only API key does NOT have.
                    #
                    # privateGetAccount() calls /api/v3/account directly:
                    #   • only needs "Enable Reading" permission
                    #   • returns balances without touching capital/config
                    account = exchange.privateGetAccount()
                    last_exc = None
                    break   # success

                except Exception as exc:
                    last_exc = exc
                    if attempt < MAX_ATTEMPTS:
                        self.logger.warning(
                            "Binance API attempt %d/%d failed (%s) — retrying in %ds...",
                            attempt, MAX_ATTEMPTS, exc, RETRY_DELAY,
                        )
                        time.sleep(RETRY_DELAY)
                    else:
                        self.logger.error(
                            "Binance API failed after %d attempts: %s", MAX_ATTEMPTS, exc,
                        )

            if last_exc is not None:
                self._record(check_name, "FAIL", str(last_exc), "CRITICAL")
                raise SystemDiagnosticsException(f"Binance API failed: {last_exc}")

            # Extract USDT balance from raw /api/v3/account response
            balances   = account.get("balances", [])
            usdt_entry = next((b for b in balances if b.get("asset") == "USDT"), {})
            total_balance = float(usdt_entry.get("free", 0)) + float(usdt_entry.get("locked", 0))
            self._record(
                check_name,
                "PASS",
                f"Connected to {'Testnet' if binance_config.get('TESTNET') else 'Live'}; "
                f"USDT Balance: {total_balance:.2f}",
                "CRITICAL",
            )

            # ── Symbol validity — use public ticker ping per symbol ────
            # Avoids load_markets() which triggers /sapi/v1/capital/config/getall
            symbols = CONFIG["SYMBOLS"].get("CRYPTO", {})
            invalid_symbols = []
            for symbol in symbols.keys():
                try:
                    exchange.publicGetTickerPrice({"symbol": symbol.replace("/", "")})
                except Exception:
                    invalid_symbols.append(symbol)

            if invalid_symbols:
                self._record(
                    "Binance Symbols", "FAIL",
                    f"Invalid symbols: {', '.join(invalid_symbols)}", "WARNING",
                )
            else:
                self._record(
                    "Binance Symbols", "PASS",
                    f"All {len(symbols)} configured symbols valid", "CRITICAL",
                )

        except SystemDiagnosticsException:
            raise   # already recorded above
        except Exception as e:
            self._record(check_name, "FAIL", str(e), "CRITICAL")
            raise SystemDiagnosticsException(f"Binance API failed: {e}")

    def _check_forex_api(self) -> None:
        """Verify OANDA API connectivity (if Forex enabled)."""
        check_name = "OANDA Forex API"
        try:
            oanda_config = CONFIG["BROKERS"].get("OANDA", {})
            if not oanda_config.get("API_KEY"):
                self._record(check_name, "SKIP", "OANDA credentials not configured", "INFO")
                return
            
            # Simple connectivity test
            import requests
            headers = {
                "Authorization": f"Bearer {oanda_config['API_KEY']}",
                "Content-Type": "application/json"
            }
            url = f"https://{'stream-' if not oanda_config.get('PRACTICE') else 'api-'}fxpractice.oanda.com/v3/accounts/{oanda_config['ACCOUNT_ID']}"
            
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                self._record(check_name, "PASS", "Connected to OANDA", "CRITICAL")
            else:
                self._record(check_name, "FAIL", f"HTTP {r.status_code}", "WARNING")
        except Exception as e:
            self._record(check_name, "FAIL", str(e), "WARNING")

    def _check_stocks_api(self) -> None:
        """Verify Alpaca API connectivity (if Stocks enabled)."""
        check_name = "Alpaca Stocks API"
        try:
            alpaca_config = CONFIG["BROKERS"].get("ALPACA", {})
            if not alpaca_config.get("API_KEY"):
                self._record(check_name, "SKIP", "Alpaca credentials not configured", "INFO")
                return
            
            # Simple connectivity test
            import requests
            headers = {
                "APCA-API-KEY-ID": alpaca_config["API_KEY"],
            }
            url = "https://paper-api.alpaca.markets/v2/account" if alpaca_config.get("PAPER") else "https://api.alpaca.markets/v2/account"
            
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                self._record(check_name, "PASS", "Connected to Alpaca", "CRITICAL")
            else:
                self._record(check_name, "FAIL", f"HTTP {r.status_code}", "WARNING")
        except Exception as e:
            self._record(check_name, "FAIL", str(e), "WARNING")

    def _check_models(self) -> None:
        """Verify ML model files exist and can be loaded."""
        check_name = "ML Models"
        try:
            if not CONFIG["AI"]["ENABLED"]:
                self._record(check_name, "SKIP", "AI disabled in config", "INFO")
                return
            
            model_path = CONFIG["AI"]["MODEL_PATH"]
            if not os.path.exists(model_path):
                self._record(check_name, "FAIL", f"Model directory not found: {model_path}", "WARNING")
                return
            
            # Check for required models
            crypto_symbols = CONFIG["SYMBOLS"].get("CRYPTO", {}).keys()
            model_files = os.listdir(model_path)
            found_models = [f for f in model_files if f.endswith(".joblib")]
            
            if found_models:
                self._record(check_name, "PASS", f"Found {len(found_models)} model files", "CRITICAL")
            else:
                self._record(check_name, "FAIL", "No .joblib model files found", "WARNING")
        except Exception as e:
            self._record(check_name, "FAIL", str(e), "WARNING")

    def _check_telegram(self) -> None:
        """Verify Telegram integration is working."""
        check_name = "Telegram Service"
        try:
            if not CONFIG["TELEGRAM"]["ENABLED"]:
                self._record(check_name, "SKIP", "Telegram disabled in config", "INFO")
                return
            
            token = CONFIG["TELEGRAM"].get("TOKEN")
            chat_id = CONFIG["TELEGRAM"].get("CHAT_ID")
            
            if not token or not chat_id:
                self._record(check_name, "FAIL", "Telegram TOKEN or CHAT_ID missing", "WARNING")
                return
            
            # Test send a simple message
            import requests
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": "✓ AegisQuant Telegram service test",
                "parse_mode": "Markdown"
            }
            
            r = requests.post(url, data=payload, timeout=5)
            if r.status_code == 200:
                self._record(check_name, "PASS", "Test message sent successfully", "CRITICAL")
            else:
                self._record(check_name, "FAIL", f"HTTP {r.status_code}", "WARNING")
        except Exception as e:
            self._record(check_name, "FAIL", str(e), "WARNING")

    def _print_summary(self) -> None:
        """Print diagnostic summary."""
        self.logger.info("=" * 70)
        self.logger.info("DIAGNOSTIC SUMMARY")
        self.logger.info("=" * 70)
        self.logger.info(f"Passed: {self.checks_passed}")
        self.logger.info(f"Failed: {self.checks_failed}")
        self.logger.info(f"Skipped: {self.checks_skipped}")
        
        critical_failures = [r for r in self.results if r["status"] == "FAIL" and r["severity"] == "CRITICAL"]
        if critical_failures:
            self.logger.critical(f"CRITICAL FAILURES ({len(critical_failures)}):")
            for r in critical_failures:
                self.logger.critical(f"  - {r['check']}: {r['message']}")
        
        self.logger.info("=" * 70)
        
        # Save diagnostics to file
        diagnostics_file = os.path.join(CONFIG["REPORTING"]["LOG_DIR"], "diagnostics.json")
        try:
            os.makedirs(os.path.dirname(diagnostics_file), exist_ok=True)
            with open(diagnostics_file, "w") as f:
                json.dump({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "passed": self.checks_passed,
                    "failed": self.checks_failed,
                    "skipped": self.checks_skipped,
                    "results": self.results
                }, f, indent=2)
            self.logger.info(f"Diagnostics saved to {diagnostics_file}")
        except Exception as e:
            self.logger.warning(f"Could not save diagnostics: {e}")
