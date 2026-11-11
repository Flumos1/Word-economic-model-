"""Domestic event journal + posted cross-border trade reconciliation."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from amfe.accounting_core import AccountingError,D
from amfe.integrated_world_v01 import InternationalAccounts
from amfe.domestic_journal_v01 import DomesticJournal,reconcile_journals

def fixture():
    world=InternationalAccounts()
    world.endow("A","AAA",100,goods=20)
    world.endow("B","BBB",100,goods=10)
    world.exchange("A","B","AAA",10,"BBB",10)
    world.trade("A","B",4,2,"AAA")
    journal=DomesticJournal()
    a={"output":150,"intermediate":50,"wage":60,
       "operating_surplus":30,"production_tax_net":10,
       "household_consumption":60,"government_consumption":15,
       "fixed_investment":17}
    b={"output":120,"intermediate":40,"wage":50,
       "operating_surplus":20,"production_tax_net":10,
       "household_consumption":65,"government_consumption":15,
       "fixed_investment":8}
    for country,items in (("A",a),("B",b)):
        for kind,value in items.items():
            journal.post("P1",country,kind,value)
    return world,journal

class JournalTests(unittest.TestCase):
    def test_gdp_derived_from_events(self):
        w,j=fixture()
        result=reconcile_journals(j,w,"P1","AAA")
        self.assertEqual(result["domestic_events"],16)
        self.assertEqual(result["trade_events"],1)
        self.assertEqual(result["accounts"]["A"]["production"],D(100))
        self.assertEqual(result["accounts"]["B"]["income"],D(80))
    def test_10000_parameter_variations(self):
        w,j=fixture()
        for i in range(10000):
            shift=D(i%101)/100
            x=DomesticJournal()
            for e in j.events:
                amount=e.amount
                if e.country=="A" and e.kind=="wage":amount+=shift
                if e.country=="A" and e.kind=="operating_surplus":amount-=shift
                x.post(e.period,e.country,e.kind,amount)
            self.assertEqual(reconcile_journals(x,w,"P1","AAA")["accounts"]["A"]["expenditure"],D(100))
    def test_inconsistent_wage_rejected(self):
        w,j=fixture()
        j.post("P1","A","wage",1)
        with self.assertRaises(AccountingError):reconcile_journals(j,w,"P1","AAA")
    def test_inconsistent_expenditure_rejected(self):
        w,j=fixture()
        j.post("P1","B","household_consumption",1)
        with self.assertRaises(AccountingError):reconcile_journals(j,w,"P1","AAA")
    def test_wrong_currency_and_period_rejected(self):
        w,j=fixture()
        with self.assertRaises(AccountingError):reconcile_journals(j,w,"P1","BBB")
        with self.assertRaises(AccountingError):reconcile_journals(j,w,"P2","AAA")
    def test_invalid_event_is_atomic(self):
        _,j=fixture()
        old=list(j.events)
        for args in (("P1","A","wage",-1),("P1","C","wage",1),
                     ("P1","A","made_up",1),("P1","A","wage",float("nan"))):
            with self.assertRaises(AccountingError):j.post(*args)
            self.assertEqual(j.events,old)
if __name__=="__main__":unittest.main(verbosity=2)
