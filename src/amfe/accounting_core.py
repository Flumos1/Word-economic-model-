"""Scoped four-sector stock-flow accounting kernel (not an empirical model)."""
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

SECTORS=("household","firm","bank","government")
INSTRUMENTS={"deposit":("household","firm","government","bank"),"loan":("bank","firm"),"bond":("bank","government")}
REAL_ASSETS=("inventory","fixed_capital")
KINDS=frozenset(("loan","repayment","bond_issue","bond_repayment","wages","transfer","levy","interest","dividend","production","household_purchase","government_purchase","investment","depreciation"))

class AccountingError(ValueError): pass
def D(x):
    if isinstance(x,bool): raise AccountingError("boolean amount")
    try: v=Decimal(str(x))
    except Exception as exc: raise AccountingError("invalid amount") from exc
    if not v.is_finite(): raise AccountingError("nonfinite amount")
    return v

@dataclass(frozen=True)
class Transaction:
    kind: str
    current: dict=field(default_factory=dict)
    real: dict=field(default_factory=dict)
    financial: dict=field(default_factory=dict)

class Accounts:
    def __init__(self):
        self.financial={};self.real={};self.events=[];self.periods=[]
        self._opening=None;self._start=0;self.begin("P1")
    def net_financial(self,s):
        return sum((v for (t,_),v in self.financial.items() if t==s),D(0))
    def real_value(self,s):
        return sum((v for (t,_),v in self.real.items() if t==s),D(0))
    def net_worth(self,s):return self.net_financial(s)+self.real_value(s)
    def _check_positions(self,f,r):
        for i in INSTRUMENTS:
            if sum((v for (s,j),v in f.items() if j==i),D(0))!=0:raise AccountingError("instrument mismatch: "+i)
        for (s,i),v in f.items():
            if s not in SECTORS or i not in INSTRUMENTS or s not in INSTRUMENTS[i]:raise AccountingError("invalid financial account")
            if i=="deposit" and ((s=="bank" and v>0) or (s!="bank" and v<0)):raise AccountingError("deposit sign")
            if i=="loan" and ((s=="bank" and v<0) or (s=="firm" and v>0)):raise AccountingError("loan sign")
            if i=="bond" and ((s=="bank" and v<0) or (s=="government" and v>0)):raise AccountingError("bond sign")
        for (s,a),v in r.items():
            if s not in SECTORS or a not in REAL_ASSETS or v<0:raise AccountingError("invalid real asset")
    def apply(self,tx):
        if tx.kind not in KINDS:raise AccountingError("unsupported transaction kind")
        c={s:D(v) for s,v in tx.current.items()}
        r={k:D(v) for k,v in tx.real.items()}
        f={k:D(v) for k,v in tx.financial.items()}
        if any(s not in SECTORS for s in c):raise AccountingError("unknown sector")
        if any(not isinstance(k,tuple) or len(k)!=2 for k in (*r,*f)):raise AccountingError("invalid key")
        if any(s not in SECTORS or a not in REAL_ASSETS for s,a in r):raise AccountingError("unknown real account")
        if any(s not in SECTORS or i not in INSTRUMENTS or s not in INSTRUMENTS[i] for s,i in f):raise AccountingError("unknown financial account")
        if sum(f.values(),D(0))!=0:raise AccountingError("financial mismatch")
        for i in INSTRUMENTS:
            if sum((v for (s,j),v in f.items() if j==i),D(0))!=0:raise AccountingError("instrument mismatch")
        for s in SECTORS:
            if c.get(s,D(0))!=sum((v for (t,_),v in r.items() if t==s),D(0))+sum((v for (t,_),v in f.items() if t==s),D(0)):
                raise AccountingError("sector current-capital-financial mismatch: "+s)
        nf=self.financial.copy();nr=self.real.copy()
        for k,v in f.items():nf[k]=nf.get(k,D(0))+v
        for k,v in r.items():nr[k]=nr.get(k,D(0))+v
        self._check_positions(nf,nr)
        self.financial=nf;self.real=nr;self.events.append((self.period,tx.kind,c,r,f))
    def begin(self,name):
        if self._opening is not None:self.close()
        self.period=name;self._start=len(self.events)
        self._opening={s:self.net_worth(s) for s in SECTORS}
    def close(self):
        if self._opening is None:raise AccountingError("no open period")
        cur={s:D(0) for s in SECTORS};cap=cur.copy();fin=cur.copy();matrix=[]
        for _,kind,c,r,f in self.events[self._start:]:
            row={"kind":kind,"current":{},"capital":{},"financial":{}}
            for s in SECTORS:
                cv=c.get(s,D(0));kv=sum((v for (t,_),v in r.items() if t==s),D(0));fv=sum((v for (t,_),v in f.items() if t==s),D(0))
                cur[s]+=cv;cap[s]+=kv;fin[s]+=fv
                row["current"][s]=cv;row["capital"][s]=kv;row["financial"][s]=fv
            matrix.append(row)
        closing={s:self.net_worth(s) for s in SECTORS}
        for s in SECTORS:
            if closing[s]-self._opening[s]!=cur[s] or cur[s]!=cap[s]+fin[s]:raise AccountingError("period bridge")
        if sum(fin.values(),D(0))!=0 or sum(cur.values(),D(0))!=sum(cap.values(),D(0)):raise AccountingError("aggregate bridge")
        result={"period":self.period,"opening":self._opening.copy(),"closing":closing,"current":cur,"capital":cap,"financial":fin,"matrix":matrix}
        self.periods.append(result);self._opening=None
        return result

def cash_transfer(kind,payer,receiver,amount):
    n=D(amount)
    if kind not in {"wages","transfer","levy","dividend"} or n<0 or payer==receiver or payer not in SECTORS or receiver not in SECTORS or "bank" in (payer,receiver):raise AccountingError("invalid transfer")
    return Transaction(kind,{payer:-n,receiver:n},{},{(payer,"deposit"):-n,(receiver,"deposit"):n})
def credit(amount,repayment=False):
    n=D(amount)
    if n<0:raise AccountingError("negative credit")
    k=-n if repayment else n
    return Transaction("repayment" if repayment else "loan",{},{},{("bank","loan"):k,("firm","loan"):-k,("firm","deposit"):k,("bank","deposit"):-k})
def government_bond(amount,repayment=False):
    n=D(amount)
    if n<0:raise AccountingError("negative bond")
    k=-n if repayment else n
    return Transaction("bond_repayment" if repayment else "bond_issue",{},{},{("bank","bond"):k,("government","bond"):-k,("government","deposit"):k,("bank","deposit"):-k})
def produce(amount):
    n=D(amount)
    if n<0:raise AccountingError("negative production")
    return Transaction("production",{"firm":n},{("firm","inventory"):n},{})
def purchase(buyer,amount):
    n=D(amount)
    if n<0 or buyer not in ("household","government"):raise AccountingError("invalid buyer")
    return Transaction("household_purchase" if buyer=="household" else "government_purchase",{buyer:-n},{("firm","inventory"):-n},{(buyer,"deposit"):-n,("firm","deposit"):n})
def interest(amount):
    n=D(amount)
    if n<0:raise AccountingError("negative interest")
    return Transaction("interest",{"firm":-n,"bank":n},{},{("firm","deposit"):-n,("bank","deposit"):n})
def invest(amount):
    n=D(amount)
    if n<0:raise AccountingError("negative investment")
    return Transaction("investment",{},{("firm","inventory"):-n,("firm","fixed_capital"):n},{})
def depreciate(amount):
    n=D(amount)
    if n<0:raise AccountingError("negative depreciation")
    return Transaction("depreciation",{"firm":-n},{("firm","fixed_capital"):-n},{})
