"""Real posted trade must reconcile with country-level GDP inputs."""
import random,sys,unittest
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from amfe.accounting_core import AccountingError,D
from amfe.integrated_world_v01 import InternationalAccounts
from amfe.national_accounts_v01 import Flow
from amfe.national_accounts_bridge_v01 import posted_trade_values,reconcile_posted_trade
from scenario_gen import trade_world,world_flows,flow_key

def fixture():
    w=InternationalAccounts()
    w.endow("A","AAA",100,goods=20)
    w.endow("B","BBB",100,goods=10)
    w.exchange("A","B","AAA",10,"BBB",10)
    w.trade("A","B",4,2,"AAA")
    a=Flow("A",output=150,intermediate=50,compensation=60,
           operating_surplus=30,taxes_less_subsidies=10,
           household_consumption=60,government_consumption=15,
           fixed_investment=17,exports=8)
    b=Flow("B",output=120,intermediate=40,compensation=50,
           operating_surplus=20,taxes_less_subsidies=10,
           household_consumption=65,government_consumption=15,
           fixed_investment=8,imports=8)
    return w,a,b

class PostedTradeTests(unittest.TestCase):
    def test_posted_trade_and_gdp(self):
        w,a,b=fixture()
        result=reconcile_posted_trade(w,"P1","AAA",[a,b])
        self.assertEqual(result["trade_events"],1)
        self.assertEqual(result["accounts"]["A"]["production"],D(100))
        self.assertEqual(result["accounts"]["B"]["expenditure"],D(80))
    def test_10000_distinct_posted_reconciliations(self):
        rng=random.Random(20261112)
        seen=set()
        for i in range(10000):
            w,exporter,cur,value=trade_world(rng)
            a,b=world_flows(rng,i,value if exporter=="A" else 0,value if exporter=="B" else 0)
            seen.add(flow_key(a,b)+(exporter,cur))
            self.assertEqual(reconcile_posted_trade(w,"P1",cur,[a,b])["trade_events"],1)
            # Fabricated exports and a wrong reporting currency must both be rejected.
            fake=replace(a,exports=a.exports+D(rng.randint(1,100))/100)
            with self.assertRaises(AccountingError):reconcile_posted_trade(w,"P1",cur,[fake,b])
            other="BBB" if cur=="AAA" else "AAA"
            with self.assertRaises(AccountingError):reconcile_posted_trade(w,"P1",other,[a,b])
        self.assertEqual(len(seen),10000)
    def test_reject_fabricated_exports(self):
        w,a,b=fixture()
        with self.assertRaises(AccountingError):
            reconcile_posted_trade(w,"P1","AAA",[replace(a,exports=9),b])
    def test_reject_unconverted_invoice_currency(self):
        w,a,b=fixture()
        with self.assertRaises(AccountingError):
            reconcile_posted_trade(w,"P1","BBB",[a,b])
    def test_reject_wrong_period_and_missing_country(self):
        w,a,b=fixture()
        with self.assertRaises(AccountingError):
            reconcile_posted_trade(w,"P2","AAA",[a,b])
        with self.assertRaises(AccountingError):
            reconcile_posted_trade(w,"P1","AAA",[a])
    def test_no_trade_not_invented(self):
        w,a,b=fixture()
        self.assertEqual(posted_trade_values(w,"P2","AAA")["count"],0)
if __name__=="__main__":unittest.main(verbosity=2)
