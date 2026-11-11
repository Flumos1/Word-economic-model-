"""Deterministic randomized scenarios for accounting regressions.

Amounts are integer cents, so every identity holds exactly in Decimal.
Each scenario carries its index in the GDP level, hence all scenarios are
distinct by construction (tests assert this instead of trusting a loop count).
"""
from decimal import Decimal
from amfe.national_accounts_v01 import Flow, FIELDS


def _split(rng, total, parts):
    cuts = sorted(rng.randint(0, total) for _ in range(parts - 1))
    edges = [0, *cuts, total]
    return [edges[k + 1] - edges[k] for k in range(parts)]


def country_flow(rng, name, index, exports, imports):
    """One internally consistent country account (cents / 100)."""
    gdp = 100000 + index
    intermediate = rng.randint(0, 200000)
    compensation, surplus, tax = _split(rng, gdp, 3)
    inventory = rng.randint(-5000, 5000)
    consumption, government, investment = _split(rng, gdp - (exports - imports) - inventory, 3)
    cents = {"output": gdp + intermediate, "intermediate": intermediate,
             "compensation": compensation, "operating_surplus": surplus,
             "taxes_less_subsidies": tax, "household_consumption": consumption,
             "government_consumption": government, "fixed_investment": investment,
             "inventory_change": inventory, "exports": exports, "imports": imports}
    return Flow(name, **{k: Decimal(v) / 100 for k, v in cents.items()})


def world_flows(rng, index, exports_a, exports_b):
    """A exports exports_a (= B imports); B exports exports_b (= A imports)."""
    return (country_flow(rng, "A", index, exports_a, exports_b),
            country_flow(rng, "B", index, exports_b, exports_a))


def trade_world(rng):
    """Two funded countries and one posted cross-border trade.

    Returns (world, exporter, invoice currency, trade value in cents).
    """
    from amfe.integrated_world_v01 import InternationalAccounts
    w = InternationalAccounts()
    w.endow("A", "AAA", 100000, goods=1000)
    w.endow("B", "BBB", 100000, goods=1000)
    w.exchange("A", "B", "AAA", 5000, "BBB", 5000)
    exporter = rng.choice("AB")
    importer = "B" if exporter == "A" else "A"
    currency = rng.choice(("AAA", "BBB"))
    quantity, price_cents = rng.randint(1, 20), rng.randint(1, 2500)
    w.trade(exporter, importer, quantity, Decimal(price_cents) / 100, currency)
    return w, exporter, currency, quantity * price_cents


def flow_key(*flows):
    return tuple(getattr(f, name) for f in flows for name in FIELDS)


def perturb(rng, flow):
    """Break exactly one field of a consistent account by a nonzero amount."""
    name = rng.choice(FIELDS)
    delta = Decimal(rng.choice((-1, 1)) * rng.randint(1, 5000)) / 100
    values = {n: getattr(flow, n) for n in FIELDS}
    values[name] = values[name] + delta
    return Flow(flow.country, **values), name
