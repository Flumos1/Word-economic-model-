"""Extended accounting kernel: accruals, revaluations, and other volume changes.

Uses the v06 Accounts ledger. The extension is deliberately restricted to
firm-household receivables, fixed capital, and a single nominal unit.
"""
from amfe.accounting_core import Accounts, AccountingError, D, SECTORS, Transaction

class ExtendedAccounts(Accounts):
    def __init__(self):
        self.revaluations=[]
        self.other_changes=[]
        self._rv_start=0
        self._oc_start=0
        super().__init__()

    def _check_positions(self, f, r):
        super()._check_positions({k:v for k,v in f.items() if k[1]!="receivable"},r)
        if f.get(("firm","receivable"),D(0)) + f.get(("household","receivable"),D(0)) != 0:
            raise AccountingError("receivable mismatch")
        if f.get(("firm","receivable"),D(0))<0 or f.get(("household","receivable"),D(0))>0:
            raise AccountingError("receivable sign")

    def apply(self,tx):
        if self._opening is None: raise AccountingError("period closed")
        # The base ledger validates known instruments; receivables require a
        # separate, equally strict atomic posting route.
        if tx.kind not in {"accrued_wages","settle_wages"}:
            return super().apply(tx)
        c={s:D(v) for s,v in tx.current.items()}
        f={k:D(v) for k,v in tx.financial.items()}
        if tx.real or any(k not in {("firm","receivable"),("household","receivable"),("firm","deposit"),("household","deposit"),("bank","deposit")} for k in f):
            raise AccountingError("invalid accrual accounts")
        if sum(f.values(),D(0))!=0:raise AccountingError("unbalanced accrual")
        for instrument in ("receivable","deposit"):
            if sum((v for (s,i),v in f.items() if i==instrument),D(0))!=0:
                raise AccountingError("instrument mismatch")
        for s in SECTORS:
            if c.get(s,D(0))!=sum((v for (t,_),v in f.items() if t==s),D(0)):
                raise AccountingError("sector mismatch")
        nf=self.financial.copy()
        for k,v in f.items():nf[k]=nf.get(k,D(0))+v
        self._check_positions(nf,self.real)
        self.financial=nf
        self.events.append((self.period,tx.kind,c,{},f))

    def accrue_wages(self,n):
        n=D(n)
        if n<0:raise AccountingError("negative accrual")
        self.apply(Transaction("accrued_wages",{"firm":-n,"household":n},{},
            {("firm","receivable"):-n,("household","receivable"):n}))

    def settle_wages(self,n):
        n=D(n)
        if n<0:raise AccountingError("negative settlement")
        self.apply(Transaction("settle_wages",{}, {},
            {("firm","receivable"):n,("household","receivable"):-n,
             ("firm","deposit"):-n,("household","deposit"):n}))

    def _nontransaction_change(self,kind,sector,asset,amount,category):
        if self._opening is None: raise AccountingError("period closed")
        n=D(amount)
        if category not in ("revaluation","other_volume") or kind not in ("fixed_capital","inventory") or asset!=kind or sector!="firm":
            raise AccountingError("unsupported nontransaction change")
        nr=self.real.copy();key=(sector,asset);nr[key]=nr.get(key,D(0))+n
        self._check_positions(self.financial,nr)
        self.real=nr
        row=(self.period,sector,asset,n)
        (self.revaluations if category=="revaluation" else self.other_changes).append(row)

    def revalue(self,asset,delta):
        self._nontransaction_change(asset,"firm",asset,delta,"revaluation")

    def other_volume_change(self,asset,delta):
        self._nontransaction_change(asset,"firm",asset,delta,"other_volume")

    def begin(self,name):
        if self._opening is not None:self.close()
        self.period=name;self._start=len(self.events)
        self._rv_start=len(self.revaluations)
        self._oc_start=len(self.other_changes)
        self._opening={s:self.net_worth(s) for s in SECTORS}

    def close(self):
        if self._opening is None:raise AccountingError("no open period")
        current={s:D(0) for s in SECTORS}
        capital=current.copy();financial=current.copy()
        matrix=[]
        for _,kind,c,r,f in self.events[self._start:]:
            row={"kind":kind,"current":{},"capital":{},"financial":{}}
            for s in SECTORS:
                cv=c.get(s,D(0))
                rv=sum((v for (t,_),v in r.items() if t==s),D(0))
                fv=sum((v for (t,_),v in f.items() if t==s),D(0))
                current[s]+=cv;capital[s]+=rv;financial[s]+=fv
                row["current"][s]=cv;row["capital"][s]=rv;row["financial"][s]=fv
            matrix.append(row)
        rev={s:D(0) for s in SECTORS}
        other=rev.copy()
        for _,s,_,n in self.revaluations[self._rv_start:]:rev[s]+=n
        for _,s,_,n in self.other_changes[self._oc_start:]:other[s]+=n
        closing={s:self.net_worth(s) for s in SECTORS}
        for s in SECTORS:
            if current[s]!=capital[s]+financial[s]:raise AccountingError("transaction bridge")
            if closing[s]-self._opening[s]!=current[s]+rev[s]+other[s]:
                raise AccountingError("opening-closing bridge")
        if sum(financial.values(),D(0))!=0:raise AccountingError("financial sum")
        result={"period":self.period,"opening":self._opening.copy(),"closing":closing,
                "current":current,"capital":capital,"financial":financial,
                "revaluation":rev,"other_volume":other,"matrix":matrix}
        self.periods.append(result);self._opening=None
        return result
