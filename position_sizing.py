"""
position_sizing.py

Two sizing models.

calculate_position_size    - notional: "put X% of the account into this".
calculate_risk_based_size  - risk:     "lose at most X% of the account if the
                                        stop is hit".

The second is the one that actually controls risk, and it is what the daily
strategy uses. The first is kept because the futures and hourly scripts still
call it, and changing their sizing is a separate decision from adding a
correct model.

WHY THE NOTIONAL MODEL IS NOT RISK CONTROL
------------------------------------------
Notional sizing answers "how much am I holding", not "how much can I lose".
With a fixed 5% stop those two differ by a factor of twenty, and the factor
changes with every instrument's multiplier:

  SPY  @ 742,    mult 1  ->  1% of 1M = 10,000 notional =  13 shares
                             risk at a 5% stop = 13 x 37.1  =      482  (0.05% of NAV)
  DAX  @ 25,181, mult 25 ->  1% of 1M = 10,000 notional =   0 contracts
                             ...floored to 1 by min_qty  =  629,525  (63% of NAV)

So the same "1%" setting risks 0.05% on one instrument and 63% on another.
The min_qty=1 floor is what does the damage: it silently converts "you cannot
afford this position" into "buy one anyway". Risk-based sizing returns 0 in
that case, because refusing the trade is the correct answer.
"""


def calculate_position_size(net_liquidation, price, position_size_pct,
                            multiplier=1, min_qty=1, max_qty=None):
    """
    Notional sizing: a percentage of account value, expressed as quantity.

    net_liquidation: total account value
    price: current price of the instrument
    position_size_pct: fraction of account to allocate (0.01 = 1%)
    multiplier: contract multiplier for futures (1 for stocks)
    min_qty: minimum quantity floor
    max_qty: optional hard cap

    NOTE: min_qty defaults to 1, which means this function can return a
    position larger than the requested allocation. See the module docstring.
    Prefer calculate_risk_based_size for anything that has a stop.
    """
    if price <= 0 or net_liquidation <= 0:
        return min_qty

    allocation = net_liquidation * position_size_pct
    contract_value = price * multiplier
    quantity = int(allocation / contract_value)

    if quantity < min_qty:
        quantity = min_qty

    if max_qty is not None and quantity > max_qty:
        quantity = max_qty

    return quantity


def calculate_risk_based_size(net_liquidation, entry_price, stop_price,
                              risk_pct=0.01, multiplier=1,
                              max_notional_pct=0.20, max_qty=None):
    """
    Size so that a stop-out costs a fixed fraction of the account.

    net_liquidation:  total account value
    entry_price:      price the position is opened at
    stop_price:       price the stop sits at (below entry for a long)
    risk_pct:         fraction of the account to put at risk (0.01 = 1%)
    multiplier:       contract multiplier (1 for stocks)
    max_notional_pct: independent ceiling on gross exposure, as a fraction of
                      the account. A tight stop makes the risk model ask for a
                      very large position -- correct on risk, reckless on
                      concentration and margin. Both limits apply; the smaller
                      wins.
    max_qty:          optional hard cap

    Returns an integer quantity, and RETURNS 0 when the account cannot afford
    even one unit within the risk budget. Zero means "do not take this trade".
    Callers must treat 0 as a skip, not as a rounding artefact.

    Worked example -- 1M account, 1% risk, SPY at 742 with a 5% stop:
        stop distance   = 742 - 704.90        =  37.10
        risk per share  = 37.10 x 1           =  37.10
        risk budget     = 1,000,000 x 0.01    =  10,000
        by risk         = 10,000 / 37.10      =  269 shares
        notional        = 269 x 742           =  199,598  (20.0% of NAV)
        notional cap    = 1,000,000 x 0.20    =  200,000  -> 269 allowed
        result          = 269 shares, risking 9,979 if stopped out
    """
    if net_liquidation <= 0 or entry_price <= 0 or multiplier <= 0:
        return 0

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        # No stop, or a stop at the entry price. There is no risk denominator
        # to size against, so this model cannot answer. Refuse rather than
        # fall back to notional sizing, which would hide the missing stop.
        return 0

    risk_per_unit = stop_distance * multiplier
    if risk_per_unit <= 0:
        return 0

    risk_budget = net_liquidation * risk_pct
    qty_by_risk = int(risk_budget / risk_per_unit)

    unit_notional = entry_price * multiplier
    notional_budget = net_liquidation * max_notional_pct
    qty_by_notional = int(notional_budget / unit_notional) if unit_notional > 0 else 0

    quantity = min(qty_by_risk, qty_by_notional)

    if max_qty is not None:
        quantity = min(quantity, max_qty)

    return max(0, quantity)


def describe_size(net_liquidation, entry_price, stop_price, quantity,
                  multiplier=1):
    """
    What a given size actually costs if the stop is hit.

    Used for the log line and the summary email, so every entry states its own
    risk in currency and in percent rather than only a share count. An entry
    that cannot say what it risks is the thing this module exists to prevent.
    """
    stop_distance = abs(entry_price - stop_price)
    risk_amount = stop_distance * multiplier * quantity
    notional = entry_price * multiplier * quantity

    return {
        "quantity": quantity,
        "risk_amount": round(risk_amount, 2),
        "risk_pct": round(risk_amount / net_liquidation * 100, 3) if net_liquidation > 0 else 0.0,
        "notional": round(notional, 2),
        "notional_pct": round(notional / net_liquidation * 100, 2) if net_liquidation > 0 else 0.0,
        "stop_distance": round(stop_distance, 4),
    }
