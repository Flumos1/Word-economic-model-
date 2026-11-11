"""Regression for accrual and other-change bridge."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from amfe.accounting_core import AccountingError,D,credit,produce
from amfe.extended_accounts import ExtendedAccounts

class ExtendedAccountingTests(unittest.TestCase):
    def test_accrue_then_settle(self):
        a=ExtendedAccounts();a.apply(credit(100))
        a.accrue_wages(25)
        self.assertEqual(a.financial[("firm","receivable")],D(-25))
        self.assertEqual(a.financial[("household","receivable")],D(25))
        self.assertEqual(a.financial[("household","deposit")],D(0)) if ("household","deposit") in a.financial else None
        a.settle_wages(10)
        self.assertEqual(a.financial[("firm","receivable")],D(-15))
        self.assertEqual(a.financial[("household","deposit")],D(10))
        p=a.close()
        self.assertEqual(p["current"]["household"],D(25))
        self.assertEqual(p["current"]["firm"],D(-25))
        self.assertEqual(p["financial"]["household"],D(25))
        self.assertEqual(p["financial"]["firm"],D(-25))
    def test_revaluation_and_volume(self):
        a=ExtendedAccounts();a.apply(produce(40))
        a.revalue("inventory",5);a.other_volume_change("inventory",-3)
        p=a.close()
        self.assertEqual(p["closing"]["firm"],D(42))
        self.assertEqual(p["current"]["firm"],D(40))
        self.assertEqual(p["revaluation"]["firm"],D(5))
        self.assertEqual(p["other_volume"]["firm"],D(-3))
    def test_two_periods(self):
        a=ExtendedAccounts();a.apply(credit(30));a.accrue_wages(12)
        p1=a.close();a.begin("P2");a.settle_wages(12)
        a.apply(produce(5));a.revalue("inventory",2)
        p2=a.close()
        self.assertEqual(p1["closing"],p2["opening"])
        self.assertEqual(p2["current"]["household"],D(0))
        self.assertEqual(p2["closing"]["firm"]-p2["opening"]["firm"],D(7))
    def test_rollback(self):
        a=ExtendedAccounts();a.apply(credit(10));a.accrue_wages(5)
        old=(a.financial.copy(),a.real.copy(),len(a.events),len(a.revaluations))
        for fn in (lambda:a.settle_wages(6),lambda:a.revalue("inventory",-1),
                   lambda:a.other_volume_change("fixed_capital",-2)):
            with self.assertRaises(AccountingError):fn()
            self.assertEqual((a.financial,a.real,len(a.events),len(a.revaluations)),old)
        a.close()
    def test_invalid_nonfinite(self):
        a=ExtendedAccounts()
        for fn in (lambda:a.accrue_wages("NaN"),lambda:a.revalue("inventory","Infinity"),
                   lambda:a.other_volume_change("inventory",True)):
            with self.assertRaises(AccountingError):fn()
if __name__=="__main__":unittest.main(verbosity=2)
