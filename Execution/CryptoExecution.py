"""
CryptoExecution — Production CCXT execution engine.
----------------------------------------------------
- Precision formatting (amount/price)
- Retry with exponential backoff
- Order fill verification (poll until filled/canceled/timeout)
- Duplicate order cooldown per symbol
- Standardized result dict
"""

import time
import asyncio
import random
from typing import Dict, Optional, Any

import ccxt
from Execution.BaseExecution import BaseExecution, execution_result
from AegisQuantConfig import CONFIG
from Core.Logger import AG_LOGGER
from Core.BinanceTimeSync import BinanceTimeSync


def _normalize_symbol(symbol: str) -> str:
    if "/" not in symbol and "USDT" in symbol.upper():
        return symbol.replace("USDT", "/USDT").replace("usdt", "/USDT")
    return symbol


class CryptoExecution(BaseExecution):
    EXCHANGE_ID = "binance"

    def __init__(self) -> None:
        self.logger = AG_LOGGER
        self.exchange: Optional[ccxt.Exchange] = None
        self._last_order_ts: Dict[str, float] = {}
        self._cooldown_sec = CONFIG.get("EXECUTION", {}).get("ORDER_COOLDOWN_SEC", 60)
        self._fill_poll_interval = CONFIG.get("EXECUTION", {}).get("FILL_POLL_INTERVAL_SEC", 2)
        self._fill_poll_timeout = CONFIG.get("EXECUTION", {}).get("FILL_POLL_TIMEOUT_SEC", 30)
        self._retry_attempts = CONFIG.get("EXECUTION", {}).get("RETRY_ATTEMPTS", 3)
        self._retry_base_delay = CONFIG.get("EXECUTION", {}).get("RETRY_BASE_DELAY_SEC", 2)
        self._symbol_min_costs: Dict[str, float] = {}  # Cache for min notional costs
        self._last_valid_balance: Dict[str, Dict[str, Any]] = {}
        self._trading_suspended = False
        self._balance_unknown = False
        self._time_offset_ms = 0
        self._connect()

    def _connect(self) -> None:
        try:
            broker = CONFIG["BROKERS"]["BINANCE"]
            exchange_class = getattr(ccxt, self.EXCHANGE_ID)
            mode = CONFIG["PROJECT"]["MODE"]
            # Changed to always use 'spot' trading (futures trading requires separate API key setup)
            default_type = "spot"
            self.exchange = exchange_class({
                "apiKey": broker["API_KEY"],
                "secret": broker["SECRET"],
                "enableRateLimit": True,
                "timeout": 30000,
                "options": {
                    "defaultType": default_type,
                    "adjustForTimeDifference": True,
                    # Load only spot markets — prevents CCXT calling
                    # /sapi/v1/margin/allPairs (no margin permissions).
                    "fetchMarkets": ["spot"],
                    # Do NOT fetch currencies — that calls /sapi/v1/capital/config/getall
                    # which requires withdrawal permissions this key doesn't have.
                    # The engine only needs USDT balances + market precision, not
                    # currency deposit/withdrawal metadata.
                    "fetchCurrencies": False,
                },
            })
            if broker.get("TESTNET"):
                self.exchange.set_sandbox_mode(True)
            self.exchange.load_markets()

            # Initialize BinanceTimeSync with exchange instance
            try:
                self._time_sync = BinanceTimeSync(exchange=self.exchange)
                self._time_sync.sync()
                self._time_offset_ms = self._time_sync.offset_ms

                # Use official CCXT adjustment instead of monkey-patching
                self.exchange.options['adjustForTimeDifference'] = True

                self.logger.info(
                    "BinanceTimeSync: offset=%dms (within safety limit)",
                    self._time_offset_ms,
                )
            except Exception as ts_err:
                self.logger.warning("BinanceTimeSync initial sync failed: %s", ts_err)

            self.logger.info(
                "CryptoExecution connected to %s (testnet=%s)",
                self.EXCHANGE_ID,
                broker.get("TESTNET", False),
            )
        except Exception as e:
            self.logger.error("CryptoExecution connect failed: %s", e)
            self.exchange = None

    def _cooldown_key(self, symbol: str, side: str) -> str:
        return f"{_normalize_symbol(symbol)}:{side.upper()}"

    def _in_cooldown(self, symbol: str, side: str) -> bool:
        key = self._cooldown_key(symbol, side)
        last = self._last_order_ts.get(key, 0)
        return (time.time() - last) < self._cooldown_sec

    def _set_cooldown(self, symbol: str, side: str) -> None:
        self._last_order_ts[self._cooldown_key(symbol, side)] = time.time()

    def _amount_to_precision(self, symbol: str, amount: float) -> float:
        if not self.exchange or not self.exchange.markets:
            return amount
        try:
            s = self.exchange.amount_to_precision(symbol, amount)
            return float(s)
        except Exception:
            return amount

    def _price_to_precision(self, symbol: str, price: float) -> float:
        if not self.exchange or not self.exchange.markets:
            return price
        try:
            s = self.exchange.price_to_precision(symbol, price)
            return float(s)
        except Exception:
            return price

    def _with_backoff(self, func, *args, **kwargs) -> Any:
        """
        Phase 11 Pre-Live Remediation: Rate Limit Protection.
        Wraps critical CCXT exchange requests with Exponential Backoff
        to survive HTTP 429 RateLimitExceeded and DDoSProtection bans.
        """
        last_error = None
        for attempt in range(self._retry_attempts):
            try:
                return func(*args, **kwargs)
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.RequestTimeout,
                    ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                # Only retry transient / rate-limit errors.
                # Non-retriable logical errors (InsufficientFunds, InvalidOrder,
                # BadSymbol…) are subclasses of ExchangeError but NOT of NetworkError,
                # so they fall through to the bare `except Exception` below and raise
                # immediately rather than wasting 2–4 s of backoff sleep.
                last_error = e
                if attempt < self._retry_attempts - 1:
                    # Exponential backoff: 2s, 4s, 8s + Jitter (M-3 Fix)
                    delay = self._retry_base_delay * (2 ** attempt)
                    jitter = random.uniform(0, delay * 0.25)
                    delay += jitter
                    self.logger.warning("API Network Error (429/Timeout): Retrying %s in %.1fs. Err: %s", func.__name__, delay, e)
                    time.sleep(delay)
                else:
                    self.logger.critical("API Network FATAL: Max retries exceeded for %s.", func.__name__)
                    raise e
            except Exception as e:
                # Non-retriable: logical errors (InsufficientFunds, InvalidOrder, etc.)
                # or locally raised exceptions — propagate immediately.
                raise e
        raise last_error

    def _wait_for_fill(self, symbol: str, order_id: str, side: str) -> Dict[str, Any]:
        deadline = time.time() + self._fill_poll_timeout
        while time.time() < deadline:
            try:
                # Wait for fill wrapped with network backoff to survive temporary bans mid-flight
                order = self._with_backoff(self.exchange.fetch_order, order_id, symbol)
                status = (order.get("status") or "").lower()
                filled = float(order.get("filled", 0) or 0)
                average = float(order.get("average") or order.get("price") or 0)
                if status in ("closed", "filled") and filled > 0:
                    return execution_result(
                        "filled", symbol, side,
                        filled_qty=filled, avg_price=average, order_id=order_id,
                    )
                if status in ("canceled", "cancelled", "rejected", "expired"):
                    if filled > 0:
                        return execution_result(
                            "filled", symbol, side,
                            filled_qty=filled, avg_price=average, order_id=order_id,
                            message=f"Partial fill completed before order {status}"
                        )
                    return execution_result(
                        "rejected", symbol, side, order_id=order_id,
                        message=f"Order {status}",
                    )
            except Exception as e:
                self.logger.warning("Fetch order %s failed: %s", order_id, e)
            time.sleep(self._fill_poll_interval)
        return execution_result("timeout", symbol, side, order_id=order_id, message="Fill poll timeout")

    def get_symbol_min_cost(self, symbol: str) -> float:
        """Get minimum notional cost for a symbol from Binance market limits (cached)."""
        symbol = _normalize_symbol(symbol)
        
        # Return cached value if available
        if symbol in self._symbol_min_costs:
            return self._symbol_min_costs[symbol]
        
        try:
            if not self.exchange or not self.exchange.markets:
                return 5.0  # Default fallback
            
            market = self.exchange.market(symbol)
            limits = market.get("limits", {})
            cost_limits = limits.get("cost", {})
            min_cost = float(cost_limits.get("min", 5.0) or 5.0)
            
            # Cache the result
            self._symbol_min_costs[symbol] = min_cost
            return min_cost
        except Exception as e:
            self.logger.debug("Failed to get min cost for %s: %s. Using 5.0", symbol, e)
            self._symbol_min_costs[symbol] = 5.0
            return 5.0

    def get_available_symbols(self, symbols: Dict[str, list], balance: float) -> Dict[str, list]:
        """
        Filter symbols by trading ability with current balance.
        Returns symbols sorted by minimum cost (cheapest first for micro accounts).
        
        balance: current USDT balance
        symbols: dict of {symbol_name: [timeframes]}
        """
        # Define hard minimums per symbol (Binance actual vs expected)
        # These are stricter than Binance's defaults to ensure viable trades
        SYMBOL_MIN_COSTS = {
            "BTC/USDT": 5.0,
            "ETH/USDT": 5.0,
            "DOGE/USDT": 5.0,
            "XRP/USDT": 5.0,
            "SOL/USDT": 2.0,
            "SHIB/USDT": 1.0,
            "PEPE/USDT": 0.5,
        }
        
        available = {}
        symbol_costs = []
        
        for symbol_name, timeframes in symbols.items():
            norm_symbol = _normalize_symbol(symbol_name)
            
            # Use hard-coded minimum first, fall back to Binance fetch
            if norm_symbol in SYMBOL_MIN_COSTS:
                min_cost = SYMBOL_MIN_COSTS[norm_symbol]
            else:
                min_cost = self.get_symbol_min_cost(norm_symbol)
            
            symbol_costs.append((norm_symbol, min_cost, timeframes))
        
        # Sort by min_cost (ascending): cheaper pairs first for small balances
        symbol_costs.sort(key=lambda x: x[1])
        
        # Log all pair costs for clarity
        cost_summary = " | ".join([f"{s}=${c:.2f}" for s, c, _ in symbol_costs])
        self.logger.info("💰 Binance min costs: %s", cost_summary)
        
        # Filter by balance
        for symbol, min_cost, timeframes in symbol_costs:
            if balance >= min_cost:
                available[symbol] = timeframes
                self.logger.info("✅ %s: balance=%.2f >= cost=%.2f [TRADEABLE]", symbol, balance, min_cost)
            else:
                self.logger.warning("❌ %s: balance=%.2f < cost=%.2f [SKIP]", symbol, balance, min_cost)
        
        # Final summary
        if available:
            available_names = list(available.keys())
            self.logger.info("\n🎯 AVAILABLE FOR TRADING (balance=%.2f): %s\n", balance, available_names)
        else:
            self.logger.error("⚠️  NO TRADEABLE SYMBOLS: balance=%.2f is below ALL minimums", balance)
        
        return available

    def open_position(
        self,
        symbol: str,
        qty: float,
        side: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> Dict:
        symbol = _normalize_symbol(symbol)
        if not self.exchange:
            return execution_result("rejected", symbol, side, message="Not connected")

        if self._in_cooldown(symbol, side):
            return execution_result("rejected", symbol, side, message="Cooldown active")

        try:
            if not self.exchange.markets:
                self.exchange.load_markets()
            market = self.exchange.market(symbol)
            limits = market.get("limits", {})
            cost_limits = limits.get("cost", {})
            amount_limits = limits.get("amount", {})
            
            # Reduce min_cost for micro accounts (<$100) to allow smaller orders
            try:
                balance = self.get_balance()
                is_micro = balance < 100
                default_min_cost = 2.0 if is_micro else 5.0
            except Exception:
                default_min_cost = 5.0
                is_micro = False
            
            min_cost = float(cost_limits.get("min", default_min_cost) or default_min_cost)
            min_amount = float(amount_limits.get("min", 0) or 0)

            qty = self._amount_to_precision(symbol, qty)
            if qty < min_amount:
                return execution_result("rejected", symbol, side, message=f"Qty {qty} < min {min_amount}")
                
            # Use _with_backoff so a transient timeout here doesn't
            # silently abort the position open with no retry.
            ticker = self._with_backoff(self.exchange.fetch_ticker, symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0)
            cost = qty * price
            
            # Phase 3: Auto-Upscale if rounding drops below min cost
            if cost < min_cost:
                try:
                    balance = self.get_balance()
                except Exception:
                    balance = 0
                    
                if min_cost <= balance:
                    # Add 1% buffer to ensure we pass exchange precision floor
                    adjusted_qty = self._amount_to_precision(symbol, (min_cost / price) * 1.02)
                    adjusted_cost = adjusted_qty * price
                    if adjusted_cost >= min_cost and adjusted_cost <= balance:
                        self.logger.info("Phase 3: Auto-upscaled [%s] size from %s (%.2f) to %s (%.2f) to meet min_cost %s", symbol, qty, cost, adjusted_qty, adjusted_cost, min_cost)
                        qty = adjusted_qty
                        cost = adjusted_cost
                    else:
                        return execution_result("rejected", symbol, side, message=f"Notional {cost:.2f} < Min {min_cost} and auto-upscale failed.")
                else:
                    return execution_result("rejected", symbol, side, message=f"Cost {cost:.2f} < min {min_cost} and insufficient balance.")

            # Phase 5: Ensure strictly MARKET orders
            mode = CONFIG["PROJECT"]["MODE"]
            if mode != "LIVE" and mode != "PAPER":
                self._set_cooldown(symbol, side)
                self.logger.info("[PAPER] Open %s %s qty=%s cost=%.2f", side, symbol, qty, cost)
                return execution_result("filled", symbol, side, filled_qty=qty, avg_price=price, order_id="paper_1")

            order = None
            last_error = None
            
            # Generate deterministic order ID (max 36 chars for Binance)
            client_oid = f"ag_{int(time.time()*1000)}_{symbol.replace('/', '')}_{side}"[:36]
            
            for attempt in range(self._retry_attempts):
                try:
                    order = self.exchange.create_order(
                        symbol, "market", side.lower(), qty,
                        params={"newClientOrderId": client_oid}
                    )
                    break
                except Exception as e:
                    last_error = e
                    
                    # Idempotency fetch to prevent duplicate fills on network timeout
                    try:
                        fetched_order = self.exchange.fetch_order(client_oid, symbol)
                        if fetched_order:
                            order = fetched_order
                            self.logger.info("Idempotency match: Order %s found on exchange despite timeout.", client_oid)
                            break
                    except Exception:
                        pass # OrderNotFound or another timeout; safe to retry
                    
                    if attempt < self._retry_attempts - 1:
                        delay = self._retry_base_delay * (2 ** attempt)
                        self.logger.warning("Create order attempt %s failed, retry in %ss: %s", attempt + 1, delay, e)
                        time.sleep(delay)
            if not order:
                self._set_cooldown(symbol, side)
                return execution_result("rejected", symbol, side, message=str(last_error or "Unknown error"))

            status = (order.get("status") or "").lower()
            order_id = str(order.get("id", ""))
            filled = float(order.get("filled", 0) or 0)
            average = float(order.get("average") or order.get("price") or price)

            if status in ("closed", "filled") and filled > 0:
                self._set_cooldown(symbol, side)
                # --- FIX 2: SL MINIMUM DISTANCE GUARD (LONG + SHORT SAFE) ---
                if sl is not None:
                    min_gap = average * 0.0015  # 0.15% minimum distance from fill
                    if side.lower() == "buy" and (average - sl) < min_gap:
                        old_sl = sl
                        sl = average - min_gap
                        self.logger.warning(
                            "SL_ADJUSTED_LONG | %s | pushed SL from %.5f to %.5f (min_gap=%.5f)",
                            symbol, old_sl, sl, min_gap,
                        )
                    elif side.lower() == "sell" and (sl - average) < min_gap:
                        old_sl = sl
                        sl = average + min_gap
                        self.logger.warning(
                            "SL_ADJUSTED_SHORT | %s | pushed SL from %.5f to %.5f (min_gap=%.5f)",
                            symbol, old_sl, sl, min_gap,
                        )
                if sl is not None or tp is not None:
                    sl_tp_success = self._place_sl_tp(symbol, side, qty, sl, tp)
                    if not sl_tp_success:
                        self.logger.critical("SL_PLACEMENT_FAILED_EMERGENCY_CLOSE for %s", symbol)
                        ec = self.close_position(symbol, qty=qty)
                        if not ec or ec.get("status") not in ("filled", "closed"):
                            self.logger.critical(
                                "GHOST POSITION RISK: SL failed AND emergency close failed for %s — manual action required!", symbol
                            )
                            try:
                                from Services.TelegramService import TelegramService as _TG
                                _TG().send_message(
                                    f"🚨 GHOST POSITION: SL+emergency-close both failed for {symbol}. Intervene NOW.",
                                    severity="CRITICAL",
                                )
                            except Exception:
                                pass
                        return execution_result("rejected", symbol, side, message="SL_PLACEMENT_FAILED_EMERGENCY_CLOSE", sl_status="FAILED / EMERGENCY_CLOSED")
                    else:
                        return execution_result("filled", symbol, side, filled_qty=filled, avg_price=average, order_id=order_id, sl_status="PLACED")
                return execution_result("filled", symbol, side, filled_qty=filled, avg_price=average, order_id=order_id, sl_status="N/A")
            if status == "open":
                result = self._wait_for_fill(symbol, order_id, side)
                if sl is not None or tp is not None:
                    sl_tp_success = self._place_sl_tp(symbol, side, result["filled_qty"], sl, tp)
                    if not sl_tp_success:
                        self.logger.critical("SL_PLACEMENT_FAILED_EMERGENCY_CLOSE for %s post-fill", symbol)
                        ec_post = self.close_position(symbol, qty=result["filled_qty"])
                        if not ec_post or ec_post.get("status") not in ("filled", "closed"):
                            self.logger.critical(
                                "GHOST POSITION RISK: SL failed AND emergency close failed for %s (post-fill) — manual action required!", symbol
                            )
                            try:
                                from Services.TelegramService import TelegramService as _TG
                                _TG().send_message(
                                    f"🚨 GHOST POSITION: SL+emergency-close both failed for {symbol} (post-fill). Intervene NOW.",
                                    severity="CRITICAL",
                                )
                            except Exception:
                                pass
                        self._set_cooldown(symbol, side)
                        return execution_result("rejected", symbol, side, order_id=order_id, message="SL_PLACEMENT_FAILED_EMERGENCY_CLOSE", sl_status="FAILED / EMERGENCY_CLOSED")
                    else:
                        result["sl_status"] = "PLACED"
                    self._set_cooldown(symbol, side)
                return result
            self._set_cooldown(symbol, side)
            return execution_result("rejected", symbol, side, order_id=order_id, message=f"Status {status}")
        except Exception as e:
            self.logger.error("Open position failed: %s", e)
            return execution_result("rejected", symbol, side, message=str(e))

    def _place_sl_tp(
        self,
        symbol: str,
        side: str,
        qty: float,
        sl: Optional[float],
        tp: Optional[float],
    ) -> bool:
        try:
            qty = self._amount_to_precision(symbol, qty)
            close_side = "sell" if side.lower() == "buy" else "buy"
            
            def _execute_sl_tp(exec_qty: float):
                if sl is not None and tp is not None:
                    # OCO Order for Binance Spot via direct endpoint
                    sl_prec = self._price_to_precision(symbol, sl)
                    tp_prec = self._price_to_precision(symbol, tp)
                    market = self.exchange.market(symbol)
                    
                    # stopLimitPrice must have a 0.1% buffer BELOW stopPrice (for
                    # close-long OCO).  When both are equal Binance fills the limit
                    # leg only if price hits exactly that tick — in a fast/gapped
                    # market the order hangs unfilled and the position is naked.
                    if close_side.lower() == "sell":
                        # closing a long: SL limit must be below trigger
                        sl_limit = sl_prec * 0.999
                    else:
                        # closing a short: SL limit must be above trigger
                        sl_limit = sl_prec * 1.001

                    params = {
                        "symbol": market["id"],
                        "side": close_side.upper(),
                        "quantity": self.exchange.amount_to_precision(symbol, exec_qty),
                        "price": self.exchange.price_to_precision(symbol, tp_prec),
                        "stopPrice": self.exchange.price_to_precision(symbol, sl_prec),
                        "stopLimitPrice": self.exchange.price_to_precision(symbol, sl_limit),
                        "stopLimitTimeInForce": "GTC"
                    }
                    
                    try:
                        # Direct CCXT native call for Binance Spot OCO
                        self.exchange.private_post_order_oco(params)
                    except Exception as e:
                        if "insufficient balance" in str(e).lower() or "-2010" in str(e):
                            raise e
                        self.logger.warning("OCO execution failed, attempting fallback sl limits: %s", e)
                        # Fallback: plain STOP_LOSS_LIMIT.  Binance requires limitPrice != stopPrice,
                        # so apply the same 0.1% buffer used in the primary OCO path.
                        if close_side.lower() == "sell":
                            sl_limit_fb = self._price_to_precision(symbol, sl_prec * 0.999)
                        else:
                            sl_limit_fb = self._price_to_precision(symbol, sl_prec * 1.001)
                        self.exchange.create_order(
                            symbol, "STOP_LOSS_LIMIT", close_side, exec_qty, sl_limit_fb,
                            params={"stopPrice": self._price_to_precision(symbol, sl_prec)}
                        )
                elif sl is not None:
                    sl_prec = self._price_to_precision(symbol, sl)
                    self.exchange.create_order(
                        symbol, "STOP_LOSS_LIMIT", close_side, exec_qty, sl_prec, params={"stopPrice": sl_prec}
                    )
                elif tp is not None:
                    tp_prec = self._price_to_precision(symbol, tp)
                    self.exchange.create_order(
                        symbol, "TAKE_PROFIT_LIMIT", close_side, exec_qty, tp_prec, params={"stopPrice": tp_prec}
                    )

            try:
                _execute_sl_tp(qty)
            except Exception as outer_e:
                if "insufficient balance" in str(outer_e).lower() or "-2010" in str(outer_e):
                    self.logger.warning("SL/TP placement blocked by Spot Fee deduction. Fetching exact free balance.")
                    base_asset = symbol.split('/')[0]
                    bal = self.exchange.fetch_balance()
                    available = float(bal.get(base_asset, {}).get("free", 0.0))
                    if 0 < available < qty:
                        new_qty = self._amount_to_precision(symbol, available)
                        self.logger.info("Retrying SL/TP with exact free balance: %.8f -> %.8f", qty, new_qty)
                        _execute_sl_tp(new_qty)
                    else:
                        raise outer_e
                else:
                    raise outer_e
                    
            return True
        except Exception as e:
            self.logger.error("SL/TP placement failed: %s", e)
            return False

    def close_position(
        self,
        symbol: str,
        position_id: Optional[str] = None,
        qty: Optional[float] = None,
    ) -> Dict:
        symbol = _normalize_symbol(symbol)
        if not self.exchange:
            return execution_result("rejected", symbol, "SELL", message="Not connected")
        try:
            if self.exchange.options.get("defaultType") == "future":
                positions = self.exchange.fetch_positions([symbol])
                for p in positions:
                    contracts = float(p.get("contracts", 0) or 0)
                    if contracts <= 0:
                        continue
                    side = "sell" if p.get("side", "").lower() == "long" else "buy"
                    close_qty = self._amount_to_precision(symbol, contracts)
                    order = self._with_backoff(self.exchange.create_order, symbol, "market", side, close_qty, params={"reduceOnly": True})
                    return execution_result(
                        "filled",
                        symbol,
                        side.upper(),
                        filled_qty=close_qty,
                        avg_price=float(order.get("average") or order.get("price") or 0),
                        order_id=str(order.get("id", "")),
                    )
                return execution_result("rejected", symbol, "SELL", message="No open position")
            if qty is not None and qty > 0:
                side = "sell"
                qty = self._amount_to_precision(symbol, qty)
                
                try:
                    order = self._with_backoff(self.exchange.create_order, symbol, "market", side, qty)
                except Exception as e:
                    if "insufficient balance" in str(e).lower() or "-2010" in str(e):
                        self.logger.warning("Close position blocked by Spot Fee deduction. Fetching exact free balance.")
                        base_asset = symbol.split('/')[0]
                        bal = self.exchange.fetch_balance()
                        available = float(bal.get(base_asset, {}).get("free", 0.0))
                        if 0 < available < qty:
                            new_qty = self._amount_to_precision(symbol, available)
                            self.logger.info("Retrying Close with exact free balance: %.8f -> %.8f", qty, new_qty)
                            order = self.exchange.create_order(symbol, "market", side, new_qty)
                            qty = new_qty
                        else:
                            raise e
                    else:
                        raise e
                        
                status = (order.get("status") or "").lower()
                if status in ("closed", "filled"):
                    return execution_result(
                        "filled", symbol, "SELL",
                        filled_qty=float(order.get("filled", qty)),
                        avg_price=float(order.get("average") or order.get("price") or 0),
                        order_id=str(order.get("id", "")),
                    )
                return execution_result("rejected", symbol, "SELL", order_id=str(order.get("id", "")), message=status)
            return execution_result("rejected", symbol, "SELL", message="Qty required for spot close")
        except Exception as e:
            self.logger.error("Close position failed: %s", e)
            return execution_result("rejected", symbol, "SELL", message=str(e))

    def get_open_positions(self) -> Any:
        if not self.exchange:
            return []
        try:
            if self.exchange.options.get("defaultType") != "future":
                return []
            positions = self._with_backoff(self.exchange.fetch_positions)
            return [p for p in positions if float(p.get("contracts", 0) or 0) > 0]
        except Exception as e:
            self.logger.error("Fetch positions failed: %s", e)
            return []

    def get_balance(self, asset: Optional[str] = None) -> Optional[float]:
        asset = asset or "USDT"
        if not self.exchange:
            self.logger.critical("BALANCE UNKNOWN — TRADING DISABLED for %s (Not connected)", asset)
            self._balance_unknown = True
            return None
        try:
            params = {"type": self.exchange.options.get("defaultType", "spot")}
            balances = self._with_backoff(self.exchange.fetch_balance, params)
            
            bal = 0.0
            found = False
            for key in ("total", "free"):
                if asset in balances.get(key, {}):
                    bal = float(balances[key][asset])
                    found = True
                    break
            
            if not found and asset in balances:
                bal = float(balances[asset])
                found = True
                
            if found:
                self._last_valid_balance[asset] = {"amount": bal, "timestamp": time.time()}
                self._balance_unknown = False
                return bal
            else:
                self.logger.warning("Asset %s not found in balance. Assuming 0.0.", asset)
                return 0.0
                
        except Exception as e:
            self.logger.error("Fetch balance failed: %s", e)
            if asset in self._last_valid_balance:
                self.logger.warning("BALANCE_FETCH_FAILED_USING_CACHED_VALUE for %s", asset)
                return self._last_valid_balance[asset]["amount"]
            else:
                self.logger.critical("BALANCE UNKNOWN — TRADING DISABLED for %s", asset)
                self._balance_unknown = True
                return None

    async def sync_time(self) -> bool:
        """Sync via BinanceTimeSync singleton (no CCXT monkey patching)."""
        if not self.exchange:
            self._trading_suspended = True
            return False

        try:
            ts = BinanceTimeSync(exchange=self.exchange)
            # Run the blocking HTTP call in a thread so the event loop stays free.
            # 8-second timeout: if Binance's /api/v3/time endpoint doesn't reply,
            # skip the sync rather than stalling the entire cycle.
            import asyncio as _asyncio
            offset = await _asyncio.wait_for(
                _asyncio.to_thread(ts.sync),
                timeout=8.0,
            )
            self._time_offset_ms = offset

            # Use official CCXT adjustment instead of monkey patch
            self.exchange.options['adjustForTimeDifference'] = True

            if abs(offset) > 5000:
                self.logger.critical(
                    "SYSTEM CLOCK OUT OF SYNC — TRADING SUSPENDED (Offset: %d ms)", offset
                )
                self._trading_suspended = True
                return False

            self._trading_suspended = False
            self.logger.info("BINANCE TIME SYNC SUCCESS — OFFSET = %d ms", offset)

            if abs(offset) > 1000:
                self.logger.warning(
                    "TIME_RESYNC_TRIGGERED: offset=%dms exceeds 1000ms", offset
                )

            return True

        except Exception as e:
            self.logger.critical("Failed to sync Binance time: %s", e)
            self._trading_suspended = True
            return False

    def get_account_summary(self) -> Dict[str, Any]:
        if not self.exchange:
            return {}
        try:
            params = {"type": self.exchange.options.get("defaultType", "spot")}
            info = self.exchange.fetch_balance(params)
            total = info.get("total", {})
            return {
                "equity": float(total.get("USDT", 0.0)),
                "balances": {"free": info.get("free", {}), "used": info.get("used", {}), "total": total},
                "currency": "USDT",
            }
        except Exception as e:
            self.logger.error("Account summary failed: %s", e)
            return {}


BinanceExecution = CryptoExecution
