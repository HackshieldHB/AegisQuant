"""
SymbolNormalizer — Centralized symbol format normalization.
-----------------------------------------------------------
Ensures consistent symbol handling across all modules:
- With slash: "BTC/USDT" (for markets, dicts)
- Without slash: "BTCUSDT" (for WebSocket, stream keys)
"""

from typing import Dict

# Cache for normalized symbols to avoid repeated conversions
_SYMBOL_CACHE: Dict[tuple, str] = {}


def normalize_symbol(symbol: str, fmt: str = "with_slash") -> str:
    """
    Normalize symbol to consistent format.
    
    Args:
        symbol: Raw symbol (e.g., "BTC/USDT", "BTCUSDT", "BTC/usdt")
        fmt: Target format - "with_slash" or "no_slash"
    
    Returns:
        Normalized symbol in requested format (e.g., "BTC/USDT" or "BTCUSDT")
    """
    cache_key = (symbol, fmt)
    if cache_key in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[cache_key]
    
    # Clean input: uppercase and remove whitespace
    clean = symbol.upper().strip()
    
    if fmt == "with_slash":
        # Target: "BTC/USDT"
        if "/" in clean:
            result = clean
        else:
            # "BTCUSDT" → "BTC/USDT"
            if clean.endswith("USDT"):
                parts = clean[:-4]  # Remove "USDT"
                result = f"{parts}/USDT"
            elif clean.endswith("BUSD"):
                parts = clean[:-4]
                result = f"{parts}/BUSD"
            else:
                # Assume format like "EURUSD"
                if len(clean) == 6:
                    result = f"{clean[:3]}/{clean[3:]}"
                else:
                    result = clean
    else:  # fmt == "no_slash"
        # Target: "BTCUSDT"
        result = clean.replace("/", "").replace("-", "")
    
    _SYMBOL_CACHE[cache_key] = result
    return result


def clear_symbol_cache() -> None:
    """Clear the symbol normalization cache."""
    global _SYMBOL_CACHE
    _SYMBOL_CACHE.clear()
