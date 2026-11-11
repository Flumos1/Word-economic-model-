"""International trade and FX conservation smoke tests."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from amfe.two_country_v01 import Country,World,ExternalAccountingError,amount

class WorldTests(unittest.TestCase):
    def world(self):
        w=World((Country("A","AAA"),Country("B","BBB")))
        w.endow("A","AAA",cash=100,goods=20)
        w.endow("B","BBB",cash=300,goods=5)
        w.endow("B","AAA",cash=20)
        w.endow("A","BBB",cash=10)
        return w
    def test_trade_and_mirrors(self):
        w=self.world();w.trade("A","B",4,3,"AAA")
        self.assertEqual(w.goods["A"],16)
        self.assertEqual(w.goods["B"],9)
        self.assertEqual(w.external[("A","AAA")],12)
        self.assertEqual(w.external[("B","AAA")],-12)
        self.assertTrue(w.verify())
    def test_fx_and_trade(self):
        w=self.world();w.exchange("B","A","BBB",30,"AAA",10)
        w.trade("A","B",5,5,"AAA")
        self.assertEqual(w.money[("B","AAA")],5)
        self.assertTrue(w.verify())
    def test_atomic_rejection(self):
        w=self.world();old=(dict(w.money),dict(w.goods),len(w.journal))
        for fn in (lambda:w.trade("A","B",21,1,"AAA"),
                   lambda:w.trade("A","B",1,100,"AAA"),
                   lambda:w.exchange("A","B","AAA",101,"BBB",1),
                   lambda:w.exchange("A","B","AAA",1,"BBB",301),
                   lambda:w.trade("A","B","NaN",1,"AAA")):
            with self.assertRaises(ExternalAccountingError):fn()
            self.assertEqual((dict(w.money),dict(w.goods),len(w.journal)),old)
    def test_separate_currency_units(self):
        w=self.world()
        total_a=sum(v for (s,c),v in w.money.items() if c=="AAA")
        total_b=sum(v for (s,c),v in w.money.items() if c=="BBB")
        w.exchange("A","B","AAA",10,"BBB",20)
        self.assertEqual(sum(v for (s,c),v in w.money.items() if c=="AAA"),total_a)
        self.assertEqual(sum(v for (s,c),v in w.money.items() if c=="BBB"),total_b)
        self.assertTrue(w.verify())

if __name__=="__main__":unittest.main(verbosity=2)
