"""AMFE international SFC bridge: two economies, four sectors each, two currencies.

Consolidated ledger, instrument-by-instrument mirror and period net-worth bridge.
Opening endowments are explicitly nontransactional initial conditions.
No claim of SNA/BPM6 completeness or FX valuation adequacy.
"""
from collections import defaultdict
from decimal import Decimal
from amfe.accounting_core import D, AccountingError

COUNTRIES=("A","B")
SECTORS=("household","firm","bank","government")
CURRENCIES={"A":"AAA","B":"BBB"}

class InternationalAccounts:
    def __init__(self):
        self.financial=defaultdict(lambda:D(0))
        self.goods=defaultdict(lambda:D(0))
        self.capital=defaultdict(lambda:D(0))
        self.journal=[]
        self.periods=[]
        self._opening=None
        self.begin("P1")

    def nw(self,c,s):
        return sum((v for (cc,ss,_,_),v in self.financial.items() if (cc,ss)==(c,s)),D(0))+(self.goods[c]+self.capital[c] if s=="firm" else D(0))

    def _check(self,f,g,k):
        for (c,s,inst,cur),v in f.items():
            if c not in COUNTRIES or s not in SECTORS or cur not in CURRENCIES.values() or inst not in ("deposit","loan","bond"):
                raise AccountingError("unknown instrument")
            if inst=="deposit" and ((s=="bank" and v>0) or (s!="bank" and v<0)):
                raise AccountingError("deposit sign")
            if inst=="loan" and s not in ("firm","bank"):raise AccountingError("loan party")
            if inst=="bond" and s not in ("government","bank"):raise AccountingError("bond party")
        for inst in ("deposit","loan","bond"):
            for cur in CURRENCIES.values():
                if sum((v for (_,_,i,cc),v in f.items() if (i,cc)==(inst,cur)),D(0))!=0:
                    raise AccountingError("unmirrored instrument")
        if any(v<0 for v in g.values()) or any(v<0 for v in k.values()):
            raise AccountingError("negative goods/capital")

    def post(self,kind,financial=None,goods=None,capital=None,current=None):
        if self._opening is None:raise AccountingError("period closed")
        financial=financial or {};goods=goods or {};capital=capital or {};current=current or {}
        f={key:D(v) for key,v in financial.items()}
        g={key:D(v) for key,v in goods.items()}
        k={key:D(v) for key,v in capital.items()}
        c={key:D(v) for key,v in current.items()}
        if any(key not in COUNTRIES for key in (*g,*k)):raise AccountingError("invalid country")
        if any(key not in [(cc,ss) for cc in COUNTRIES for ss in SECTORS] for key in c):
            raise AccountingError("invalid sector")
        nf=self.financial.copy();ng=self.goods.copy();nk=self.capital.copy()
        for key,v in f.items():nf[key]+=v
        for key,v in g.items():ng[key]+=v
        for key,v in k.items():nk[key]+=v
        self._check(nf,ng,nk)
        for cc in COUNTRIES:
            for ss in SECTORS:
                df=sum((v for (x,y,_,_),v in f.items() if (x,y)==(cc,ss)),D(0))
                dr=(g.get(cc,D(0))+k.get(cc,D(0))) if ss=="firm" else D(0)
                if c.get((cc,ss),D(0))!=df+dr:
                    raise AccountingError("sector bridge")
        self.financial=nf;self.goods=ng;self.capital=nk
        self.journal.append((self.period,kind,f,g,k,c))

    def endow(self,c,currency,amount,goods=0):
        if self.journal or self.periods:
            raise AccountingError("endowment only before first operation")
        if c not in COUNTRIES or currency!=CURRENCIES[c]:raise AccountingError("invalid endowment")
        n=D(amount);q=D(goods)
        if n<0 or q<0:raise AccountingError("negative endowment")
        self.financial[(c,"firm","deposit",currency)]+=n
        self.financial[(c,"bank","deposit",currency)]-=n
        self.goods[c]+=q
        self._opening={(cc,ss):self.nw(cc,ss) for cc in COUNTRIES for ss in SECTORS}

    def transfer(self,c,payer,receiver,n,currency=None):
        n=D(n);cur=currency or CURRENCIES[c]
        if c not in COUNTRIES or payer==receiver or payer not in ("firm","household","government") or receiver not in ("firm","household","government") or n<0:
            raise AccountingError("invalid transfer")
        self.post("transfer",{(c,payer,"deposit",cur):-n,(c,receiver,"deposit",cur):n},current={(c,payer):-n,(c,receiver):n})

    def trade(self,exporter,importer,quantity,unit_price,currency):
        q=D(quantity);p=D(unit_price)
        if exporter==importer or exporter not in COUNTRIES or importer not in COUNTRIES or currency not in CURRENCIES.values() or q<=0 or p<=0:
            raise AccountingError("invalid trade")
        n=q*p
        # Importer household buys inventory from exporter firm; payment is in
        # the invoice currency. Bank liability migrates to exporter's bank.
        f={(importer,"household","deposit",currency):-n,
           (importer,"bank","deposit",currency):n,
           (exporter,"firm","deposit",currency):n,
           (exporter,"bank","deposit",currency):-n}
        g={exporter:-q,importer:q}
        # Goods imported become inventory of the importing firm. Household
        # purchase is a separate transaction; trade itself is asset exchange.
        # The exporter has sold inventory q at carrying value q, and gains
        # invoice proceeds n; importer acquires inventory q for n.
        # At price != 1, explicit trading gains/losses appear in current flows.
        current={(exporter,"firm"):n-q,(importer,"firm"):q,(importer,"household"):-n}
        self.post("cross_border_trade",f,g,current=current)

    def exchange(self,c1,c2,cur1,n1,cur2,n2):
        x=D(n1);y=D(n2)
        if c1==c2 or c1 not in COUNTRIES or c2 not in COUNTRIES or cur1==cur2 or {cur1,cur2}!=set(CURRENCIES.values()) or x<=0 or y<=0:
            raise AccountingError("invalid FX")
        # FX transfer between firms: asset change per currency; value
        # difference is intentionally disallowed without valuation model.
        if x!=y:raise AccountingError("FX valuation required for unequal legs")
        f={(c1,"firm","deposit",cur1):-x,(c1,"bank","deposit",cur1):x,
           (c2,"firm","deposit",cur1):x,(c2,"bank","deposit",cur1):-x,
           (c2,"firm","deposit",cur2):-y,(c2,"bank","deposit",cur2):y,
           (c1,"firm","deposit",cur2):y,(c1,"bank","deposit",cur2):-y}
        self.post("fx",f)

    def begin(self,name):
        if self._opening is not None:self.close()
        self.period=name;self._start=len(self.journal)
        self._opening={(c,s):self.nw(c,s) for c in COUNTRIES for s in SECTORS}

    def close(self):
        if self._opening is None:raise AccountingError("period closed")
        current=defaultdict(lambda:D(0));rows=[]
        for _,kind,f,g,k,c in self.journal[self._start:]:
            rows.append({"kind":kind,"financial":f,"goods":g,"capital":k,"current":c})
            for key,v in c.items():current[key]+=v
        closing={(c,s):self.nw(c,s) for c in COUNTRIES for s in SECTORS}
        for key in closing:
            if closing[key]-self._opening[key]!=current[key]:
                raise AccountingError("period reconciliation")
        self._check(self.financial,self.goods,self.capital)
        result={"period":self.period,"opening":self._opening.copy(),"closing":closing,
                "current":dict(current),"matrix":rows}
        self.periods.append(result);self._opening=None
        return result
