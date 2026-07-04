import numpy as np
from scipy.stats import norm
from scipy.optimize import minimize
from scipy.integrate import quad
import yfinance as yf
from typing import Dict, List, Tuple, Any

# ==========================================
# 1. BLACK-SCHOLES GREEKS IMPLEMENTATION
# ==========================================

def bs_greeks(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str = "call"
) -> Dict[str, float]:
    """
    Computes Black-Scholes option price and Greeks (Delta, Gamma, Vanna, Charm).
    """
    if T <= 0:
        # Avoid division by zero at expiration
        price = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
        return {
            "price": price,
            "delta": 1.0 if option_type == "call" and S > K else (0.0 if option_type == "call" else (-1.0 if S < K else 0.0)),
            "gamma": 0.0,
            "vanna": 0.0,
            "charm": 0.0
        }
    
    if sigma <= 0:
        sigma = 1e-5  # Prevent division by zero
        
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    pdf_d1 = norm.pdf(d1)
    cdf_d1 = norm.cdf(d1)
    cdf_d2 = norm.cdf(d2)
    
    if option_type.lower() == "call":
        price = S * cdf_d1 - K * np.exp(-r * T) * cdf_d2
        delta = cdf_d1
        # Charm (Delta decay over time)
        charm = -pdf_d1 * (r / (sigma * np.sqrt(T)) - d2 / (2 * T))
    else:
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = cdf_d1 - 1.0
        # Put Charm
        charm = -pdf_d1 * (r / (sigma * np.sqrt(T)) - d2 / (2 * T)) # standard model assumption
        
    gamma = pdf_d1 / (S * sigma * np.sqrt(T))
    vanna = -pdf_d1 * d2 / sigma
    
    return {
        "price": float(price),
        "delta": float(delta),
        "gamma": float(gamma),
        "vanna": float(vanna),
        "charm": float(charm)
    }

# ==========================================
# 2. GAMMA EXPOSURE (GEX) MODELING
# ==========================================

def calculate_gex_profile(
    spot: float,
    option_chains: List[Dict[str, Any]],
    r: float = 0.05
) -> Tuple[Dict[float, float], float]:
    """
    Calculates notional GEX for each strike and determines Net GEX and the Zero Gamma Level.
    
    Each option chain dict should contain:
    - strike: float
    - type: 'call' or 'put'
    - open_interest: float
    - implied_volatility: float
    - dte: float (days to expiration)
    """
    strike_gex = {}
    net_gex = 0.0
    
    for opt in option_chains:
        K = opt["strike"]
        T = opt["dte"] / 365.0
        oi = opt["open_interest"]
        iv = opt["implied_volatility"]
        opt_type = opt["type"]
        
        # Calculate individual option gamma
        greeks = bs_greeks(S=spot, K=K, T=T, r=r, sigma=iv, option_type=opt_type)
        gamma = greeks["gamma"]
        
        # GEX Call = Gamma * OI * Contract Size (100) * Spot^2 * 0.01 = Gamma * OI * Spot^2
        # GEX Put = -Gamma * OI * Spot^2
        multiplier = 1.0 if opt_type.lower() == "call" else -1.0
        gex_val = multiplier * gamma * oi * (spot ** 2)
        
        strike_gex[K] = strike_gex.get(K, 0.0) + gex_val
        net_gex += gex_val
        
    # Find Gamma Flip (Zero Gamma Level) by evaluating Net GEX across a range of spot prices
    # We search in a range of +/- 20% of the current spot
    spots_grid = np.linspace(spot * 0.8, spot * 1.2, 80)
    net_gex_grid = []
    
    for s in spots_grid:
        gex_sum = 0.0
        for opt in option_chains:
            K = opt["strike"]
            T = opt["dte"] / 365.0
            oi = opt["open_interest"]
            iv = opt["implied_volatility"]
            opt_type = opt["type"]
            
            gamma = bs_greeks(S=s, K=K, T=T, r=r, sigma=iv, option_type=opt_type)["gamma"]
            multiplier = 1.0 if opt_type.lower() == "call" else -1.0
            gex_sum += multiplier * gamma * oi * (s ** 2)
        net_gex_grid.append(gex_sum)
        
    # Find crossing point
    flip_level = spot
    for i in range(len(net_gex_grid) - 1):
        if net_gex_grid[i] * net_gex_grid[i+1] <= 0:
            # Linear interpolation to find the exact zero level
            s1, s2 = spots_grid[i], spots_grid[i+1]
            g1, g2 = net_gex_grid[i], net_gex_grid[i+1]
            flip_level = s1 - g1 * (s2 - s1) / (g2 - g1)
            break
            
    return strike_gex, float(flip_level)

# ==========================================
# 3. SVI VOLATILITY SMILE CALIBRATION
# ==========================================

def svi_variance(k: np.ndarray, a: float, b: float, rho: float, m: float, sigma: float) -> np.ndarray:
    """
    Computes total implied variance w(k) under Stochastic Volatility Inspired (SVI) model:
    w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2))
    """
    return a + b * (rho * (k - m) + np.sqrt((k - m)**2 + sigma**2))

def check_svi_arbitrage(a: float, b: float, rho: float, m: float, sigma: float) -> bool:
    """
    Validates SVI parameters against static no-arbitrage conditions:
    1. b >= 0, sigma > 0, |rho| < 1
    2. a + b * sigma * sqrt(1 - rho^2) >= 0 (non-negative variance)
    3. Lee moment bound: b * (1 + |rho|) <= 4
    """
    if b < 0 or sigma <= 0 or abs(rho) >= 1:
        return False
    if a + b * sigma * np.sqrt(1 - rho**2) < 0:
        return False
    if b * (1 + abs(rho)) > 4.0:
        return False
    return True

def calibrate_svi(
    strikes: np.ndarray,
    implied_vols: np.ndarray,
    forward: float,
    T: float
) -> Dict[str, Any]:
    """
    Calibrates SVI variance curve to an option chain.
    - strikes: array of strike prices
    - implied_vols: array of implied volatilities (decimals, e.g. 0.20)
    - forward: forward price of underlying
    - T: time to maturity in years
    """
    log_moneyness = np.log(strikes / forward)
    market_variances = (implied_vols ** 2) * T
    
    # Objective function: mean squared error
    def objective(params):
        a, b, rho, m, sig = params
        model_vars = svi_variance(log_moneyness, a, b, rho, m, sig)
        mse = np.mean((market_variances - model_vars) ** 2)
        
        # Enforce hard constraints via penalties
        penalty = 0.0
        if b < 0: penalty += 1e5 * (0.0 - b)
        if sig <= 0: penalty += 1e5 * (1e-5 - sig)
        if abs(rho) >= 1.0: penalty += 1e5 * (abs(rho) - 0.999)
        if a + b * sig * np.sqrt(1 - rho**2) < 0:
            penalty += 1e5 * abs(a + b * sig * np.sqrt(1 - rho**2))
        # Lee moment bound penalty
        lee = b * (1 + abs(rho))
        if lee > 4.0:
            penalty += 1e5 * (lee - 4.0)
            
        return mse + penalty

    # Initial guesses: [a, b, rho, m, sigma]
    init_params = [0.04, 0.1, -0.3, 0.0, 0.1]
    bounds = [
        (None, None),     # a
        (0.0, 2.0),       # b
        (-0.99, 0.99),    # rho
        (-2.0, 2.0),      # m
        (1e-4, 1.0)       # sigma
    ]
    
    res = minimize(objective, init_params, bounds=bounds, method="L-BFGS-B")
    a, b, rho, m, sig = res.x
    
    # Verify Durrleman Condition (Butterfly Arbitrage)
    # Butterfly arbitrage is free if the implied probability density is non-negative.
    # PDF g(k) = (1 - k*w'/2w)^2 - w'^2/4 * (1/w + 1/4) + w''/2
    # We evaluate this on a grid of k values
    k_grid = np.linspace(-1.5, 1.5, 100)
    w_grid = svi_variance(k_grid, a, b, rho, m, sig)
    
    # Numerical derivatives of w(k)
    dk = k_grid[1] - k_grid[0]
    w_prime = np.gradient(w_grid, dk)
    w_prime_prime = np.gradient(w_prime, dk)
    
    durrleman_violation = False
    for i in range(len(k_grid)):
        w = w_grid[i]
        wp = w_prime[i]
        wpp = w_prime_prime[i]
        k = k_grid[i]
        
        # Density formula g(k)
        term1 = (1.0 - (k * wp) / (2.0 * w)) ** 2
        term2 = (wp ** 2 / 4.0) * (1.0 / w + 0.25)
        term3 = wpp / 2.0
        g_k = term1 - term2 + term3
        
        if g_k < 0:
            durrleman_violation = True
            break

    return {
        "params": {
            "a": float(a),
            "b": float(b),
            "rho": float(rho),
            "m": float(m),
            "sigma": float(sig)
        },
        "success": bool(res.success and check_svi_arbitrage(a, b, rho, m, sig)),
        "durrleman_arbitrage_free": not durrleman_violation,
        "fitted_vols": list(np.sqrt(svi_variance(log_moneyness, a, b, rho, m, sig) / T))
    }

# ==========================================
# 4. HESTON STOCHASTIC VOLATILITY MODEL
# ==========================================

def heston_characteristic_function(
    u: Any, S0: float, v0: float, kappa: float, theta: float, xi: float, rho: float, T: float, r: float
) -> Any:
    """
    Computes Heston characteristic function for options pricing.
    """
    x = np.log(S0)
    a = kappa * theta
    
    # SDE helpers
    d = np.sqrt((rho * xi * u * 1j - kappa)**2 + xi**2 * (u * 1j + u**2))
    g = (kappa - rho * xi * u * 1j - d) / (kappa - rho * xi * u * 1j + d)
    
    # Complex exponentials
    C = (r * u * 1j * T + 
         (a / xi**2) * ((kappa - rho * xi * u * 1j - d) * T - 2.0 * np.log((1.0 - g * np.exp(-d * T)) / (1.0 - g))))
    
    D = ((kappa - rho * xi * u * 1j - d) / xi**2) * ((1.0 - np.exp(-d * T)) / (1.0 - g * np.exp(-d * T)))
    
    return np.exp(C + D * v0 + u * x * 1j)

def heston_price(
    S0: float, K: float, T: float, r: float, v0: float, kappa: float, theta: float, xi: float, rho: float, option_type: str = "call"
) -> float:
    """
    Prices options under Heston model using numerical integration.
    """
    # Heston integration integrand
    def integrand(u, j):
        # We need complex evaluation
        u_complex = u - (1j if j == 1 else 0j)
        num = heston_characteristic_function(u_complex, S0, v0, kappa, theta, xi, rho, T, r)
        den = u * 1j * heston_characteristic_function(-1j if j == 1 else 0j, S0, v0, kappa, theta, xi, rho, T, r)
        val = np.exp(-1j * u * np.log(K)) * num / den
        return np.real(val)

    # Numerical integration up to a large upper bound (e.g., 100)
    P1_int, _ = quad(lambda u: integrand(u, 1), 1e-8, 100.0, limit=200)
    P2_int, _ = quad(lambda u: integrand(u, 2), 1e-8, 100.0, limit=200)
    
    P1 = 0.5 + P1_int / np.pi
    P2 = 0.5 + P2_int / np.pi
    
    # Clip probabilities to [0, 1]
    P1 = np.clip(P1, 0.0, 1.0)
    P2 = np.clip(P2, 0.0, 1.0)
    
    call_price = S0 * P1 - K * np.exp(-r * T) * P2
    
    if option_type.lower() == "call":
        return float(max(0.0, call_price))
    else:
        # Put-Call Parity: P = C - S + K*e^(-rT)
        put_price = call_price - S0 + K * np.exp(-r * T)
        return float(max(0.0, put_price))

def calibrate_heston(
    strikes: np.ndarray,
    market_prices: np.ndarray,
    S0: float,
    T: float,
    r: float = 0.05,
    option_type: str = "call"
) -> Dict[str, Any]:
    """
    Calibrates Heston model parameters to option market prices.
    Parameters to calibrate: kappa, theta, xi, rho, v0
    """
    def objective(params):
        kappa, theta, xi, rho, v0 = params
        
        # Penalty functions
        penalty = 0.0
        if kappa <= 0: penalty += 1e4 * (0.001 - kappa)
        if theta <= 0: penalty += 1e4 * (0.001 - theta)
        if xi <= 0: penalty += 1e4 * (0.001 - xi)
        if abs(rho) >= 1.0: penalty += 1e4 * (abs(rho) - 0.99)
        if v0 <= 0: penalty += 1e4 * (0.001 - v0)
        
        # Feller's condition constraint: 2 * kappa * theta > xi^2
        feller = 2.0 * kappa * theta - xi**2
        if feller <= 0:
            penalty += 1e4 * abs(feller)
            
        sse = 0.0
        for i in range(len(strikes)):
            try:
                model_pr = heston_price(S0, strikes[i], T, r, v0, kappa, theta, xi, rho, option_type)
                sse += (market_prices[i] - model_pr) ** 2
            except Exception:
                sse += 1e4
                
        return sse + penalty

    # Initial parameter guess: [kappa, theta, xi, rho, v0]
    init_params = [2.0, 0.04, 0.2, -0.5, 0.04]
    bounds = [
        (0.01, 10.0),    # kappa
        (0.001, 1.0),    # theta
        (0.01, 2.0),     # xi
        (-0.95, 0.95),   # rho
        (0.001, 1.0)     # v0
    ]
    
    res = minimize(objective, init_params, bounds=bounds, method="L-BFGS-B")
    kappa, theta, xi, rho, v0 = res.x
    feller_stat = 2.0 * kappa * theta - xi**2
    
    return {
        "params": {
            "kappa": float(kappa),
            "theta": float(theta),
            "xi": float(xi),
            "rho": float(rho),
            "v0": float(v0)
        },
        "success": bool(res.success and feller_stat > 0),
        "feller_satisfied": bool(feller_stat > 0),
        "feller_value": float(feller_stat)
    }

# ==========================================
# 5. INTEGRATED DATA SNAPPING PIPELINE
# ==========================================

def fetch_local_gex_data(ticker_symbol: str) -> Dict[str, Any]:
    """
    Fetches raw option chain data via yfinance, computes the BS Greeks,
    performs SVI smile calibration, and returns the aggregate GEX profile.
    This acts as our offline numerical Greeks integration core.
    """
    ticker = yf.Ticker(ticker_symbol)
    history = ticker.history(period="1d")
    if history.empty:
        raise ValueError(f"Could not retrieve stock price for {ticker_symbol}")
    
    spot_price = float(history["Close"].iloc[-1])
    expirations = ticker.options
    
    if not expirations:
        raise ValueError(f"No options found for {ticker_symbol}")
        
    # Take the nearest maturity
    nearest_expiry = expirations[0]
    opt_chain = ticker.option_chain(nearest_expiry)
    
    # Calculate days to expiration
    from datetime import datetime
    today = datetime.now().date()
    expiry_date = datetime.strptime(nearest_expiry, "%Y-%m-%d").date()
    dte = max(1.0, float((expiry_date - today).days))
    
    # Build list of options for GEX calculations
    option_chains_list = []
    
    # Process calls
    for idx, row in opt_chain.calls.iterrows():
        # yfinance impliedVolatility is in decimals, e.g. 0.23
        iv = row["impliedVolatility"]
        oi = row["openInterest"]
        if np.isnan(iv) or iv <= 0:
            iv = 0.25 # fallback
        if np.isnan(oi) or oi < 0:
            oi = 0
            
        option_chains_list.append({
            "strike": float(row["strike"]),
            "type": "call",
            "open_interest": float(oi),
            "implied_volatility": float(iv),
            "dte": dte
        })
        
    # Process puts
    for idx, row in opt_chain.puts.iterrows():
        iv = row["impliedVolatility"]
        oi = row["openInterest"]
        if np.isnan(iv) or iv <= 0:
            iv = 0.25
        if np.isnan(oi) or oi < 0:
            oi = 0
            
        option_chains_list.append({
            "strike": float(row["strike"]),
            "type": "put",
            "open_interest": float(oi),
            "implied_volatility": float(iv),
            "dte": dte
        })
        
    # Calculate GEX and Flip level
    strike_gex, flip_level = calculate_gex_profile(spot_price, option_chains_list, r=0.05)
    net_gex = sum(strike_gex.values())
    
    # SVI Calibration on Calls with open interest
    valid_calls = [opt for opt in option_chains_list if opt["type"] == "call" and opt["open_interest"] > 0]
    if len(valid_calls) >= 5:
        strikes = np.array([opt["strike"] for opt in valid_calls])
        vols = np.array([opt["implied_volatility"] for opt in valid_calls])
        svi_results = calibrate_svi(strikes, vols, spot_price, dte / 365.0)
    else:
        svi_results = {"success": False, "params": {}}
        
    return {
        "ticker": ticker_symbol,
        "underlying_price": spot_price,
        "dte": dte,
        "net_gex": net_gex,
        "gamma_flip": flip_level,
        "svi_calibration": svi_results,
        "strike_gex_count": len(strike_gex)
    }

if __name__ == "__main__":
    # Quick self-test
    print("Testing math library...")
    res = fetch_local_gex_data("SPY")
    print(f"SPY Spot: {res['underlying_price']}")
    print(f"Net GEX: {res['net_gex']}")
    print(f"Gamma Flip: {res['gamma_flip']}")
    print(f"SVI Calibrated parameters: {res['svi_calibration']['params']}")
