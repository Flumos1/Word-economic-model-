"""Two-country, two-currency external-sector prototype for AMFE-17.

Physical goods are tracked in units, not added across currencies.
FX is an explicit two-legged exchange; no synthetic money creation.
This module is NOT yet linked to the four-sector SFC accounts.
"""
from decimal import Decimal
from dataclasses import dataclass
from collections import defaultdict

class ExternalAccountingError(ValueError):pass
def amount(x):
    if isinstance(x,bool):raise ExternalAccountingError("invalid amount")
    try:n=Decimal(str(x))
    except Exception as e:raise ExternalAccountingError("invalid amount") from e
    if not n.is_finite() or n<0:raise ExternalAccountingError("negative/nonfinite amount")
    return n

@dataclass(frozen=True)
class Country:
    name:str
    currency:str

class World:
    def __init__(self,countries):
        if len(countries)!=2 or len({c.name for c in countries})!=2 or len({c.currency for c in countries})!=2:
            raise ExternalAccountingError("two distinct countries and currencies required")
        self.countries={c.name:c for c in countries}
        self.money=defaultdict(lambda:Decimal(0))
        self.goods=defaultdict(lambda:Decimal(0))
        self.external=defaultdict(lambda:Decimal(0))
        self.journal=[]

    def endow(self,country,currency,cash=0,goods=0):
        if country not in self.countries or currency not in {c.currency for c in self.countries.values()}:
            raise ExternalAccountingError("unknown account")
        cash=amount(cash);goods=amount(goods)
        self.money[(country,currency)]+=cash
        self.goods[country]+=goods
        self.journal.append(("opening_endowment",country,currency,cash,goods))

    def exchange(self,first,second,sell_currency,sell_amount,buy_currency,buy_amount):
        if first==second or first not in self.countries or second not in self.countries or sell_currency==buy_currency:
            raise ExternalAccountingError("invalid FX parties")
        if {sell_currency,buy_currency}!={c.currency for c in self.countries.values()}:
            raise ExternalAccountingError("invalid FX currencies")
        x=amount(sell_amount);y=amount(buy_amount)
        if x==0 or y==0:raise ExternalAccountingError("zero FX leg")
        if self.money[(first,sell_currency)]<x or self.money[(second,buy_currency)]<y:
            raise ExternalAccountingError("insufficient FX liquidity")
        self.money[(first,sell_currency)]-=x;self.money[(second,sell_currency)]+=x
        self.money[(second,buy_currency)]-=y;self.money[(first,buy_currency)]+=y
        self.journal.append(("fx",first,second,sell_currency,x,buy_currency,y))

    def trade(self,exporter,importer,units,unit_price,invoice_currency):
        if exporter==importer or exporter not in self.countries or importer not in self.countries:
            raise ExternalAccountingError("invalid trade parties")
        if invoice_currency not in {c.currency for c in self.countries.values()}:
            raise ExternalAccountingError("invalid currency")
        q=amount(units);p=amount(unit_price);value=q*p
        if q==0 or p==0:raise ExternalAccountingError("zero trade")
        if self.goods[exporter]<q or self.money[(importer,invoice_currency)]<value:
            raise ExternalAccountingError("insufficient goods or funds")
        self.goods[exporter]-=q;self.goods[importer]+=q
        self.money[(importer,invoice_currency)]-=value
        self.money[(exporter,invoice_currency)]+=value
        self.external[(exporter,invoice_currency)]+=value
        self.external[(importer,invoice_currency)]-=value
        self.journal.append(("trade",exporter,importer,q,p,invoice_currency))

    def verify(self):
        currencies={c.currency for c in self.countries.values()}
        for currency in currencies:
            if sum((self.external[(country,currency)] for country in self.countries),Decimal(0))!=0:
                raise ExternalAccountingError("unbalanced cross-border current account")
        if any(v<0 for v in self.money.values()) or any(v<0 for v in self.goods.values()):
            raise ExternalAccountingError("negative balance")
        return True
