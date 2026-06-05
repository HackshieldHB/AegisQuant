import numpy as np

def calculate_nonlinear_slippage(
    base_bps: float, 
    order_size: float, 
    adv_volume: float, 
    volatility_scalar: float = 1.0,
    is_market_order: bool = True
) -> float:
    """
    Nonlinear Slippage Model (Stage 1 Remediation).
    Replaces static constant basis point slippage with a liquidity-aware function.
    
    Formula: slippage_bps = base_bps * (1 + (order_size / ADV)^2) * vol_scalar
    
    Args:
        base_bps: The baseline slippage for a tiny, zero-impact order (e.g., 2.0 bps).
        order_size: The absolute quantity of the asset being traded.
        adv_volume: The Average Daily Volume (ADV) of the asset.
        volatility_scalar: A multiplier representing current regime (e.g., VIX spike = 2.0).
        is_market_order: If False (Limit order), assumes queue position capture and halves the impact.
        
    Returns:
        float: The dynamic slippage in basis points to apply to the execution price.
    """
    if adv_volume <= 0:
        # Total liquidity collapse assumptions
        return base_bps * 100.0 * volatility_scalar
        
    # Liquidity impact ratio
    impact_ratio = order_size / float(adv_volume)
    
    # Quadratic impact curve
    # 1.0 BTC on 100.0 ADV = (1/100)^2 = 0.0001
    # 50.0 BTC on 100.0 ADV = (50/100)^2 = 0.25 (Base slips gets scaled by 1.25x)
    dynamic_slippage = base_bps * (1.0 + (impact_ratio ** 2))
    
    # Volatility expansion
    dynamic_slippage *= volatility_scalar
    
    # Order type resolution
    if not is_market_order:
         # Limit orders suffer adverse selection and queue uncertainty,
         # but fundamentally capture spread better than Taker sweeps.
         dynamic_slippage *= 0.5
         
    return float(dynamic_slippage)
