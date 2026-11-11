"""National account consistency, including deliberate bad-flow rejection."""
import random, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from amfe.national_accounts_v01 import Flow,reconcile
from amfe.accounting_core import AccountingError,D
from scenario_gen import world_flows,flow_key,perturb

def sample():
    # A: GDP=100, NX=10. B: GDP=80, NX=-10.
    return [
        Flow("A",output=150,intermediate=50,compensation=60,operating_surplus=30,
             taxes_less_subsidies=10,household_consumption=60,government_consumption=15,
             fixed_investment=15,exports=20,imports=10),
        Flow("B",output=120,intermediate=40,compensation=50,operating_surplus=20,
             taxes_less_subsidies=10,household_consumption=65,government_consumption=15,
             fixed_investment=10,exports=10,imports=20),
    ]

class NationalAccountsTests(unittest.TestCase):
    def test_three_methods_and_world_trade(self):
        r=reconcile(sample())
        self.assertEqual(r["A"]["production"],D(100))
        self.assertEqual(r["B"]["income"],D(80))
        self.assertEqual(sum(v["net_exports"] for v in r.values()),D(0))
    def test_10000_distinct_scenarios_and_single_field_mutations(self):
        rng=random.Random(20261111)
        seen=set()
        for i in range(10000):
            a,b=world_flows(rng,i,rng.randint(0,50000),rng.randint(0,50000))
            seen.add(flow_key(a,b))
            r=reconcile([a,b])
            self.assertEqual(r["A"]["production"],r["A"]["expenditure"])
            self.assertEqual(r["B"]["income"],r["B"]["expenditure"])
            # Every consistent world must be rejected once any single field is broken.
            bad,_=perturb(rng,rng.choice((a,b)))
            with self.assertRaises(AccountingError):
                reconcile([bad if bad.country=="A" else a,bad if bad.country=="B" else b])
        self.assertEqual(len(seen),10000)
    def test_reject_offsetting_trade_errors(self):
        # Net exports cancel (+3 and -3) yet X_A != M_B and X_B != M_A.
        a,b=sample()
        a=Flow(**{**vars(a),"exports":12,"imports":9,"fixed_investment":22})
        b=Flow(**{**vars(b),"exports":7,"imports":10,"fixed_investment":3})
        self.assertEqual((a.exports-a.imports)+(b.exports-b.imports),0)
        with self.assertRaises(AccountingError):reconcile([a,b])
    def test_reject_mismatch(self):
        a,b=sample()
        with self.assertRaises(AccountingError):
            reconcile([Flow(**{**vars(a),"compensation":61}),b])
    def test_reject_trade_mismatch(self):
        a,b=sample()
        with self.assertRaises(AccountingError):
            reconcile([a,Flow(**{**vars(b),"exports":11,"household_consumption":64})])
    def test_reject_bad_countries_and_negative(self):
        a,b=sample()
        with self.assertRaises(AccountingError):reconcile([a,a])
        with self.assertRaises(AccountingError):reconcile([a,Flow(**{**vars(b),"imports":-1})])
        with self.assertRaises(AccountingError):reconcile([a])
if __name__=="__main__":unittest.main(verbosity=2)
