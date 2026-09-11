"""Black-Scholes pricing engine and paper options-chain generator."""
import math as _math
from datetime import date, timedelta


def norm_cdf(x):
    return (1 + _math.erf(x / _math.sqrt(2))) / 2


def norm_pdf(x):
    return _math.exp(-x**2 / 2) / _math.sqrt(2 * _math.pi)


def bs_price(S, K, T, sigma, r=0.05, opt='call'):
    """Black-Scholes option price. S=spot, K=strike, T=years, sigma=IV."""
    if T <= 0:
        return round(max(0.0, (S - K) if opt == 'call' else (K - S)), 4)
    d1 = (_math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * _math.sqrt(T))
    d2 = d1 - sigma * _math.sqrt(T)
    if opt == 'call':
        price = S * norm_cdf(d1) - K * _math.exp(-r * T) * norm_cdf(d2)
    else:
        price = K * _math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
    return round(max(0.0, price), 4)


def bs_greeks(S, K, T, sigma, r=0.05, opt='call'):
    if T <= 0:
        delta = 1.0 if (opt == 'call' and S > K) else (-1.0 if (opt == 'put' and S < K) else 0.0)
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (_math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * _math.sqrt(T))
    d2 = d1 - sigma * _math.sqrt(T)
    delta = norm_cdf(d1) if opt == 'call' else norm_cdf(d1) - 1
    gamma = norm_pdf(d1) / (S * sigma * _math.sqrt(T))
    theta = (-(S * norm_pdf(d1) * sigma) / (2 * _math.sqrt(T))
             - r * K * _math.exp(-r * T) * (norm_cdf(d2) if opt == 'call' else norm_cdf(-d2))) / 365
    vega = S * norm_pdf(d1) * _math.sqrt(T) / 100
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
    }


def options_chain(symbol: str, spot: float):
    """Generate a paper options chain around the current spot price."""
    today = date.today()
    # next 4 weekly Fridays + 2 monthly expiries
    expiries = []
    d = today + timedelta(days=(4 - today.weekday()) % 7 + 1)
    for _ in range(4):
        expiries.append(d.isoformat())
        d += timedelta(weeks=1)
    # monthly: 3rd Friday of next 2 months
    for month_offset in range(1, 3):
        yr, mo = (today.year + (today.month + month_offset - 1) // 12,
                  (today.month + month_offset - 1) % 12 + 1)
        first = date(yr, mo, 1)
        first_fri = first + timedelta(days=(4 - first.weekday()) % 7)
        third_fri = first_fri + timedelta(weeks=2)
        if third_fri.isoformat() not in expiries:
            expiries.append(third_fri.isoformat())
    expiries.sort()

    # strikes: ±25% of spot in sensible increments
    step = 1 if spot < 20 else (5 if spot < 100 else (10 if spot < 500 else 25))
    low = round(spot * 0.75 / step) * step
    high = round(spot * 1.25 / step) * step
    strikes = []
    k = low
    while k <= high:
        strikes.append(round(k, 2))
        k += step

    chain = {}
    sigma = 0.30  # baseline IV 30%
    r = 0.05
    for exp in expiries:
        T = max(0.001, (_math.floor((date.fromisoformat(exp) - today).days / 365.0 * 1000)) / 1000)
        chain[exp] = []
        for strike in strikes:
            call_price = bs_price(spot, strike, T, sigma, r, 'call')
            put_price = bs_price(spot, strike, T, sigma, r, 'put')
            call_greeks = bs_greeks(spot, strike, T, sigma, r, 'call')
            put_greeks = bs_greeks(spot, strike, T, sigma, r, 'put')
            spread = round(call_price * 0.04, 2)
            chain[exp].append({
                "strike": strike,
                "atm": abs(strike - spot) < step,
                "call": {**call_greeks, "mid": call_price, "bid": round(call_price - spread, 2), "ask": round(call_price + spread, 2), "iv": sigma},
                "put": {**put_greeks, "mid": put_price, "bid": round(put_price - spread, 2), "ask": round(put_price + spread, 2), "iv": sigma},
            })
    return {"symbol": symbol, "spot": spot, "expiries": expiries, "chain": chain}
