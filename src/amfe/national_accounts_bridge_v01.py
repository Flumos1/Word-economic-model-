"""Bridge between posted international trade journal and national-account flows.

The bridge NEVER infers production, wages or GDP from a cash transfer.
Only invoice values of recorded cross-border goods transfers are extracted.
Invoices must share a reporting currency; conversion/valuation is not implicit.
"""
from dataclasses import replace
from amfe.accounting_core import D, AccountingError
from amfe.national_accounts_v01 import Flow, reconcile

def posted_trade_values(world, period, reporting_currency):
    if reporting_currency not in ("AAA","BBB"):
        raise AccountingError("unsupported reporting currency")
    exports={"A":D(0),"B":D(0)}
    imports={"A":D(0),"B":D(0)}
    count=0
    for name,kind,financial,goods,capital,current in world.journal:
        if name!=period or kind!="cross_border_trade":
            continue
        if capital:
            raise AccountingError("trade event unexpectedly changes capital")
        exporters=[country for country,quantity in goods.items() if D(quantity)<0]
        importers=[country for country,quantity in goods.items() if D(quantity)>0]
        if len(exporters)!=1 or len(importers)!=1:
            raise AccountingError("invalid physical trade direction")
        exporter,importer=exporters[0],importers[0]
        if D(goods[exporter])!=-D(goods[importer]):
            raise AccountingError("goods mirror mismatch")
        keys=[(c,s,i,cur) for c,s,i,cur in financial if D(financial[(c,s,i,cur)])!=0]
        if not keys or any(i!="deposit" or cur!=reporting_currency for c,s,i,cur in keys):
            raise AccountingError("invoice requires explicit FX conversion")
        amount=D(financial.get((exporter,"firm","deposit",reporting_currency),0))
        if amount<=0 or D(financial.get((importer,"firm","deposit",reporting_currency),0))!=-amount:
            raise AccountingError("invoice counterpart mismatch")
        if D(financial.get((exporter,"bank","deposit",reporting_currency),0))!=-amount or D(financial.get((importer,"bank","deposit",reporting_currency),0))!=amount:
            raise AccountingError("bank settlement mismatch")
        if len(keys)!=4:
            raise AccountingError("unexpected trade postings")
        exports[exporter]+=amount
        imports[importer]+=amount
        count+=1
    return {"exports":exports,"imports":imports,"count":count}

def reconcile_posted_trade(world,period,reporting_currency,domestic_flows):
    """Check GDP three ways AND require trade to match actual posted events.

    Domestic production/income/consumption values remain explicit inputs until
    the domestic event ledger is integrated. This function does not certify
    that domestic values originated in posted operations.
    """
    if not any(name==period for name,*_ in world.journal):
        raise AccountingError("period has no journal events")
    posted=posted_trade_values(world,period,reporting_currency)
    flows=[]
    for row in domestic_flows:
        if row.country not in ("A","B"):
            raise AccountingError("invalid country")
        if D(row.exports)!=posted["exports"][row.country] or D(row.imports)!=posted["imports"][row.country]:
            raise AccountingError("national accounts trade differs from posted ledger")
        flows.append(row)
    return {"accounts":reconcile(flows),"trade_events":posted["count"],"period":period,
            "reporting_currency":reporting_currency}
