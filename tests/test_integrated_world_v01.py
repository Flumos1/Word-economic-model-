"""Cross-country four-sector reconciliation and rollback checks."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from amfe.integrated_world_v01 import InternationalAccounts,COUNTRIES,SECTORS
from amfe.accounting_core import D,AccountingError

class IntegrationTests(unittest.TestCase):
    def scenario(self):
        w=InternationalAccounts()
        w.endow("A","AAA",100,goods=20)
        w.endow("B","BBB",100,goods=5)
        w.exchange("A","B","AAA",10,"BBB",10)
        w.trade("A","B",4,2,"AAA")
        return w
    def test_consolidated_mirror(self):
        w=self.scenario()
        self.assertEqual(w.goods["A"],D(16))
        self.assertEqual(w.goods["B"],D(9))
        self.assertEqual(w.financial[("A","firm","deposit","AAA")],D(98))
        self.assertEqual(w.financial[("B","firm","deposit","AAA")],D(2))
        p=w.close()
        self.assertEqual(len(p["matrix"]),2)
        self.assertEqual(p["current"][("A","firm")],D(4))
        self.assertEqual(p["current"][("B","firm")],D(-4)) # imported inventory +4, invoice payment -8
        self.assertEqual(p["current"].get(("B","household"),D(0)),D(0))
        self.assertEqual(sum(p["current"].values()),D(0))
    def test_two_periods(self):
        w=self.scenario();first=w.close();w.begin("P2")
        w.transfer("A","firm","government",3,"AAA")
        second=w.close()
        self.assertEqual(first["closing"],second["opening"])
        self.assertEqual(second["current"][("A","government")],D(3))
    def test_rejection_atomic(self):
        w=self.scenario();old=(dict(w.financial),dict(w.goods),len(w.journal))
        for fn in (lambda:w.trade("A","B",100,1,"AAA"),
                   lambda:w.trade("A","B",1,1000,"AAA"),
                   lambda:w.exchange("A","B","AAA",2,"BBB",3),
                   lambda:w.transfer("B","household","firm",1000,"AAA")):
            with self.assertRaises(AccountingError):fn()
            self.assertEqual((dict(w.financial),dict(w.goods),len(w.journal)),old)
    def test_unsupported_fx_valuation(self):
        w=InternationalAccounts();w.endow("A","AAA",10);w.endow("B","BBB",10)
        with self.assertRaises(AccountingError):w.exchange("A","B","AAA",2,"BBB",4)
    def test_no_post_after_close(self):
        w=self.scenario();w.close()
        with self.assertRaises(AccountingError):w.transfer("A","firm","household",1)
    def test_reject_wrong_loan_and_bond_signs(self):
        w=InternationalAccounts()
        w.endow("A","AAA",100)
        before=(dict(w.financial),len(w.journal))
        for inst,sector in (("loan","firm"),("bond","government")):
            with self.assertRaises(AccountingError):
                w.post("invalid_"+inst,financial={
                    ("A","bank",inst,"AAA"):-1,
                    ("A",sector,inst,"AAA"):1
                },current={("A","bank"):-1,("A",sector):1})
            self.assertEqual((dict(w.financial),len(w.journal)),before)
if __name__=="__main__":unittest.main(verbosity=2)
