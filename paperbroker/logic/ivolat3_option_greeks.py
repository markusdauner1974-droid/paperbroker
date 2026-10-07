###############################
#
# Greeks for european-style options
#
###############################

import math

from . import bs_model

def get_option_greeks(option_type, strike, underlying_price, days_to_expiration, price, dividend = 0.0):

    out = {
        "iv": None,
        "delta": None,
        "vega": None,
        "theta": None,
        "rho": None,
        "gamma": None,
        "vanna": None,
        "charm": None,
        "speed": None,
        "zomma": None,
        "color": None,
        "veta": None,
        "vomma": None,
        "ultima": None,
        "dual_delta": None,
    }

    if option_type is None or (option_type != 'put' and option_type != 'call') : return out
    if strike is None: return out
    if underlying_price is None: return out
    if days_to_expiration is None or days_to_expiration <= 0: return out
    if price is None: return out
    if dividend is None: dividend = 0.0

    # stock price
    s = underlying_price
    # option strike price
    k = strike
    # risk-free interest rate
    r = days_to_expiration / 365 * 0.02 # (2% treasury rate)
    # drift rate (dividend)
    q = dividend
    # time remaining until expiration (in years)
    t = days_to_expiration / 365.0
    # call option price
    p = price

    # annual volatility of stock price
    # sigma = 0.2

    # call opition implied volatility
    sigma = bs_model.ivolat_call(s, k, r, q, t, p) if option_type == 'call' else bs_model.ivolat_put(s, k, r, q, t, p)

    if sigma != sigma:
        # means sigma is not a number
        # but everything else needs it so bail
        #we were unable to calculate implied volatility which is the base of all the other calculations
        # no iv means no greeks
        return out

    out['iv'] = sigma

    out['delta'] = bs_model.delta_call(s, k, r, q, t, sigma) if \
        option_type == 'call' else bs_model.delta_put(s, k, r, q, t, sigma)

    out['vega'] = bs_model.vega(s, k, r, q, t, sigma)

    out['theta'] = (bs_model.theta_call(s, k, r, q, t, sigma) if
                    option_type == 'call' else bs_model.theta_put(s, k, r, q, t, sigma)) / 365

    out['rho'] = bs_model.rho_call(s, k, r, q, t, sigma) if \
        option_type == 'call' else bs_model.rho_put(s, k, r, q, t, sigma)

    out['gamma'] = bs_model.gamma(s, k, r, q, t, sigma)
    out['vanna'] = bs_model.vanna(s, k, r, q, t, sigma)
    out['charm'] = (bs_model.charm_call(s, k, r, q, t, sigma) if \
        option_type == 'call' else bs_model.charm_put(s, k, r, q, t, sigma)) / 365
    out['speed'] = bs_model.speed(s, k, r, q, t, sigma) / 365
    out['zomma'] = bs_model.zomma(s, k, r, q, t, sigma) / 365
    out['color'] = bs_model.color(s, k, r, q, t, sigma) / (365)
    out['veta'] = bs_model.DvegaDtime(s, k, r, q, t, sigma) / (100 * 365)
    out['vomma'] = bs_model.vomma(s, k, r, q, t, sigma)
    out['ultima'] = bs_model.ultima(s, k, r, q, t, sigma) / 365

    out['dual_delta'] = bs_model.dualdelta_call(s, k, r, q, t, sigma) if option_type == 'call' else bs_model.dualdelta_put(
        s, k, r, q, t, sigma)

    return out

