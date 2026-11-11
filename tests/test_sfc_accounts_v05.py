"""AMFE four-sector stock-flow accounting, two periods, Decimal. Toy model."""
from collections import defaultdict
from decimal import Decimal
D=lambda x: Decimal(str(x))
SECTORS=("household","firm","bank","government")
class AccountingError(ValueError): pass

class Economy:
    def __init__(self):
        self.positions={}
        self.inventory=D(0)
        self.periods=[]
        self.history=[]
        self.label=None
        self.open_period("P1")

    def pos(self,s,a): return self.positions.get((s,a),D(0))
    def worth(self,s):
        return sum((v for (sector,_),v in self.positions.items() if sector==s),D(0))+(self.inventory if s=="firm" else D(0))
    def check(self):
        if sum(self.positions.values(),D(0))!=0: raise AccountingError("financial imbalance")
        if self.pos("bank","loan")!=-self.pos("firm","loan_debt"): raise AccountingError("loan mismatch")
        if self.pos("bank","bond")!=-self.pos("government","bond_debt"): raise AccountingError("bond mismatch")
        if self.pos("bank","deposit_debt")!=-sum((self.pos(s,"deposit") for s in ("firm","household","government")),D(0)): raise AccountingError("deposit mismatch")
        if self.inventory<0 or any(self.pos(s,"deposit")<0 for s in ("firm","household","government")): raise AccountingError("negative stock")
        if self.output!=self.household_use+self.government_use+self.inventory-self.open_inventory: raise AccountingError("goods mismatch")
        if sum((self.worth(s) for s in SECTORS),D(0))!=self.inventory: raise AccountingError("net worth mismatch")

    def post(self,name,positions,current=None,capital=None,inventory=0,output=0,household_use=0,government_use=0):
        positions={k:D(v) for k,v in positions.items()}
        current={k:D(v) for k,v in (current or {}).items()}
        capital={k:D(v) for k,v in (capital or {}).items()}
        inv=D(inventory)
        if any(s not in SECTORS for s,_ in positions) or any(s not in SECTORS for s in current) or any(s not in SECTORS for s in capital): raise AccountingError("unknown sector")
        if sum(positions.values(),D(0))!=0 or sum(current.values(),D(0))!=inv: raise AccountingError("unbalanced entries")
        if sum(capital.values(),D(0))!=inv or capital.get("firm",D(0))!=inv: raise AccountingError("capital mismatch")
        financial=defaultdict(lambda:D(0))
        for (s,_),v in positions.items(): financial[s]+=v
        for s in SECTORS:
            if current.get(s,D(0))!=financial[s]+capital.get(s,D(0)): raise AccountingError("flow-stock mismatch "+s)
        old=(self.positions.copy(),self.inventory,dict(self.current),dict(self.capital),dict(self.financial),self.output,self.household_use,self.government_use,list(self.history))
        try:
            for k,v in positions.items(): self.positions[k]=self.positions.get(k,D(0))+v
            for s,v in current.items(): self.current[s]+=v
            for s,v in capital.items(): self.capital[s]+=v
            for s,v in financial.items(): self.financial[s]+=v
            self.inventory+=inv
            self.output+=D(output)
            self.household_use+=D(household_use)
            self.government_use+=D(government_use)
            self.check()
        except Exception:
            (self.positions,self.inventory,cur,cap,fin,self.output,self.household_use,self.government_use,self.history)=old
            self.current=defaultdict(lambda:D(0),cur)
            self.capital=defaultdict(lambda:D(0),cap)
            self.financial=defaultdict(lambda:D(0),fin)
            raise
        self.history.append((self.label,name))

    def open_period(self,label):
        if self.label is not None:self.close_period()
        self.label=label
        self.opening={s:self.worth(s) for s in SECTORS}
        self.open_inventory=self.inventory
        self.current=defaultdict(lambda:D(0))
        self.capital=defaultdict(lambda:D(0))
        self.financial=defaultdict(lambda:D(0))
        self.output=self.household_use=self.government_use=D(0)

    def close_period(self):
        self.check()
        for s in SECTORS:
            if self.worth(s)-self.opening[s]!=self.current[s]: raise AccountingError("net worth bridge "+s)
            if self.current[s]-self.capital[s]!=self.financial[s]: raise AccountingError("sector flow bridge "+s)
        inv_delta=self.inventory-self.open_inventory
        if sum((self.current[s] for s in SECTORS),D(0))!=inv_delta: raise AccountingError("aggregate current mismatch")
        if sum((self.financial[s] for s in SECTORS),D(0))!=0: raise AccountingError("aggregate financial mismatch")
        if sum((self.capital[s] for s in SECTORS),D(0))!=inv_delta: raise AccountingError("aggregate capital mismatch")
        result={"period":self.label,"opening":self.opening.copy(),"closing":{s:self.worth(s) for s in SECTORS},"net_worth_change":{s:self.current[s] for s in SECTORS},"real_asset_change":{s:self.capital[s] for s in SECTORS},"financial_change":{s:self.financial[s] for s in SECTORS},"output":self.output,"household_use":self.household_use,"government_use":self.government_use,"inventory_change":inv_delta}
        self.periods.append(result)
        return result

    def loan(self,n):
        n=D(n)
        if n<0:raise AccountingError("negative loan")
        self.post("loan",{("bank","loan"):n,("firm","loan_debt"):-n,("firm","deposit"):n,("bank","deposit_debt"):-n})
    def bond(self,n):
        n=D(n)
        if n<0:raise AccountingError("negative bond")
        self.post("bond",{("bank","bond"):n,("government","bond_debt"):-n,("government","deposit"):n,("bank","deposit_debt"):-n})
    def transfer(self,payer,receiver,n,label="transfer"):
        n=D(n)
        if n<0 or payer==receiver or payer not in ("firm","household","government") or receiver not in ("firm","household","government"):raise AccountingError("invalid transfer")
        self.post(label,{(payer,"deposit"):-n,(receiver,"deposit"):n},current={payer:-n,receiver:n})
    def produce(self,n):
        n=D(n)
        if n<0:raise AccountingError("negative production")
        self.post("production",{},current={"firm":n},capital={"firm":n},inventory=n,output=n)
    def buy(self,buyer,n):
        n=D(n)
        if n<0 or buyer not in ("household","government"):raise AccountingError("invalid purchase")
        self.post("final purchase",{(buyer,"deposit"):-n,("firm","deposit"):n},current={buyer:-n},capital={"firm":-n},inventory=-n,household_use=n if buyer=="household" else 0,government_use=n if buyer=="government" else 0)
    def interest(self,n):
        n=D(n)
        if n<0:raise AccountingError("negative interest")
        self.post("interest",{("firm","deposit"):-n,("bank","deposit_debt"):n},current={"firm":-n,"bank":n})
    def repay(self,n):
        n=D(n)
        if n<0 or n>self.pos("bank","loan"):raise AccountingError("invalid repayment")
        self.post("repay",{("firm","deposit"):-n,("bank","deposit_debt"):n,("bank","loan"):-n,("firm","loan_debt"):n})

def test_full():
    e=Economy();e.loan(100);e.produce(100);e.transfer("firm","household",100,"wages")
    e.buy("household",70);e.transfer("firm","government",30,"levy")
    e.transfer("government","household",10,"benefit");e.buy("government",15)
    e.interest(5);e.repay(50);r=e.close_period()
    assert r["closing"]=={"household":D(40),"firm":D(-35),"bank":D(5),"government":D(5)}
    assert r["real_asset_change"]["firm"]==15 and r["financial_change"]["firm"]==-50
    assert r["output"]==r["household_use"]+r["government_use"]+r["inventory_change"]==100

def test_two_periods():
    e=Economy();e.loan(100);e.produce(100);e.transfer("firm","household",100,"wages");e.buy("household",80)
    p1=e.close_period();e.open_period("P2");e.buy("household",10);p2=e.close_period()
    assert p2["opening"]==p1["closing"] and p2["output"]==0
    assert p2["inventory_change"]==-10 and p2["household_use"]==10 and e.inventory==10

def test_government_bond():
    e=Economy();e.loan(100);e.bond(20);e.produce(100)
    e.transfer("firm","household",100);e.buy("household",70)
    e.transfer("firm","government",5);e.transfer("government","household",10)
    e.buy("government",15);e.repay(80)
    r=e.close_period()
    assert r["closing"]["government"]==-20 and e.pos("government","deposit")==0

def test_atomic_and_decimal():
    e=Economy();e.loan("10.25");e.produce("10.25");e.transfer("firm","household","10.25")
    e.buy("household","4.10");assert e.inventory==D("6.15")
    old=(e.positions.copy(),e.inventory,len(e.history))
    for operation in (lambda:e.buy("household",7),lambda:e.post("bad",{("firm","deposit"):1}),lambda:e.produce(-1),lambda:e.post("bad-flow",{},current={"firm":1})):
        try:operation()
        except AccountingError:pass
        else:raise AssertionError("invalid operation accepted")
        assert old==(e.positions,e.inventory,len(e.history))
    e.close_period()

def test_grid():
    count=0
    for sales in range(50,90,5):
        for levy in range(0,25,3):
            for public in range(0,11,2):
                e=Economy();e.loan(100);e.produce(100);e.transfer("firm","household",100)
                e.buy("household",sales);e.transfer("firm","government",levy)
                transfer=levy//2
                if transfer:e.transfer("government","household",transfer)
                need=max(0,public+transfer-levy)
                if need:e.bond(need)
                e.buy("government",public);e.repay(min(D(100),e.pos("firm","deposit")))
                p=e.close_period()
                assert p["output"]==100 and p["inventory_change"]==100-sales-public
                count+=1
    assert count==432

if __name__=="__main__":
    for t in (test_full,test_two_periods,test_government_bond,test_atomic_and_decimal,test_grid):
        t();print("PASS",t.__name__)
