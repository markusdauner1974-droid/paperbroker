#
#    Pure-Python Black-Scholes model replacing the ivolat3 C-extension
#    (ivolat3 could not be built on Python 3.13; interface-compatible).
#
#    Conventions kept identical to the former ivolat3 usage in
#    logic/ivolat3_option_greeks.py:
#      - theta/charm/speed/zomma/color/ultima  -> per day  (value/365)
#      - veta                                  -> /(100*365)
#      - all other greeks as textbook values
#    Exotic greeks (vanna/charm/speed/zomma/color/veta/vomma/ultima)
#    are computed by central finite differences of the analytic base
#    greeks - no closed-form guesswork.
#
import math

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(s, k, r, q, t, sigma):
    vt = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / vt
    d2 = d1 - vt
    return d1, d2


def _price(s, k, r, q, t, sigma, is_call):
    d1, d2 = _d1_d2(s, k, r, q, t, sigma)
    if is_call:
        return s * math.exp(-q * t) * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return k * math.exp(-r * t) * _norm_cdf(-d2) - s * math.exp(-q * t) * _norm_cdf(-d1)


def _intrinsic(s, k, r, q, t, is_call):
    if is_call:
        return max(s * math.exp(-q * t) - k * math.exp(-r * t), 0.0)
    return max(k * math.exp(-r * t) - s * math.exp(-q * t), 0.0)


def ivolat_call(s, k, r, q, t, p):
    return _ivol(s, k, r, q, t, p, True)


def ivolat_put(s, k, r, q, t, p):
    return _ivol(s, k, r, q, t, p, False)


def _ivol(s, k, r, q, t, p, is_call):
    """Implied volatility via bisection; NaN when no solution exists."""
    if p is None or p <= 0.0 or t <= 0.0 or s <= 0.0 or k <= 0.0:
        return float('nan')
    lo = _intrinsic(s, k, r, q, t, is_call)
    if p <= lo * 1.0000001:
        return float('nan')  # at/below intrinsic - no sigma can fit
    lo_sigma, hi_sigma = 1e-6, 15.0
    flo = _price(s, k, r, q, t, lo_sigma, is_call) - p
    fhi = _price(s, k, r, q, t, hi_sigma, is_call) - p
    if flo > 0 or fhi < 0:
        return float('nan')
    for _ in range(100):
        mid = 0.5 * (lo_sigma + hi_sigma)
        fm = _price(s, k, r, q, t, mid, is_call) - p
        if abs(fm) < 1e-12:
            return mid
        if fm < 0:
            lo_sigma = mid
        else:
            hi_sigma = mid
    return 0.5 * (lo_sigma + hi_sigma)


def delta_call(s, k, r, q, t, sigma):
    if (sigma:=sigma) <= 0 or t <= 0:
        return float('nan')
    d1, _ = _d1_d2(s, k, r, q, t, sigma)
    return math.exp(-q * t) * _norm_cdf(d1)


def delta_put(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    d1, _ = _d1_d2(s, k, r, q, t, sigma)
    return math.exp(-q * t) * (_norm_cdf(d1) - 1.0)


def gamma(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    d1, _ = _d1_d2(s, k, r, q, t, sigma)
    return math.exp(-q * t) * _norm_pdf(d1) / (s * sigma * math.sqrt(t))


def vega(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    d1, _ = _d1_d2(s, k, r, q, t, sigma)
    return s * math.exp(-q * t) * _norm_pdf(d1) * math.sqrt(t)


def theta_call(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    d1, d2 = _d1_d2(s, k, r, q, t, sigma)
    return (-s * math.exp(-q * t) * _norm_pdf(d1) * sigma / (2 * math.sqrt(t))
            - r * k * math.exp(-r * t) * _norm_cdf(d2)
            + q * s * math.exp(-q * t) * _norm_cdf(d1))


def theta_put(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    d1, d2 = _d1_d2(s, k, r, q, t, sigma)
    return (-s * math.exp(-q * t) * _norm_pdf(d1) * sigma / (2 * math.sqrt(t))
            + r * k * math.exp(-r * t) * _norm_cdf(-d2)
            - q * s * math.exp(-q * t) * _norm_cdf(-d1))


def rho_call(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    _, d2 = _d1_d2(s, k, r, q, t, sigma)
    return k * t * math.exp(-r * t) * _norm_cdf(d2)


def rho_put(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    _, d2 = _d1_d2(s, k, r, q, t, sigma)
    return -k * t * math.exp(-r * t) * _norm_cdf(-d2)


def dualdelta_call(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    d1, d2 = _d1_d2(s, k, r, q, t, sigma)
    return -math.exp(-r * t) * _norm_cdf(d2)


def dualdelta_put(s, k, r, q, t, sigma):
    if sigma <= 0 or t <= 0:
        return float('nan')
    _, d2 = _d1_d2(s, k, r, q, t, sigma)
    return math.exp(-r * t) * _norm_cdf(-d2)


# --- exotic greeks via central finite differences -------------------------

_SD = 0.01   # relative spot shock for finite differences
_ST = 1.0 / 365.0  # one day for time differences
_SV = 0.001  # absolute vol shock


def vanna(s, k, r, q, t, sigma):
    h = max(s * _SD, _SD)
    up = vega((s + h), k, r, q, t, sigma)
    dn = vega(max(s - h, 1e-8), k, r, q, t, sigma)
    return (up - dn) / (2 * h)


def vomma(s, k, r, q, t, sigma):
    h = _SV
    return (vega(s, k, r, q, t, sigma + h) - vega(s, k, r, q, t, max(sigma - h, 1e-6))) / (2 * h)


def ultima(s, k, r, q, t, sigma):
    h = _SV
    up = vega(s, k, r, q, t, sigma + h)
    mid = vega(s, k, r, q, t, sigma)
    dn = vega(s, k, r, q, t, max(sigma - h, 1e-6))
    return (up - 2 * mid + dn) / (h * h)


def charm_call(s, k, r, q, t, sigma):
    return (delta_call(s, k, r, q, t, max(t - _ST, 1e-6)) - delta_call(s, k, r, q, t, t + _ST)) / (2 * _ST)


def charm_put(s, k, r, q, t, sigma):
    return (delta_put(s, k, r, q, t, max(t - _ST, 1e-6)) - delta_put(s, k, r, q, t, t + _ST)) / (2 * _ST)


def speed(s, k, r, q, t, sigma):
    h = max(s * _SD, _SD)
    return (gamma(s + h, k, r, q, t, sigma) - gamma(max(s - h, 1e-8), k, r, q, t, sigma)) / (2 * h)


def zomma(s, k, r, q, t, sigma):
    h = _SV
    return (gamma(s, k, r, q, t, sigma + h) - gamma(s, k, r, q, t, max(sigma - h, 1e-6))) / (2 * h)


def color(s, k, r, q, t, sigma):
    return (gamma(s, k, r, q, t, max(t - _ST, 1e-6)) - gamma(s, k, r, q, t, t + _ST)) / (2 * _ST)


def DvegaDtime(s, k, r, q, t, sigma):
    return (vega(s, k, r, q, t, max(t - _ST, 1e-6)) - vega(s, k, r, q, t, t + _ST)) / (2 * _ST)