"""Deterministic 120-period integrated-world exercise and 10,000 accounting cases.

This tests accounting identities, not empirical forecast accuracy.
"""
import sys,unittest
from pathlib import Path
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from amfe.integrated_world_v01 import InternationalAccounts
from amfe.accounting_core import AccountingError,D

class LongRun(unittest.TestCase):
    def test_120_periods(self):
        w=InternationalAccounts()
        w.endow("A","AAA",100000,goods=10000)
        w.endow("B","BBB",100000,goods=10000)
        for t in range(120):
            if t:w.begin("P"+str(t+1))
            w.exchange("A","B","AAA",10,"BBB",10)
            w.trade("A","B",2,2,"AAA")
            w.trade("B","A",1,1,"BBB")
            w.transfer("A","firm","household",1,"AAA")
            w.transfer("B","firm","government",1,"BBB")
            p=w.close()
            self.assertEqual(sum(p["current"].values(),D(0)),D(0))
            for sector in p["opening"]:
                self.assertEqual(p["closing"][sector]-p["opening"][sector],p["current"].get(sector,D(0)))
        self.assertEqual(len(w.periods),120)
        self.assertEqual(len(w.journal),600)
    def test_10000_mirrors_and_atomicity(self):
        for i in range(10000):
            w=InternationalAccounts()
            w.endow("A","AAA",100,goods=10)
            w.endow("B","BBB",100,goods=10)
            q=D((i%9)+1)/D(10)
            w.exchange("A","B","AAA",1,"BBB",1)
            w.trade("A","B",q,1,"AAA")
            p=w.close()
            self.assertEqual(sum(p["current"].values(),D(0)),D(0))
            for key in p["opening"]:
                self.assertEqual(p["closing"][key]-p["opening"][key],p["current"].get(key,D(0)))
            with self.assertRaises(AccountingError):w.trade("A","B",1,1,"AAA")

if __name__=="__main__":unittest.main(verbosity=2)
