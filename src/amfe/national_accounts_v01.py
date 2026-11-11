"""Two-country SNA-inspired flow reconciliation, independent of financial ledger.

Scope: simplified closed domestic production and cross-border goods trade.
All inputs must use a common valuation unit and the same accounting period.
This is not complete SNA 2025 or BPM7.
"""
from dataclasses import dataclass
from amfe.accounting_core import D, AccountingError

@dataclass(frozen=True)
class Flow:
    country: str
    output: object = 0
    intermediate: object = 0
    compensation: object = 0
    operating_surplus: object = 0
    taxes_less_subsidies: object = 0
    household_consumption: object = 0
    government_consumption: object = 0
    fixed_investment: object = 0
    inventory_change: object = 0
    exports: object = 0
    imports: object = 0

FIELDS = tuple(k for k in Flow.__dataclass_fields__ if k != "country")

def reconcile(flows):
    """Return GDP by three methods, refusing any inconsistent account.

    Input/output and trade are gross values; imported intermediates are
    included in intermediate consumption. No hidden statistical discrepancy.
    """
    if not flows:
        raise AccountingError("missing flow observations")
    result = {}
    for row in flows:
        if row.country not in ("A", "B") or row.country in result:
            raise AccountingError("invalid or duplicate country")
        x = {name:D(getattr(row,name)) for name in FIELDS}
        if any(x[k] < 0 for k in FIELDS if k != "inventory_change"):
            raise AccountingError("negative non-inventory flow")
        if x["intermediate"] > x["output"]:
            raise AccountingError("intermediate exceeds output")
        production = x["output"] - x["intermediate"]
        income = x["compensation"] + x["operating_surplus"] + x["taxes_less_subsidies"]
        expenditure = (x["household_consumption"] + x["government_consumption"]
                       + x["fixed_investment"] + x["inventory_change"]
                       + x["exports"] - x["imports"])
        if production != income or production != expenditure:
            raise AccountingError("GDP production/income/expenditure mismatch: " + row.country)
        result[row.country] = {"production":production,"income":income,
                               "expenditure":expenditure,"net_exports":x["exports"]-x["imports"]}
    if set(result) != {"A","B"}:
        raise AccountingError("both countries required")
    if result["A"]["net_exports"] + result["B"]["net_exports"] != 0:
        raise AccountingError("world goods trade does not mirror")
    return result
