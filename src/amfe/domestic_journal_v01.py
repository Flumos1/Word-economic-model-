"""Typed domestic activity journal; derive all three GDP measures from posted events.

This is an economic flow subledger, NOT a financial double-entry ledger.
Cross-border trade is sourced exclusively from InternationalAccounts.
All amounts are valued in a common reporting unit; no implicit FX conversion.
"""
from collections import defaultdict
from dataclasses import dataclass
from amfe.accounting_core import D, AccountingError
from amfe.national_accounts_v01 import Flow,reconcile
from amfe.national_accounts_bridge_v01 import posted_trade_values

TYPES = {
    "output":"output", "intermediate":"intermediate",
    "wage":"compensation", "operating_surplus":"operating_surplus",
    "production_tax_net":"taxes_less_subsidies",
    "household_consumption":"household_consumption",
    "government_consumption":"government_consumption",
    "fixed_investment":"fixed_investment",
    "inventory_change":"inventory_change"
}

@dataclass(frozen=True)
class EconomicEvent:
    period: str
    country: str
    kind: str
    amount: object

class DomesticJournal:
    def __init__(self):
        self.events=[]
    def post(self,period,country,kind,amount):
        if not isinstance(period,str) or not period.strip():
            raise AccountingError("invalid period")
        if country not in ("A","B") or kind not in TYPES:
            raise AccountingError("unknown domestic event")
        value=D(amount)
        if value<0 and kind!="inventory_change":
            raise AccountingError("negative domestic event")
        self.events.append(EconomicEvent(period,country,kind,value))
    def flows(self,period,trade):
        sums=defaultdict(lambda:D(0))
        for event in self.events:
            if event.period==period:
                sums[(event.country,TYPES[event.kind])]+=event.amount
        rows=[]
        for country in ("A","B"):
            values={field:sums[(country,field)] for field in TYPES.values()}
            rows.append(Flow(country=country,**values,
                             exports=trade["exports"][country],
                             imports=trade["imports"][country]))
        return rows

def reconcile_journals(domestic,world,period,reporting_currency):
    if not isinstance(domestic,DomesticJournal):
        raise AccountingError("invalid domestic journal")
    if not any(e.period==period for e in domestic.events):
        raise AccountingError("no domestic events for period")
    trade=posted_trade_values(world,period,reporting_currency)
    rows=domestic.flows(period,trade)
    accounts=reconcile(rows)
    return {"period":period,"reporting_currency":reporting_currency,
            "trade_events":trade["count"],"accounts":accounts,
            "domestic_events":sum(e.period==period for e in domestic.events)}
